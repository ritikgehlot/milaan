"""End-to-end tests for the core invoice-to-follow-up workflow.

Run: .venv/bin/python -m unittest test_e2e -v

Every supplier, invoice and GSTR-2B row is synthetic. These tests exercise
the real parser, validation, persistence, reconciliation, draft generation
and approval code against a temporary SQLite database. Nothing is sent.
"""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import gstr2b  # noqa: E402

_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_CSV_HEADER = (
    "supplier_gstin,invoice_number,invoice_date,tax_period,"
    "taxable_value,cgst,sgst,igst,total_gst"
)


def reference_checksum(first_14: str) -> str:
    total = 0
    for position, character in enumerate(first_14):
        value = _CHARSET.index(character.upper())
        product = value * (1 if position % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _CHARSET[(36 - total % 36) % 36]


def make_gstin(first_14: str) -> str:
    return first_14 + reference_checksum(first_14)


GSTIN_A = make_gstin("27AAACS1234A1Z")
GSTIN_B = make_gstin("29AAACB5678B1Z")
GSTIN_C = make_gstin("24AAACD9012C1Z")
PERIOD = "2026-03"


def rupees(paise: int) -> str:
    return f"{paise // 100}.{paise % 100:02d}"


def csv_bytes(rows) -> bytes:
    lines = [_CSV_HEADER]
    for values in rows:
        lines.append(",".join(str(value) for value in values))
    return ("\n".join(lines) + "\n").encode("utf-8")


def gstr2b_row(
    gstin,
    invoice_number,
    *,
    total_gst=225_000,
    taxable=1_250_000,
    invoice_date="2026-03-05",
    tax_period=PERIOD,
):
    cgst = total_gst // 2
    sgst = total_gst - cgst
    return [
        gstin,
        invoice_number,
        invoice_date,
        tax_period,
        rupees(taxable),
        rupees(cgst),
        rupees(sgst),
        "0.00",
        rupees(total_gst),
    ]


class WorkflowTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="milaan-test-")
        self._old_env = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(Path(self._tmp) / "test_milaan.db")
        db.init_db()

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._old_env
        shutil.rmtree(self._tmp, ignore_errors=True)

    def add_supplier(self, name, gstin, phone="9999999999"):
        with db.db() as conn:
            return conn.execute(
                """INSERT INTO suppliers (supplier_name, gstin, contact_phone)
                   VALUES (?, ?, ?)""",
                (name, gstin, phone),
            ).lastrowid

    def add_invoice(
        self,
        supplier_id,
        number,
        *,
        taxable=1_250_000,
        total_gst=225_000,
        invoice_date="2026-03-05",
        tax_period=PERIOD,
    ):
        cgst = total_gst // 2
        sgst = total_gst - cgst
        with db.db() as conn:
            return conn.execute(
                """INSERT INTO invoices
                       (supplier_id, invoice_number, invoice_date, tax_period,
                        taxable_value, gst_rate, cgst, sgst, igst, total_gst,
                        invoice_total, source)
                   VALUES (?, ?, ?, ?, ?, 18, ?, ?, 0, ?, ?, 'e2e')""",
                (
                    supplier_id,
                    number,
                    invoice_date,
                    tax_period,
                    taxable,
                    cgst,
                    sgst,
                    total_gst,
                    taxable + total_gst,
                ),
            ).lastrowid

    def import_rows(self, rows, file_name="gstr2b.csv"):
        content = csv_bytes(rows)
        parsed = gstr2b.parse_gstr2b(content, file_name)
        saved = db.save_gstr2b_import(parsed)
        return saved, content, parsed

    def missing_and_matched_case(self):
        supplier_a = self.add_supplier("Alpha Synthetic Traders", GSTIN_A)
        missing_invoice = self.add_invoice(
            supplier_a, "INV-A-MISSING", total_gst=225_000
        )
        supplier_b = self.add_supplier(
            "Beta Synthetic Traders", GSTIN_B, "8888888888"
        )
        matched_invoice = self.add_invoice(
            supplier_b,
            "INV-B-MATCHED",
            taxable=500_000,
            total_gst=90_000,
        )
        db.revalidate_all_invoices()
        saved, content, parsed = self.import_rows([
            gstr2b_row(
                GSTIN_B,
                "INV-B-MATCHED",
                taxable=500_000,
                total_gst=90_000,
            )
        ])
        summary = db.reconcile_gstr2b_import(saved["import_id"])
        return {
            "supplier_a": supplier_a,
            "supplier_b": supplier_b,
            "missing_invoice": missing_invoice,
            "matched_invoice": matched_invoice,
            "batch_id": saved["import_id"],
            "content": content,
            "parsed": parsed,
            "run_id": summary["run_id"],
            "summary": summary,
        }

    def draft_count(self):
        with db.db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM followup_drafts"
            ).fetchone()[0]

    def delivery_count(self):
        with db.db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM message_deliveries"
            ).fetchone()[0]


class CoreWorkflowTests(WorkflowTestCase):
    def test_clean_workflow_produces_and_approves_one_draft(self):
        case = self.missing_and_matched_case()
        self.assertEqual(case["summary"]["matched_count"], 1)
        self.assertEqual(case["summary"]["missing_count"], 1)
        self.assertEqual(case["summary"]["amount_mismatch_count"], 0)
        self.assertEqual(case["summary"]["needs_review_count"], 0)
        self.assertEqual(case["summary"]["not_eligible_count"], 0)

        results = {
            row["Invoice number"]: row
            for row in db.get_reconciliation_results(case["run_id"])
        }
        self.assertEqual(results["INV-B-MATCHED"]["Status"], "MATCHED")
        self.assertEqual(results["INV-A-MISSING"]["Status"], "MISSING_FROM_2B")
        self.assertTrue(results["INV-B-MATCHED"]["2B row"])
        self.assertFalse(results["INV-A-MISSING"]["2B row"])

        eligibility = db.get_followup_eligibility(case["batch_id"])
        self.assertEqual(eligibility["run"]["id"], case["run_id"])
        self.assertEqual(eligibility["eligible_invoice_count"], 1)
        self.assertEqual(len(eligibility["plans"]), 1)
        self.assertEqual(
            eligibility["plans"][0]["supplier_id"], case["supplier_a"]
        )

        generated = db.generate_followup_drafts(case["batch_id"])
        self.assertEqual(generated["created_drafts"], 1)
        self.assertEqual(generated["existing_drafts"], 0)
        draft_id = generated["draft_ids"][0]
        draft = db.get_followup_draft(draft_id)
        self.assertEqual(draft["status"], "DRAFT")
        self.assertEqual(draft["supplier_name"], "Alpha Synthetic Traders")
        self.assertTrue(draft["channel_available"])
        self.assertEqual(draft["review_reasons"], [])
        self.assertIn("INV-A-MISSING", draft["draft_text"])
        self.assertIn(f"batch #{case['batch_id']}", draft["draft_text"])
        self.assertIn(f"run #{case['run_id']}", draft["draft_text"])

        self.assertEqual(db.get_followup_approval_problems(draft_id), [])
        self.assertEqual(self.delivery_count(), 0)
        db.approve_followup_draft(draft_id, confirmed=True)

        approved = db.get_followup_draft(draft_id)
        self.assertEqual(approved["status"], "APPROVED")
        self.assertEqual(approved["approved_text"], draft["draft_text"])
        self.assertEqual(approved["approved_recipient_phone"], "9999999999")
        self.assertTrue(approved["approved_at"])
        events = db.get_followup_draft_events(draft_id)
        self.assertEqual(
            [(event["Event"], event["From"], event["To"]) for event in events],
            [
                ("CREATED", "", "DRAFT"),
                ("STATUS_CHANGED", "DRAFT", "APPROVED"),
            ],
        )
        self.assertIn("no message sent", events[-1]["Note"].lower())
        self.assertEqual(self.delivery_count(), 0)

        with self.assertRaises(ValueError):
            db.approve_followup_draft(draft_id, confirmed=True)
        self.assertEqual(
            len(db.get_followup_draft_events(draft_id)), len(events)
        )
        self.assertEqual(self.delivery_count(), 0)

    def test_exact_and_amount_mismatch_are_excluded_from_drafts(self):
        supplier = self.add_supplier("Gamma Synthetic Traders", GSTIN_C)
        exact_id = self.add_invoice(
            supplier, "INV-C-EXACT", total_gst=225_000
        )
        mismatch_id = self.add_invoice(
            supplier, "INV-C-DIFF", total_gst=225_000
        )
        missing_id = self.add_invoice(
            supplier,
            "INV-C-MISSING",
            taxable=500_000,
            total_gst=90_000,
        )
        db.revalidate_all_invoices()
        saved, _, _ = self.import_rows([
            gstr2b_row(GSTIN_C, "INV-C-EXACT", total_gst=225_000),
            gstr2b_row(GSTIN_C, "INV-C-DIFF", total_gst=225_100),
        ])
        summary = db.reconcile_gstr2b_import(saved["import_id"])
        self.assertEqual(summary["matched_count"], 1)
        self.assertEqual(summary["amount_mismatch_count"], 1)
        self.assertEqual(summary["missing_count"], 1)

        results = {
            row["Invoice number"]: row
            for row in db.get_reconciliation_results(summary["run_id"])
        }
        self.assertEqual(results["INV-C-EXACT"]["Invoice #"], exact_id)
        self.assertEqual(results["INV-C-EXACT"]["Status"], "MATCHED")
        self.assertEqual(results["INV-C-DIFF"]["Invoice #"], mismatch_id)
        self.assertEqual(
            results["INV-C-DIFF"]["Status"], "AMOUNT_MISMATCH"
        )
        self.assertEqual(results["INV-C-DIFF"]["Difference (Rs)"], "-1.00")
        self.assertEqual(results["INV-C-MISSING"]["Invoice #"], missing_id)
        self.assertEqual(
            results["INV-C-MISSING"]["Status"], "MISSING_FROM_2B"
        )

        eligibility = db.get_followup_eligibility(saved["import_id"])
        self.assertEqual(eligibility["eligible_invoice_count"], 1)
        self.assertEqual(len(eligibility["plans"]), 1)
        planned = eligibility["plans"][0]["invoices"]
        self.assertEqual(
            [invoice["invoice_number"] for invoice in planned],
            ["INV-C-MISSING"],
        )
        excluded = {
            item["invoice_number"]: item["reason"]
            for item in eligibility["excluded"]
        }
        self.assertEqual(
            excluded["INV-C-EXACT"], "MATCH_STATUS_MATCHED"
        )
        self.assertEqual(
            excluded["INV-C-DIFF"], "MATCH_STATUS_AMOUNT_MISMATCH"
        )

        generated = db.generate_followup_drafts(saved["import_id"])
        draft_invoices = db.get_followup_draft_invoices(
            generated["draft_ids"][0]
        )
        self.assertEqual(
            [row["Invoice number"] for row in draft_invoices],
            ["INV-C-MISSING"],
        )

    def test_invalid_invoice_is_not_eligible_for_drafts(self):
        supplier_a = self.add_supplier("Delta Synthetic Traders", GSTIN_A)
        invalid_id = self.add_invoice(
            supplier_a,
            "INV-D-INVALID",
            invoice_date="2026-02-30",
        )
        supplier_b = self.add_supplier("Epsilon Synthetic Traders", GSTIN_B)
        matched_id = self.add_invoice(
            supplier_b,
            "INV-E-MATCHED",
            taxable=500_000,
            total_gst=90_000,
        )
        entries = db.revalidate_all_invoices()
        statuses = {entry["invoice_id"]: entry["result"].status
                    for entry in entries}
        self.assertEqual(statuses[invalid_id], "invalid")
        self.assertEqual(statuses[matched_id], "valid")

        with db.db() as conn:
            persisted = dict(conn.execute(
                "SELECT id, validation_status FROM invoices"
            ).fetchall())
        self.assertEqual(persisted[invalid_id], "invalid")
        self.assertEqual(persisted[matched_id], "valid")

        saved, _, _ = self.import_rows([
            gstr2b_row(GSTIN_A, "INV-D-INVALID"),
            gstr2b_row(
                GSTIN_B,
                "INV-E-MATCHED",
                taxable=500_000,
                total_gst=90_000,
            ),
        ])
        summary = db.reconcile_gstr2b_import(saved["import_id"])
        self.assertEqual(summary["matched_count"], 1)
        self.assertEqual(summary["not_eligible_count"], 1)

        results = {
            row["Invoice number"]: row
            for row in db.get_reconciliation_results(summary["run_id"])
        }
        self.assertEqual(results["INV-D-INVALID"]["Status"], "NOT_ELIGIBLE")
        self.assertEqual(results["INV-E-MATCHED"]["Status"], "MATCHED")

        eligibility = db.get_followup_eligibility(saved["import_id"])
        self.assertEqual(eligibility["plans"], [])
        self.assertEqual(
            eligibility["excluded"][0]["reason"], "INVOICE_NOT_VALID:invalid"
        )
        with self.assertRaises(ValueError):
            db.generate_followup_drafts(saved["import_id"])
        self.assertEqual(self.draft_count(), 0)

    def test_duplicate_file_import_is_explicit_and_independent(self):
        supplier = self.add_supplier("Zeta Synthetic Traders", GSTIN_A)
        self.add_invoice(supplier, "INV-F-1")
        db.revalidate_all_invoices()
        first, content, _ = self.import_rows(
            [gstr2b_row(GSTIN_A, "INV-F-1")], "same-file.csv"
        )
        second, _, _ = self.import_rows(
            [gstr2b_row(GSTIN_A, "INV-F-1")], "same-file.csv"
        )
        self.assertNotEqual(first["import_id"], second["import_id"])

        file_hash = gstr2b.sha256_hex(content)
        prior = db.find_gstr2b_imports_by_hash(file_hash)
        self.assertEqual(
            [row["id"] for row in prior],
            [second["import_id"], first["import_id"]],
        )
        for saved in (first, second):
            batch = db.get_gstr2b_import(saved["import_id"])
            self.assertEqual(batch["file_hash"], file_hash)
            self.assertEqual(batch["total_rows"], 1)
            self.assertEqual(batch["accepted_rows"], 1)
            self.assertEqual(
                len(db.get_gstr2b_rows(saved["import_id"])), 1
            )

        first_run = db.reconcile_gstr2b_import(first["import_id"])
        second_run = db.reconcile_gstr2b_import(second["import_id"])
        self.assertNotEqual(first_run["run_id"], second_run["run_id"])
        self.assertEqual(first_run["matched_count"], 1)
        self.assertEqual(second_run["matched_count"], 1)
        self.assertEqual(
            db.get_latest_reconciliation_run()["id"], second_run["run_id"]
        )
        self.assertFalse(
            db.get_reconciliation_run(first_run["run_id"])["is_latest"]
        )

    def test_regeneration_preserves_edits_and_does_not_duplicate(self):
        case = self.missing_and_matched_case()
        first = db.generate_followup_drafts(case["batch_id"])
        draft_id = first["draft_ids"][0]
        db.update_followup_draft_text(draft_id, "Please verify INV-A-MISSING.")

        before_revalidation = db.get_invoices()
        db.revalidate_all_invoices()
        db.revalidate_all_invoices()
        self.assertEqual(db.get_invoices(), before_revalidation)

        second = db.generate_followup_drafts(case["batch_id"])
        self.assertEqual(second["created_drafts"], 0)
        self.assertEqual(second["existing_drafts"], 1)
        self.assertEqual(second["draft_ids"], [])
        self.assertEqual(self.draft_count(), 1)
        draft = db.get_followup_draft(draft_id)
        self.assertEqual(
            draft["draft_text"], "Please verify INV-A-MISSING."
        )
        self.assertEqual(
            [event["Event"] for event in db.get_followup_draft_events(draft_id)],
            ["CREATED", "EDITED"],
        )

    def test_approval_is_blocked_after_evidence_becomes_stale(self):
        case = self.missing_and_matched_case()
        draft_id = db.generate_followup_drafts(
            case["batch_id"]
        )["draft_ids"][0]
        self.assertEqual(db.get_followup_approval_problems(draft_id), [])

        newer = db.reconcile_gstr2b_import(case["batch_id"])
        self.assertNotEqual(newer["run_id"], case["run_id"])
        problems = db.get_followup_approval_problems(draft_id)
        self.assertIn("STALE_RECONCILIATION_RUN", problems)
        with self.assertRaises(ValueError) as caught:
            db.approve_followup_draft(draft_id, confirmed=True)
        self.assertIn("STALE_RECONCILIATION_RUN", str(caught.exception))
        self.assertEqual(
            db.get_followup_draft(draft_id)["status"], "DRAFT"
        )

        replacement_id = db.generate_followup_drafts(
            case["batch_id"]
        )["draft_ids"][0]
        self.assertNotEqual(replacement_id, draft_id)
        db.approve_followup_draft(replacement_id, confirmed=True)
        self.assertEqual(
            db.get_followup_draft(replacement_id)["status"], "APPROVED"
        )
        self.assertEqual(self.delivery_count(), 0)

    def test_ui_generates_and_approves_without_delivery(self):
        from streamlit.testing.v1 import AppTest

        case = self.missing_and_matched_case()
        at = AppTest.from_file(
            str(Path(__file__).parent / "app.py"), default_timeout=30
        )
        at.run()
        self.assertEqual(at.exception, [])
        at.switch_page("pages/6_followups.py").run()
        self.assertEqual(at.exception, [])

        at.button(key="generate_followups").click().run()
        self.assertEqual(at.exception, [])
        self.assertEqual(self.draft_count(), 1)
        draft_id = db.get_followup_drafts(
            gstr2b_import_id=case["batch_id"]
        )[0]["Draft"]

        checkbox_key = f"followup_confirm_{draft_id}"
        approve_key = f"followup_approve_{draft_id}"
        confirm = next(
            item for item in at.checkbox if item.key == checkbox_key
        )
        confirm.set_value(True).run()
        self.assertEqual(at.exception, [])
        approve = next(
            item for item in at.button if item.key == approve_key
        )
        self.assertFalse(approve.disabled)
        approve.click().run()
        self.assertEqual(at.exception, [])

        self.assertEqual(
            db.get_followup_draft(draft_id)["status"], "APPROVED"
        )
        self.assertEqual(self.delivery_count(), 0)
        warning_text = " ".join(item.value for item in at.warning)
        self.assertIn("No message has been sent", warning_text)
        success_text = " ".join(item.value for item in at.success)
        self.assertIn(
            f"Draft #{draft_id} APPROVED for delivery review", success_text
        )
        self.assertIn("No message has been sent", success_text)


if __name__ == "__main__":
    unittest.main()
