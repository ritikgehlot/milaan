"""Regression tests for the deterministic synthetic benchmark.

Run: .venv/bin/python -m unittest test_benchmark -v
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import benchmark  # noqa: E402


class BenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.first = benchmark.run_benchmark()
        cls.second = benchmark.run_benchmark()
        cls.metrics = cls.first["metrics"]

    def test_benchmark_is_reproducible(self):
        self.assertEqual(self.first["metrics"], self.second["metrics"])
        self.assertEqual(self.first["per_invoice"], self.second["per_invoice"])
        self.assertEqual(self.metrics["invoices_total"], 15)
        self.assertEqual(self.metrics["gstr2b_rows_total"], 14)
        self.assertEqual(self.metrics["gstr2b_rows_accepted"], 14)
        self.assertEqual(self.metrics["gstr2b_rows_rejected"], 0)

    def test_headline_matching_metrics(self):
        metrics = self.metrics
        self.assertEqual(metrics["expected_matches"], 3)
        self.assertEqual(metrics["actual_matches"], 3)
        self.assertEqual(metrics["headline_cases"], 9)
        self.assertEqual(metrics["true_positives"], 3)
        self.assertEqual(metrics["false_positives"], 0)
        self.assertEqual(metrics["false_negatives"], 0)
        self.assertEqual(metrics["true_negatives"], 6)
        self.assertEqual(metrics["precision"], 1.0)
        self.assertEqual(metrics["recall"], 1.0)
        self.assertEqual(metrics["exact_match_rate"], 1.0)

    def test_exception_metrics(self):
        metrics = self.metrics
        self.assertEqual(metrics["amount_mismatch_expected"], 2)
        self.assertEqual(metrics["amount_mismatch_detected"], 2)
        self.assertEqual(metrics["amount_mismatch_detection_rate"], 1.0)
        self.assertEqual(metrics["invalid_records_expected"], 1)
        self.assertEqual(metrics["invalid_records_excluded"], 1)
        self.assertEqual(metrics["invalid_record_exclusion_rate"], 1.0)
        self.assertEqual(metrics["ambiguous_cases"], 5)
        self.assertEqual(metrics["ambiguous_actual_review"], 5)
        self.assertEqual(metrics["ambiguous_auto_accepted"], 0)

    def test_followup_decisions_and_exclusions(self):
        metrics = self.metrics
        self.assertEqual(metrics["followup_decisions_total"], 15)
        self.assertEqual(metrics["followup_decisions_correct"], 15)
        self.assertEqual(metrics["followup_decision_accuracy"], 1.0)
        self.assertEqual(metrics["clean_followup_invoices_expected"], 3)
        self.assertEqual(metrics["clean_followup_invoices_drafted"], 3)
        self.assertEqual(metrics["draft_groups_generated"], 2)
        self.assertEqual(metrics["clean_draft_groups"], 1)
        self.assertEqual(metrics["needs_review_draft_groups"], 1)
        self.assertEqual(metrics["phone_missing_review_expected"], 1)
        self.assertEqual(metrics["phone_missing_review_actual"], 1)

    def test_duplicate_import_and_repeat_run_behavior(self):
        metrics = self.metrics
        self.assertEqual(metrics["duplicate_import_batches"], 2)
        self.assertTrue(metrics["duplicate_import_batch_ids_distinct"])
        self.assertTrue(metrics["repeated_run_results_identical"])
        self.assertEqual(metrics["batch_run_history_count"], 2)
        self.assertNotEqual(
            metrics["duplicate_batch_run_id"], metrics["first_run_id"]
        )
        self.assertNotEqual(
            metrics["duplicate_batch_run_id"], metrics["repeated_run_id"]
        )

    def test_every_ground_truth_status_is_reported_as_expected(self):
        for row in self.first["per_invoice"]:
            with self.subTest(case=row["key"]):
                self.assertEqual(row["actual"], row["expected"])
                self.assertEqual(
                    row["followup_actual"], row["followup_expected"]
                )

    def test_baseline_is_measured_on_the_same_cases(self):
        metrics = self.metrics
        self.assertEqual(metrics["baseline_true_positives"], 3)
        self.assertEqual(metrics["baseline_false_positives"], 3)
        self.assertEqual(metrics["baseline_false_negatives"], 0)
        self.assertEqual(metrics["baseline_true_negatives"], 3)
        self.assertEqual(metrics["baseline_precision"], 0.5)
        self.assertEqual(metrics["baseline_recall"], 1.0)
        self.assertEqual(metrics["baseline_ambiguous_auto_accepts"], 3)

    def test_no_ocr_or_extraction_claim(self):
        self.assertIsNone(self.metrics["ocr_accuracy"])
        self.assertFalse(self.metrics["extraction_pipeline"])


if __name__ == "__main__":
    unittest.main()
