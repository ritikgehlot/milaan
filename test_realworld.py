"""Real-world-style compatibility checks and Streamlit freshness checks.

Run: .venv/bin/python -m unittest test_realworld -v

The fixtures are synthetic normalized GSTR-2B files. They are not GST
portal exports, and no real taxpayer data or credentials are used.
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

FIXTURES = Path(__file__).parent / "fixtures"
GSTIN_A = "27AAACS1234A1Z2"
GSTIN_B = "29AAACB5678B1ZP"
PERIOD = "2026-03"


class FixtureParsingTests(unittest.TestCase):
    def parse(self, name):
        return gstr2b.parse_gstr2b((FIXTURES / name).read_bytes(), name)

    def test_valid_normalized_csv_fixture(self):
        parsed = self.parse("valid_normalized.csv")
        self.assertEqual(parsed.source_format, "csv")
        self.assertEqual(
            (parsed.total_rows, parsed.accepted_rows, parsed.rejected_rows),
            (2, 2, 0),
        )
        first, second = parsed.rows
        self.assertEqual(first.supplier_gstin, GSTIN_A)
        self.assertEqual(first.invoice_date, "2026-03-05")
        self.assertEqual(first.taxable_value, 1_250_000)
        self.assertEqual(first.total_gst, 225_000)
        self.assertEqual(second.cgst, 0)
        self.assertEqual(second.igst, 90_000)

    def test_valid_normalized_xlsx_fixture(self):
        parsed = self.parse("valid_normalized.xlsx")
        self.assertEqual(parsed.source_format, "xlsx")
        self.assertEqual(parsed.accepted_rows, 2)
        self.assertEqual(parsed.rows[0].invoice_date, "2026-03-05")
        self.assertEqual(parsed.rows[0].taxable_value, 1_250_000)
        self.assertEqual(parsed.rows[1].invoice_date, "2026-03-10")
        self.assertEqual(parsed.rows[1].total_gst, 90_000)

    def test_xlsx_reads_the_active_worksheet(self):
        parsed = self.parse("active_sheet.xlsx")
        self.assertEqual(parsed.accepted_rows, 1)
        self.assertEqual(parsed.rows[0].invoice_number, "INV-FIX-001")

    def test_bom_and_crlf_fixture(self):
        content = (FIXTURES / "bom_crlf.csv").read_bytes()
        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"\r\n", content)
        parsed = gstr2b.parse_gstr2b(content, "bom_crlf.csv")
        self.assertEqual(parsed.accepted_rows, 2)

    def test_extra_columns_fixture(self):
        parsed = self.parse("extra_columns.csv")
        self.assertEqual(parsed.accepted_rows, 1)
        self.assertIn("remarks", parsed.rows[0].raw_data)
        self.assertIn("synthetic note", parsed.rows[0].raw_data)

    def test_invalid_rows_fixture(self):
        parsed = self.parse("invalid_rows.csv")
        self.assertEqual(
            (parsed.total_rows, parsed.accepted_rows, parsed.rejected_rows),
            (7, 1, 6),
        )
        by_number = {
            row.invoice_number: {issue.code for issue in row.errors}
            for row in parsed.rows
        }
        self.assertIn("INVALID_DATE", by_number["INV-FIX-BAD-DATE"])
        self.assertIn("GSTIN_FORMAT", by_number["INV-FIX-BAD-GSTIN"])
        self.assertIn(
            "INVALID_TAX_PERIOD", by_number["INV-FIX-BAD-PERIOD"]
        )
        self.assertIn("INVALID_MONEY", by_number["INV-FIX-BAD-MONEY"])
        self.assertIn("NEGATIVE_AMOUNT", by_number["INV-FIX-NEGATIVE"])
        self.assertIn("TAX_TOTAL_MISMATCH", by_number["INV-FIX-BAD-TOTAL"])

    def test_duplicate_rows_fixture(self):
        parsed = self.parse("duplicate_rows.csv")
        self.assertEqual(parsed.accepted_rows, 1)
        self.assertEqual(parsed.rejected_rows, 1)
        self.assertIn(
            "DUPLICATE_ROW",
            {issue.code for issue in parsed.rows[1].errors},
        )

    def test_decimal_values_fixture(self):
        parsed = self.parse("decimal_values.csv")
        self.assertEqual(parsed.accepted_rows, 2)
        self.assertEqual(parsed.rows[0].taxable_value, 29)
        self.assertEqual(parsed.rows[1].taxable_value, 1_250_050)
        self.assertEqual(parsed.rows[1].igst, 225_009)
        self.assertEqual(parsed.rows[1].total_gst, 225_009)

    def test_empty_and_header_only_fixtures_fail_safely(self):
        for name, message in (
            ("empty.csv", "file is empty"),
            ("header_only.csv", "header but no data rows"),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as caught:
                    self.parse(name)
                self.assertIn(message, str(caught.exception))

    def test_malformed_files_fail_safely(self):
        for name, message in (
            ("malformed.xlsx", "not a readable XLSX workbook"),
            ("malformed_utf8.csv", "not valid UTF-8 text"),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError) as caught:
                    self.parse(name)
                self.assertIn(message, str(caught.exception))

    def test_missing_required_column_is_rejected(self):
        header = (
            "supplier_gstin,invoice_number,invoice_date,tax_period,"
            "taxable_value,cgst,sgst,igst"
        )
        content = (
            header + "\n"
            + f"{GSTIN_A},INV-1,2026-03-05,2026-03,1.00,0,0,0\n"
        ).encode()
        with self.assertRaises(ValueError) as caught:
            gstr2b.parse_gstr2b(content, "missing.csv")
        self.assertIn("total_gst", str(caught.exception))


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="milaan-realworld-")
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

    def add_invoice(self, supplier_id, number, taxable, total_gst):
        cgst = total_gst // 2
        sgst = total_gst - cgst
        with db.db() as conn:
            return conn.execute(
                """INSERT INTO invoices
                       (supplier_id, invoice_number, invoice_date, tax_period,
                        taxable_value, gst_rate, cgst, sgst, igst, total_gst,
                        invoice_total, source)
                   VALUES (?, ?, '2026-03-05', ?, ?, 18, ?, ?, 0, ?, ?,
                           'realworld-test')""",
                (
                    supplier_id,
                    number,
                    PERIOD,
                    taxable,
                    cgst,
                    sgst,
                    total_gst,
                    taxable + total_gst,
                ),
            ).lastrowid


class StreamlitFreshnessTests(DatabaseTestCase):
    def test_import_and_reconcile_refresh_visible_totals(self):
        from streamlit.testing.v1 import AppTest

        supplier = self.add_supplier("Synthetic Fixture Traders", GSTIN_A)
        invoice_id = self.add_invoice(
            supplier, "INV-FIX-001", 1_250_000, 225_000
        )
        self.assertEqual(db.revalidate_invoice(invoice_id).status, "valid")
        self.assertEqual(db.get_gstr2b_imports(), [])
        self.assertEqual(db.get_reconciliation_runs(), [])

        at = AppTest.from_file(
            str(Path(__file__).parent / "app.py"), default_timeout=30
        )
        at.run()
        self.assertEqual(at.exception, [])
        at.switch_page("pages/3_gstr2b_import.py").run()
        self.assertEqual(at.exception, [])

        content = (FIXTURES / "valid_normalized.csv").read_bytes()
        uploader = next(iter(at.file_uploader))
        uploader.set_value(
            ("valid_normalized.csv", content, "text/csv")
        ).run()
        self.assertEqual(at.exception, [])
        at.button(key="gstr2b_import").click().run()
        self.assertEqual(at.exception, [])
        self.assertEqual(len(db.get_gstr2b_imports()), 1)
        self.assertTrue(
            any("Batch #1 imported" in item.value for item in at.success),
            [item.value for item in at.success],
        )

        at.switch_page("pages/4_reconciliation.py").run()
        self.assertEqual(at.exception, [])
        at.button(key="reconcile").click().run()
        self.assertEqual(at.exception, [])
        runs = db.get_reconciliation_runs()
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]["Matched"], 1)
        self.assertEqual(runs[0]["Missing from 2B"], 0)
        metrics = {item.label: str(item.value) for item in at.metric}
        self.assertIn("Matched", metrics)
        self.assertTrue(metrics["Matched"].startswith("1 "))
        self.assertNotIn(
            "No reconciliation runs yet.",
            [item.value for item in at.info],
        )
