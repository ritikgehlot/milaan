"""Tests for the complete synthetic demo workflow."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path


class DemoWorkflowTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="milaan-test-")
        self._old_env = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(Path(self._tmp) / "demo.db")
        import db

        db.init_db()
        self.db = db

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._old_env
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_demo_runs_through_real_pipeline(self):
        import demo_workflow

        result = demo_workflow.load_demo_workspace()
        self.assertTrue(result["imported"])
        self.assertTrue(result["reconciled"])

        summary = self.db.get_validation_summary()
        self.assertEqual(summary["valid"], 5)
        self.assertEqual(summary["invalid"], 1)
        self.assertEqual(summary["needs_review"], 0)

        imports = self.db.get_gstr2b_imports()
        self.assertEqual(len(imports), 1)
        self.assertEqual(imports[0]["Accepted"], 2)
        self.assertEqual(imports[0]["Rejected"], 1)

        run_summary = self.db.get_reconciliation_summary(result["run_id"])
        counts = {
            status: item["count"]
            for status, item in run_summary["by_status"].items()
        }
        self.assertEqual(counts["MATCHED"], 1)
        self.assertEqual(counts["AMOUNT_MISMATCH"], 1)
        self.assertEqual(counts["MISSING_FROM_2B"], 3)
        self.assertEqual(counts["NEEDS_REVIEW"], 0)
        self.assertEqual(counts["NOT_ELIGIBLE"], 1)

    def test_demo_is_idempotent_and_sends_nothing(self):
        import demo_workflow

        first = demo_workflow.load_demo_workspace()
        second = demo_workflow.load_demo_workspace()
        self.assertEqual(second["seeded"], {"suppliers": 0, "invoices": 0})
        self.assertFalse(second["imported"])
        self.assertFalse(second["reconciled"])
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertEqual(len(self.db.get_gstr2b_imports()), 1)
        self.assertEqual(len(self.db.get_reconciliation_runs()), 1)
        self.assertEqual(self.db.get_followup_drafts(), [])
        self.assertEqual(self.db.get_message_deliveries(), [])

    def test_demo_contains_valid_match_mismatch_and_validation_failure(self):
        import demo_workflow

        demo_workflow.load_demo_workspace()
        invoices = {row["Invoice"]: row for row in self.db.get_invoices()}
        self.assertEqual(invoices["INV-DEMO-001"]["Validation"], "valid")
        self.assertEqual(invoices["INV-DEMO-201"]["Validation"], "invalid")
        self.assertEqual(invoices["INV-DEMO-201"]["Source"], "demo-seed")


if __name__ == "__main__":
    unittest.main()
