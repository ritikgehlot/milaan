"""Tests for purchase-invoice CSV/XLSX import and persistence."""

from __future__ import annotations

import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import invoice_import


class InvoiceImportParseTests(unittest.TestCase):
    def test_template_matches_parser_and_passes_validation(self):
        content = invoice_import.template_csv().encode("utf-8")
        parsed = invoice_import.parse_invoice_file(content, "template.csv")
        self.assertEqual(parsed.total_rows, 1)
        self.assertEqual(parsed.accepted_rows, 1)
        self.assertEqual(parsed.rejected_rows, 0)
        self.assertEqual(parsed.rows[0].validation_status, "valid")
        header = parsed.headers
        for column in invoice_import.REQUIRED_COLUMNS:
            self.assertIn(column, header)

    def test_missing_required_column_raises_file_error(self):
        content = (
            "supplier_name,supplier_gstin,invoice_number,invoice_date\n"
            "Acme,27AAACS1234A1Z2,INV-1,2026-03-01\n"
        ).encode()
        with self.assertRaisesRegex(ValueError, "missing required column"):
            invoice_import.parse_invoice_file(content, "bad.csv")

    def test_row_errors_are_reported_without_aborting_file(self):
        header = ",".join(
            invoice_import.REQUIRED_COLUMNS
            + invoice_import.OPTIONAL_COLUMNS
        )
        rows = [
            # bad GSTIN checksum and negative tax
            "Acme,27AAACS1234A1Z9,INV-1,2026-03-01,2026-03,"
            "1000.00,18,90.00,90.00,0.00,-180.00,1180.00,9900000000",
            # non-existent date and unsupported rate
            "Beta,29AAACB5678B1ZP,INV-2,2026-02-30,2026-03,"
            "1000.00,7,90.00,90.00,0.00,180.00,1180.00,9900000001",
            # valid row
            "Gamma,24AAACD9012C1ZB,INV-3,2026-03-03,2026-03,"
            "1000.00,18,90.00,90.00,0.00,180.00,1180.00,9900000002",
            # duplicate natural key of the valid row
            "Gamma,24AAACD9012C1ZB,INV-3,2026-03-03,2026-03,"
            "1000.00,18,90.00,90.00,0.00,180.00,1180.00,9900000002",
        ]
        content = (header + "\n" + "\n".join(rows) + "\n").encode()
        parsed = invoice_import.parse_invoice_file(content, "rows.csv")
        self.assertEqual(parsed.total_rows, 4)
        self.assertEqual(parsed.accepted_rows, 1)
        self.assertEqual(parsed.rejected_rows, 3)
        codes = {
            issue.code
            for row in parsed.rows
            for issue in (*row.errors, *row.warnings)
        }
        self.assertIn("GSTIN_CHECKSUM", codes)
        self.assertIn("NEGATIVE_AMOUNT", codes)
        self.assertIn("INVALID_DATE", codes)
        self.assertIn("UNSUPPORTED_RATE", codes)
        self.assertIn("DUPLICATE_ROW", codes)

    def test_arithmetic_failure_is_rejected(self):
        header = ",".join(
            invoice_import.REQUIRED_COLUMNS
            + invoice_import.OPTIONAL_COLUMNS
        )
        row = (
            "Acme,27AAACS1234A1Z2,INV-1,2026-03-01,2026-03,"
            "1000.00,18,90.00,90.00,0.00,200.00,1200.00,9900000000"
        )
        parsed = invoice_import.parse_invoice_file(
            (header + "\n" + row + "\n").encode(), "arith.csv"
        )
        self.assertEqual(parsed.accepted_rows, 0)
        codes = {issue.code for issue in parsed.rows[0].errors}
        self.assertIn("TAX_TOTAL_MISMATCH", codes)

    def test_xlsx_import_is_supported(self):
        from openpyxl import Workbook

        workbook = Workbook()
        sheet = workbook.active
        sheet.append(
            list(
                invoice_import.REQUIRED_COLUMNS
                + invoice_import.OPTIONAL_COLUMNS
            )
        )
        sheet.append([
            "Acme", "27AAACS1234A1Z2", "INV-X", "2026-03-01",
            "2026-03", 1000.00, 18, 90.00, 90.00, 0.00, 180.00,
            1180.00, "9900000000",
        ])
        buffer = io.BytesIO()
        workbook.save(buffer)
        parsed = invoice_import.parse_invoice_file(
            buffer.getvalue(), "invoices.xlsx"
        )
        self.assertEqual(parsed.source_format, "xlsx")
        self.assertEqual(parsed.accepted_rows, 1)

    def test_unsupported_extension_raises(self):
        with self.assertRaisesRegex(ValueError, "unsupported file type"):
            invoice_import.parse_invoice_file(b"data", "invoices.txt")

    def test_blank_file_raises(self):
        header = ",".join(
            invoice_import.REQUIRED_COLUMNS
            + invoice_import.OPTIONAL_COLUMNS
        )
        with self.assertRaisesRegex(ValueError, "no data rows"):
            invoice_import.parse_invoice_file(
                (header + "\n").encode(), "empty.csv"
            )


class InvoiceImportSaveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="milaan-test-")
        self._old_env = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(Path(self._tmp) / "test.db")
        import db

        db.init_db()
        self.db = db

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._old_env
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_save_creates_supplier_and_validated_invoice(self):
        parsed = invoice_import.parse_invoice_file(
            invoice_import.template_csv().encode(), "template.csv"
        )
        result = self.db.save_invoice_import(parsed)
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["suppliers_created"], 1)
        self.assertEqual(result["duplicates"], 0)
        self.assertEqual(self.db.get_validation_summary()["valid"], 1)
        invoice = self.db.get_invoices()[0]
        self.assertEqual(invoice["Validation"], "valid")
        self.assertEqual(invoice["Source"], "upload")
        self.assertEqual(invoice["Supplier"], "(Synthetic) Template Supplier")

    def test_second_import_reports_duplicate_without_writing(self):
        content = invoice_import.template_csv().encode()
        self.db.save_invoice_import(
            invoice_import.parse_invoice_file(content, "a.csv")
        )
        result = self.db.save_invoice_import(
            invoice_import.parse_invoice_file(content, "b.csv")
        )
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["duplicates"], 1)
        self.assertEqual(self.db.get_totals()["invoice_count"], 1)

    def test_rejected_rows_are_not_persisted(self):
        header = ",".join(
            invoice_import.REQUIRED_COLUMNS
            + invoice_import.OPTIONAL_COLUMNS
        )
        row = (
            "Acme,27AAACS1234A1Z9,INV-1,2026-03-01,2026-03,"
            "1000.00,18,90.00,90.00,0.00,180.00,1180.00,9900000000"
        )
        parsed = invoice_import.parse_invoice_file(
            (header + "\n" + row + "\n").encode(), "bad.csv"
        )
        result = self.db.save_invoice_import(parsed)
        self.assertEqual(result["imported"], 0)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(self.db.get_totals()["invoice_count"], 0)


if __name__ == "__main__":
    unittest.main()
