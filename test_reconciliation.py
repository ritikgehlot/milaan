"""Tests for deterministic reconciliation (Task 5).

Run: .venv/bin/python -m unittest test_reconciliation -v

All GSTINs, invoices and GSTR-2B rows are synthetic.
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import gstr2b  # noqa: E402
import reconciliation  # noqa: E402

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
    gstin=GSTIN_A,
    invoice_number="INV-1",
    tax_period=PERIOD,
    total_gst_paise=225_000,
    taxable_paise=1_250_000,
):
    cgst = total_gst_paise // 2
    sgst = total_gst_paise - cgst
    return [
        gstin,
        invoice_number,
        "2026-03-05",
        tax_period,
        rupees(taxable_paise),
        rupees(cgst),
        rupees(sgst),
        "0.00",
        rupees(total_gst_paise),
    ]


def invoice_record(
    invoice_id=1,
    gstin=GSTIN_A,
    number="INV-1",
    period=PERIOD,
    total_gst=225_000,
    status="valid",
):
    return reconciliation.InvoiceRecord(
        invoice_id=invoice_id,
        supplier_gstin=gstin,
        invoice_number=number,
        tax_period=period,
        total_gst=total_gst,
        validation_status=status,
    )


def row_record(
    row_id=1,
    gstin=GSTIN_A,
    number="INV-1",
    period=PERIOD,
    total_gst=225_000,
    status="valid",
):
    return reconciliation.Gstr2bRecord(
        row_id=row_id,
        supplier_gstin=gstin,
        invoice_number=number,
        tax_period=period,
        total_gst=total_gst,
        validation_status=status,
    )


class NormalizationTests(unittest.TestCase):
    def test_invoice_number_case_and_whitespace(self):
        self.assertEqual(
            reconciliation.normalize_invoice_number("  inv  001 "),
            "INV 001",
        )
        self.assertEqual(
            reconciliation.normalize_invoice_number("inv-001"),
            reconciliation.normalize_invoice_number(" INV-001 "),
        )

    def test_invoice_number_punctuation_is_not_removed(self):
        self.assertNotEqual(
            reconciliation.normalize_invoice_number("INV-001"),
            reconciliation.normalize_invoice_number("INV001"),
        )
        self.assertIsNone(reconciliation.normalize_invoice_number("   "))

    def test_tax_period_normalization(self):
        self.assertEqual(
            reconciliation.normalize_tax_period(" 2026-03 "), "2026-03"
        )
        self.assertEqual(
            reconciliation.normalize_tax_period("2026/03"), "2026-03"
        )
        for bad in ("2026-13", "2026-00", "2026-3", "26-03", "", None):
            with self.subTest(value=bad):
                self.assertIsNone(reconciliation.normalize_tax_period(bad))

    def test_gstin_normalization(self):
        self.assertEqual(
            reconciliation.normalize_gstin(f" {GSTIN_A.lower()} "), GSTIN_A
        )
        self.assertIsNone(reconciliation.normalize_gstin("   "))

    def test_tolerance_must_be_non_negative_integer(self):
        for bad in (-1, True, "10", 1.5, None):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    reconciliation.ReconciliationConfig(
                        amount_tolerance_paise=bad
                    )
        self.assertEqual(
            reconciliation.ReconciliationConfig().amount_tolerance_paise, 0
        )


class PureMatchingTests(unittest.TestCase):
    def match(self, invoice, rows, tolerance=0):
        config = reconciliation.ReconciliationConfig(
            amount_tolerance_paise=tolerance
        )
        results = reconciliation.reconcile_invoices(
            [invoice], rows, config=config
        )
        self.assertEqual(len(results), 1)
        return results[0]

    def test_exact_match(self):
        result = self.match(invoice_record(), [row_record()])
        self.assertEqual(result.match_status, "MATCHED")
        self.assertEqual(result.match_method, "exact")
        self.assertEqual(result.amount_difference_paise, 0)
        self.assertEqual(result.gstr2b_row_id, 1)
        self.assertEqual(result.reason_codes, ())

    def test_invoice_number_case_and_whitespace_match(self):
        result = self.match(
            invoice_record(number="  inv-1 "),
            [row_record(number="INV-1")],
        )
        self.assertEqual(result.match_status, "MATCHED")

    def test_different_tax_period_never_matches(self):
        result = self.match(
            invoice_record(period="2026-04"),
            [row_record(period="2026-03")],
        )
        self.assertEqual(result.match_status, "MISSING_FROM_2B")
        self.assertIsNone(result.gstr2b_row_id)
        self.assertIsNone(result.amount_difference_paise)

    def test_gstin_mismatch_never_matches(self):
        result = self.match(invoice_record(), [row_record(gstin=GSTIN_B)])
        self.assertEqual(result.match_status, "MISSING_FROM_2B")

    def test_amount_difference_default_zero_tolerance(self):
        result = self.match(
            invoice_record(total_gst=225_001),
            [row_record(total_gst=225_000)],
        )
        self.assertEqual(result.match_status, "AMOUNT_MISMATCH")
        self.assertEqual(result.amount_difference_paise, 1)
        self.assertEqual(result.gstr2b_row_id, 1)
        self.assertIn(
            "AMOUNT_DIFFERENCE_BEYOND_TOLERANCE", result.reason_codes
        )

    def test_amount_difference_within_tolerance_is_transparent(self):
        result = self.match(
            invoice_record(total_gst=225_100),
            [row_record(total_gst=225_000)],
            tolerance=100,
        )
        self.assertEqual(result.match_status, "MATCHED")
        self.assertEqual(result.match_method, "exact_tolerance")
        self.assertEqual(result.amount_difference_paise, 100)
        self.assertIn("AMOUNT_WITHIN_TOLERANCE", result.reason_codes)

    def test_amount_difference_beyond_tolerance_boundary(self):
        result = self.match(
            invoice_record(total_gst=225_101),
            [row_record(total_gst=225_000)],
            tolerance=100,
        )
        self.assertEqual(result.match_status, "AMOUNT_MISMATCH")
        self.assertEqual(result.amount_difference_paise, 101)

    def test_invalid_and_needs_review_invoices_are_not_eligible(self):
        for status, code in (
            ("invalid", "INVOICE_INVALID"),
            ("needs_review", "INVOICE_NEEDS_REVIEW"),
            ("pending", "INVOICE_PENDING"),
        ):
            with self.subTest(status=status):
                result = self.match(
                    invoice_record(status=status), [row_record()]
                )
                self.assertEqual(result.match_status, "NOT_ELIGIBLE")
                self.assertEqual(result.match_method, "ineligible")
                self.assertIsNone(result.gstr2b_row_id)
                self.assertIn(code, result.reason_codes)

    def test_duplicate_gstr2b_candidates_are_not_selected(self):
        result = self.match(
            invoice_record(),
            [
                row_record(row_id=1, number="INV-1"),
                row_record(row_id=2, number="inv-1"),
            ],
        )
        self.assertEqual(result.match_status, "NEEDS_REVIEW")
        self.assertIn("DUPLICATE_GSTR2B_CANDIDATES", result.reason_codes)
        self.assertIsNone(result.gstr2b_row_id)

    def test_ambiguous_non_exact_candidates_are_not_selected(self):
        result = self.match(
            invoice_record(number="INV-99"),
            [
                row_record(row_id=1, number="INV-1"),
                row_record(row_id=2, number="INV-2"),
            ],
        )
        self.assertEqual(result.match_status, "NEEDS_REVIEW")
        self.assertIn("AMBIGUOUS_CANDIDATES", result.reason_codes)
        self.assertIsNone(result.gstr2b_row_id)

    def test_single_non_exact_candidate_needs_review(self):
        result = self.match(
            invoice_record(number="INV-99"),
            [row_record(number="INV-1")],
        )
        self.assertEqual(result.match_status, "NEEDS_REVIEW")
        self.assertEqual(result.match_method, "candidate_amount")
        self.assertEqual(result.gstr2b_row_id, 1)
        self.assertIn(
            "CANDIDATE_NOT_EXACT_INVOICE_NUMBER", result.reason_codes
        )

    def test_duplicate_purchase_invoices_need_review(self):
        results = reconciliation.reconcile_invoices(
            [
                invoice_record(invoice_id=1),
                invoice_record(invoice_id=2),
            ],
            [row_record()],
        )
        self.assertEqual(len(results), 2)
        for result in results:
            self.assertEqual(result.match_status, "NEEDS_REVIEW")
            self.assertIn("DUPLICATE_PURCHASE_INVOICE", result.reason_codes)
            self.assertIsNone(result.gstr2b_row_id)

    def test_rejected_gstr2b_rows_are_ignored(self):
        result = self.match(
            invoice_record(),
            [row_record(status="invalid"), row_record(row_id=2, status="needs_review")],
        )
        self.assertEqual(result.match_status, "MISSING_FROM_2B")

    def test_missing_row_has_no_evidence(self):
        result = self.match(invoice_record(), [])
        self.assertEqual(result.match_status, "MISSING_FROM_2B")
        self.assertIsNone(result.gstr2b_row_id)
        self.assertIsNone(result.gstr2b_total_gst_paise)
        self.assertIn("NO_2B_ROW_FOUND", result.reason_codes)

    def test_results_are_ordered_by_invoice_id(self):
        results = reconciliation.reconcile_invoices(
            [invoice_record(invoice_id=3), invoice_record(invoice_id=1, number="X")],
            [row_record()],
        )
        self.assertEqual([result.invoice_id for result in results], [1, 3])


class DatabaseTestCase(unittest.TestCase):
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

    def add_supplier(self, name="Synthetic Traders", gstin=GSTIN_A):
        with db.db() as conn:
            cursor = conn.execute(
                "INSERT INTO suppliers (supplier_name, gstin) VALUES (?, ?)",
                (name, gstin),
            )
            return cursor.lastrowid

    def add_invoice(
        self,
        supplier_id,
        number="INV-1",
        period=PERIOD,
        total_gst=225_000,
        status="valid",
    ):
        taxable = 1_250_000
        with db.db() as conn:
            cursor = conn.execute(
                """INSERT INTO invoices
                       (supplier_id, invoice_number, invoice_date, tax_period,
                        taxable_value, gst_rate, cgst, sgst, igst, total_gst,
                        invoice_total, validation_status)
                   VALUES (?, ?, '2026-03-05', ?, ?, 18, ?, 0, 0, ?, ?, ?)""",
                (
                    supplier_id,
                    number,
                    period,
                    taxable,
                    total_gst,
                    total_gst,
                    taxable + total_gst,
                    status,
                ),
            )
            return cursor.lastrowid

    def import_rows(self, rows, name="gstr2b.csv"):
        parsed = gstr2b.parse_gstr2b(csv_bytes(rows), name)
        return db.save_gstr2b_import(parsed)["import_id"]

    def snapshot(self):
        conn = db.get_connection()
        try:
            invoices = [
                tuple(row)
                for row in conn.execute("SELECT * FROM invoices ORDER BY id")
            ]
            rows = [
                tuple(row)
                for row in conn.execute(
                    "SELECT * FROM gstr2b_rows ORDER BY id"
                )
            ]
        finally:
            conn.close()
        return invoices, rows


class PersistenceTests(DatabaseTestCase):
    def test_run_and_results_are_persisted_with_evidence(self):
        supplier_id = self.add_supplier()
        invoice_id = self.add_invoice(supplier_id)
        batch_id = self.import_rows([gstr2b_row(invoice_number=" inv-1 ")])

        summary = db.reconcile_gstr2b_import(batch_id)
        self.assertEqual(summary["matched_count"], 1)
        self.assertEqual(summary["run_id"], 1)

        run = db.get_reconciliation_run(summary["run_id"])
        self.assertEqual(run["is_latest"], 1)
        self.assertEqual(run["invoice_count"], 1)
        self.assertEqual(run["amount_tolerance_paise"], 0)

        results = db.get_reconciliation_results(summary["run_id"])
        self.assertEqual(len(results), 1)
        evidence = results[0]
        self.assertEqual(evidence["Invoice #"], invoice_id)
        self.assertEqual(evidence["Status"], "MATCHED")
        self.assertEqual(evidence["Method"], "exact")
        self.assertEqual(evidence["Difference (Rs)"], "0.00")
        self.assertTrue(evidence["2B row"])

    def test_difference_and_totals_are_persisted_in_paise(self):
        supplier_id = self.add_supplier()
        self.add_invoice(supplier_id, total_gst=225_050)
        batch_id = self.import_rows([gstr2b_row(total_gst_paise=225_000)])

        summary = db.reconcile_gstr2b_import(batch_id, amount_tolerance_paise=10)
        self.assertEqual(summary["amount_mismatch_count"], 1)

        detail = db.get_reconciliation_summary(summary["run_id"])
        self.assertEqual(
            detail["by_status"]["AMOUNT_MISMATCH"]["count"], 1
        )
        self.assertEqual(
            detail["by_status"]["AMOUNT_MISMATCH"]["total_gst_paise"], 225_050
        )
        results = db.get_reconciliation_results(summary["run_id"])
        self.assertEqual(results[0]["Difference (Rs)"], "+0.50")

    def test_rerun_preserves_history_and_moves_latest_flag(self):
        supplier_id = self.add_supplier()
        self.add_invoice(supplier_id)
        batch_id = self.import_rows([gstr2b_row()])

        first = db.reconcile_gstr2b_import(batch_id)
        second = db.reconcile_gstr2b_import(batch_id)

        runs = db.get_reconciliation_runs()
        self.assertEqual(len(runs), 2)
        self.assertEqual(runs[0]["Run"], second["run_id"])
        self.assertTrue(runs[0]["Latest"])
        self.assertFalse(runs[1]["Latest"])

        self.assertEqual(
            len(db.get_reconciliation_results(first["run_id"])), 1
        )
        self.assertEqual(
            len(db.get_reconciliation_results(second["run_id"])), 1
        )

    def test_failed_run_rolls_back_run_and_results(self):
        supplier_id = self.add_supplier()
        self.add_invoice(supplier_id, number="INV-1")
        self.add_invoice(supplier_id, number="INV-2")
        batch_id = self.import_rows(
            [
                gstr2b_row(invoice_number="INV-1"),
                gstr2b_row(invoice_number="INV-2"),
            ]
        )
        first = db.reconcile_gstr2b_import(batch_id)

        calls = {"count": 0}
        original = db._insert_reconciliation_result

        def flaky(conn, run_id, result):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("simulated insert failure")
            original(conn, run_id, result)

        with mock.patch.object(
            db, "_insert_reconciliation_result", side_effect=flaky
        ):
            with self.assertRaises(RuntimeError):
                db.reconcile_gstr2b_import(batch_id)

        runs = db.get_reconciliation_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["Run"], first["run_id"])
        self.assertTrue(runs[0]["Latest"])
        with db.db() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM reconciliation_results"
            ).fetchone()[0]
        self.assertEqual(total, 2)

    def test_source_records_are_not_mutated(self):
        supplier_id = self.add_supplier()
        self.add_invoice(supplier_id, number="INV-1")
        self.add_invoice(supplier_id, number="INV-2", status="invalid")
        batch_id = self.import_rows(
            [
                gstr2b_row(invoice_number="INV-1"),
                gstr2b_row(invoice_number="INV-2"),
            ]
        )
        before = self.snapshot()
        db.reconcile_gstr2b_import(batch_id)
        self.assertEqual(self.snapshot(), before)

    def test_no_invoices_is_refused_without_creating_a_run(self):
        batch_id = self.import_rows([gstr2b_row()])
        with self.assertRaises(ValueError):
            db.reconcile_gstr2b_import(batch_id)
        self.assertEqual(db.get_reconciliation_runs(), [])

    def test_batch_with_no_valid_rows_is_refused(self):
        supplier_id = self.add_supplier()
        self.add_invoice(supplier_id)
        batch_id = self.import_rows(
            [gstr2b_row(gstin="not-a-gstin", invoice_number="BROKEN")]
        )
        with self.assertRaises(ValueError):
            db.reconcile_gstr2b_import(batch_id)
        self.assertEqual(db.get_reconciliation_runs(), [])

    def test_all_ineligible_invoices_is_refused(self):
        supplier_id = self.add_supplier()
        self.add_invoice(supplier_id, status="invalid")
        batch_id = self.import_rows([gstr2b_row()])
        with self.assertRaises(ValueError):
            db.reconcile_gstr2b_import(batch_id)
        self.assertEqual(db.get_reconciliation_runs(), [])

    def test_missing_batch_is_refused(self):
        with self.assertRaises(ValueError):
            db.reconcile_gstr2b_import(9999)

    def test_duplicate_purchase_invoices_are_flagged_end_to_end(self):
        supplier_lower = self.add_supplier(
            name="Lowercase GSTIN", gstin=GSTIN_A.lower()
        )
        supplier_upper = self.add_supplier(
            name="Uppercase GSTIN", gstin=GSTIN_A
        )
        self.add_invoice(supplier_lower, number="INV-DUP")
        self.add_invoice(supplier_upper, number="INV-DUP")
        batch_id = self.import_rows([gstr2b_row(invoice_number="OTHER")])

        summary = db.reconcile_gstr2b_import(batch_id)
        self.assertEqual(summary["needs_review_count"], 2)
        results = db.get_reconciliation_results(summary["run_id"])
        self.assertEqual(
            [row["Status"] for row in results], ["NEEDS_REVIEW", "NEEDS_REVIEW"]
        )
        for row in results:
            self.assertFalse(row["2B row"])
            self.assertIn("DUPLICATE_PURCHASE_INVOICE", row["Reasons"])

    def test_foreign_keys_and_status_checks(self):
        supplier_id = self.add_supplier()
        self.add_invoice(supplier_id)
        batch_id = self.import_rows([gstr2b_row()])
        summary = db.reconcile_gstr2b_import(batch_id)
        row_id = None
        with db.db() as conn:
            row_id = conn.execute(
                "SELECT id FROM gstr2b_rows WHERE import_id = ?", (batch_id,)
            ).fetchone()[0]

        conn = db.get_connection()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO reconciliation_results
                           (run_id, invoice_id, match_status, match_method,
                            invoice_number, invoice_total_gst_paise)
                       VALUES (99999, 1, 'MISSING_FROM_2B', 'none', 'X', 0)"""
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO reconciliation_results
                           (run_id, invoice_id, gstr2b_row_id, match_status,
                            match_method, invoice_number,
                            invoice_total_gst_paise)
                       VALUES (?, 1, 99999, 'MATCHED', 'exact', 'X', 0)""",
                    (summary["run_id"],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO reconciliation_results
                           (run_id, invoice_id, gstr2b_row_id, match_status,
                            match_method, invoice_number,
                            invoice_total_gst_paise)
                       VALUES (?, 99999, ?, 'MATCHED', 'exact', 'X', 0)""",
                    (summary["run_id"], row_id),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO reconciliation_results
                           (run_id, invoice_id, match_status, match_method,
                            invoice_number, invoice_total_gst_paise)
                       VALUES (?, 1, 'NOT_A_STATUS', 'none', 'X', 0)""",
                    (summary["run_id"],),
                )
        finally:
            conn.close()

    def test_ui_reconcile_button_reports_a_real_run(self):
        from streamlit.testing.v1 import AppTest

        supplier_id = self.add_supplier()
        self.add_invoice(supplier_id)
        self.import_rows([gstr2b_row()])

        at = AppTest.from_file(
            str(Path(__file__).parent / "app.py"), default_timeout=30
        )
        at.run()
        self.assertEqual(at.exception, [])
        at.switch_page("pages/4_reconciliation.py").run()
        self.assertEqual(at.exception, [])
        at.button(key="reconcile").click().run()
        self.assertEqual(at.exception, [])
        self.assertTrue(
            any("Run #" in message.value for message in at.success),
            [message.value for message in at.success],
        )
        metrics = {metric.label: str(metric.value) for metric in at.metric}
        self.assertEqual(metrics.get("Matched"), "1 · Rs 2250.00")
