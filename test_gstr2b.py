"""Tests for deterministic GSTR-2B ingestion (Task 4).

Run: .venv/bin/python -m unittest test_gstr2b -v
"""

import io
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import gstr2b  # noqa: E402

_CSV_HEADER = (
    "supplier_gstin,invoice_number,invoice_date,tax_period,"
    "taxable_value,cgst,sgst,igst,total_gst"
)


def reference_checksum(first_14: str) -> str:
    charset = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    total = 0
    for position, character in enumerate(first_14):
        value = charset.index(character.upper())
        product = value * (1 if position % 2 == 0 else 2)
        total += product // 36 + product % 36
    return charset[(36 - total % 36) % 36]


GSTIN_INTRA = "27AAACS1234A1Z" + reference_checksum("27AAACS1234A1Z")
GSTIN_INTER = "29AAACB5678B1Z" + reference_checksum("29AAACB5678B1Z")
SEED_PLACEHOLDER_GSTIN = "27FAKE1000F1Z5"


def row(
    gstin=GSTIN_INTRA,
    invoice_number="INV-G2B-1",
    invoice_date="2026-03-05",
    tax_period="2026-03",
    taxable="12500.00",
    cgst="1125.00",
    sgst="1125.00",
    igst="0.00",
    total_gst="2250.00",
):
    return [
        gstin, invoice_number, invoice_date, tax_period,
        taxable, cgst, sgst, igst, total_gst,
    ]


def csv_bytes(rows, header=_CSV_HEADER) -> bytes:
    lines = [header]
    for values in rows:
        cells = []
        for value in values:
            text = str(value)
            if "," in text:
                text = '"' + text.replace('"', '""') + '"'
            cells.append(text)
        lines.append(",".join(cells))
    return ("\n".join(lines) + "\n").encode("utf-8")


def xlsx_bytes(header, rows) -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(header)
    for values in rows:
        sheet.append(values)
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


class MoneyParsingTests(unittest.TestCase):
    def test_exact_decimal_conversion(self):
        self.assertEqual(gstr2b.parse_money_to_paise("12500.50"), 1_250_050)
        self.assertEqual(gstr2b.parse_money_to_paise(" 0.29 "), 29)
        self.assertEqual(gstr2b.parse_money_to_paise(Decimal("0.10")), 10)
        self.assertEqual(gstr2b.parse_money_to_paise(1250.5), 125_050)
        self.assertEqual(gstr2b.parse_money_to_paise(5), 500)
        self.assertEqual(gstr2b.parse_money_to_paise(0), 0)
        self.assertEqual(gstr2b.parse_money_to_paise("0"), 0)

    def test_rejects_booleans_and_non_finite(self):
        for bad in (True, False, float("nan"), float("inf"),
                    Decimal("NaN"), Decimal("Infinity")):
            with self.subTest(value=bad):
                with self.assertRaises(gstr2b.MoneyParseError):
                    gstr2b.parse_money_to_paise(bad)

    def test_rejects_malformed_and_lossy_values(self):
        for bad in ("abc", "", "  ", "1,250.00", "1e5", "0x10",
                    "1.005", "12.", ".5"):
            with self.subTest(value=bad):
                with self.assertRaises(gstr2b.MoneyParseError):
                    gstr2b.parse_money_to_paise(bad)

    def test_rejects_negative(self):
        for bad in ("-1", "-0.01", -1, Decimal("-2")):
            with self.subTest(value=bad):
                with self.assertRaises(gstr2b.MoneyParseError) as caught:
                    gstr2b.parse_money_to_paise(bad)
                self.assertEqual(caught.exception.code, "NEGATIVE_AMOUNT")

    def test_rejects_unsupported_types(self):
        with self.assertRaises(gstr2b.MoneyParseError):
            gstr2b.parse_money_to_paise(None)
        with self.assertRaises(gstr2b.MoneyParseError):
            gstr2b.parse_money_to_paise(["1"])


class CsvParsingTests(unittest.TestCase):
    def test_valid_csv_two_rows(self):
        content = csv_bytes([
            row(),
            row(gstin=GSTIN_INTER, invoice_number="INV-G2B-2",
                taxable="5000.00", cgst="0.00", sgst="0.00",
                igst="900.00", total_gst="900.00"),
        ])
        parsed = gstr2b.parse_gstr2b(content, "gstr2b.csv")
        self.assertEqual(parsed.source_format, "csv")
        self.assertEqual(parsed.file_size, len(content))
        self.assertEqual(parsed.file_hash, gstr2b.sha256_hex(content))
        self.assertEqual((parsed.total_rows, parsed.accepted_rows,
                          parsed.rejected_rows), (2, 2, 0))
        first = parsed.rows[0]
        self.assertEqual(first.validation_status, "valid")
        self.assertEqual(first.row_number, 1)
        self.assertEqual(first.taxable_value, 1_250_000)
        self.assertEqual(first.cgst, 112_500)
        self.assertEqual(first.total_gst, 225_000)

    def test_header_case_and_space_normalization(self):
        header = (
            "Supplier GSTIN,Invoice Number,Invoice Date,Tax Period,"
            "Taxable Value,CGST,SGST,IGST,Total GST"
        )
        parsed = gstr2b.parse_gstr2b(csv_bytes([row()], header=header),
                                     "normalized.csv")
        self.assertEqual(parsed.accepted_rows, 1)

    def test_common_export_header_aliases_are_accepted(self):
        header = (
            "GSTIN of supplier,Invoice No,Invoice Dt,Return Period,"
            "Taxable Amount,CGST Amount,SGST Amount,IGST Amount,Total Tax"
        )
        parsed = gstr2b.parse_gstr2b(
            csv_bytes([row()], header=header), "common-export.csv"
        )
        self.assertEqual(parsed.accepted_rows, 1)
        self.assertEqual(parsed.rows[0].invoice_number, "INV-G2B-1")

    def test_canonical_and_alias_duplicates_are_rejected(self):
        header = _CSV_HEADER + ",Invoice No"
        with self.assertRaisesRegex(ValueError, "duplicate column"):
            gstr2b.parse_gstr2b(
                csv_bytes([row() + ["INV-G2B-1"]], header=header),
                "duplicate-alias.csv",
            )

    def test_bom_and_crlf_are_accepted(self):
        plain = csv_bytes([row()])
        content = b"\xef\xbb\xbf" + plain.replace(b"\n", b"\r\n")
        parsed = gstr2b.parse_gstr2b(content, "bom.csv")
        self.assertEqual(parsed.accepted_rows, 1)

    def test_extra_columns_are_ignored_but_audited(self):
        content = csv_bytes(
            [row() + ["synthetic remark"]],
            header=_CSV_HEADER + ",remarks",
        )
        parsed = gstr2b.parse_gstr2b(content, "extra.csv")
        self.assertEqual(parsed.accepted_rows, 1)
        self.assertIn("synthetic remark", parsed.rows[0].raw_data)
        self.assertIn("remarks", parsed.rows[0].raw_data)

    def test_blank_rows_are_skipped(self):
        lines = [
            _CSV_HEADER,
            ",".join(row()),
            ",,,,,,,,",
            ",".join(row(invoice_number="INV-G2B-2")),
        ]
        parsed = gstr2b.parse_gstr2b(
            ("\n".join(lines) + "\n").encode(), "blanks.csv"
        )
        self.assertEqual(parsed.total_rows, 2)
        self.assertEqual([r.row_number for r in parsed.rows], [1, 2])

    def test_bad_rows_are_rejected_but_batch_continues(self):
        bad_rows = [
            row(invoice_number="INV-BAD-MONEY", taxable="abc"),
            row(invoice_number="INV-NEGATIVE", taxable="-5.00"),
            row(invoice_number=""),
            row(invoice_number="INV-BAD-DATE", invoice_date="2026-02-30"),
            row(invoice_number="INV-BAD-PERIOD", tax_period="2026-13"),
            row(invoice_number="INV-FAKE-GSTIN",
                gstin=SEED_PLACEHOLDER_GSTIN),
            row(invoice_number="INV-BAD-TOTAL", total_gst="2000.00"),
            row(invoice_number="INV-BAD-SPLIT", cgst="1000.00",
                sgst="1250.00"),
            row(invoice_number="INV-BOTH-STRUCTURES", igst="100.00",
                total_gst="2350.00"),
            row(),  # valid baseline
        ]
        parsed = gstr2b.parse_gstr2b(csv_bytes(bad_rows), "mixed.csv")
        self.assertEqual(parsed.total_rows, 10)
        self.assertEqual(parsed.accepted_rows, 1)
        self.assertEqual(parsed.rejected_rows, 9)

        by_number = {r.invoice_number: r for r in parsed.rows}
        self.assertEqual(
            {i.code for i in by_number["INV-BAD-MONEY"].errors if
             i.field == "taxable_value"},
            {"INVALID_MONEY"},
        )
        self.assertIsNone(by_number["INV-BAD-MONEY"].taxable_value)
        self.assertEqual(by_number["INV-BAD-MONEY"].validation_status,
                         "invalid")
        self.assertIn("NEGATIVE_AMOUNT",
                      {i.code for i in by_number["INV-NEGATIVE"].errors})
        self.assertIn("REQUIRED_FIELD",
                      {i.code for i in by_number[None].errors})
        self.assertIn("INVALID_DATE",
                      {i.code for i in by_number["INV-BAD-DATE"].errors})
        self.assertIn("INVALID_TAX_PERIOD",
                      {i.code for i in by_number["INV-BAD-PERIOD"].errors})
        self.assertIn("GSTIN_FORMAT",
                      {i.code for i in by_number["INV-FAKE-GSTIN"].errors})
        self.assertIn("TAX_TOTAL_MISMATCH",
                      {i.code for i in by_number["INV-BAD-TOTAL"].errors})
        self.assertIn("CGST_SGST_MISMATCH",
                      {i.code for i in by_number["INV-BAD-SPLIT"].errors})
        self.assertIn("TAX_STRUCTURE_CONFLICT",
                      {i.code for i in by_number["INV-BOTH-STRUCTURES"].errors})
        # Every source row is preserved verbatim, including rejected ones.
        for parsed_row in parsed.rows:
            self.assertIn("supplier_gstin", parsed_row.raw_data)

    def test_duplicate_rows_in_same_file(self):
        parsed = gstr2b.parse_gstr2b(
            csv_bytes([row(), row()]), "dupes.csv"
        )
        self.assertEqual(parsed.total_rows, 2)
        self.assertEqual(parsed.accepted_rows, 1)
        second = parsed.rows[1]
        self.assertEqual(second.validation_status, "invalid")
        self.assertIn("DUPLICATE_ROW", {i.code for i in second.errors})

    def test_missing_required_header_is_fatal(self):
        header = _CSV_HEADER.replace(",total_gst", "")
        with self.assertRaises(ValueError) as caught:
            gstr2b.parse_gstr2b(csv_bytes([row()], header=header), "bad.csv")
        self.assertIn("missing required column", str(caught.exception))

    def test_duplicate_header_is_fatal(self):
        header = _CSV_HEADER + ",Taxable Value"
        with self.assertRaises(ValueError):
            gstr2b.parse_gstr2b(csv_bytes([row()], header=header), "dupe.csv")

    def test_empty_and_header_only_files_are_fatal(self):
        for content, name in (
            (b"", "empty.csv"),
            ((_CSV_HEADER + "\n").encode(), "header_only.csv"),
        ):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    gstr2b.parse_gstr2b(content, name)

    def test_unsupported_extension_is_fatal(self):
        with self.assertRaises(ValueError):
            gstr2b.parse_gstr2b(b"whatever", "gstr2b.txt")

    def test_non_utf8_csv_is_fatal(self):
        with self.assertRaises(ValueError):
            gstr2b.parse_gstr2b(b"\xff\xfe\x00bad", "binary.csv")


class XlsxParsingTests(unittest.TestCase):
    def test_valid_xlsx_with_numeric_and_date_cells(self):
        header = [name for name in _CSV_HEADER.split(",")]
        content = xlsx_bytes(header, [[
            GSTIN_INTRA, "INV-XLSX-1", datetime(2026, 3, 15), "2026-03",
            12500, 1125, 1125, 0, 2250,
        ]])
        parsed = gstr2b.parse_gstr2b(content, "gstr2b.xlsx")
        self.assertEqual(parsed.source_format, "xlsx")
        self.assertEqual(parsed.accepted_rows, 1, parsed.rows[0].errors)
        parsed_row = parsed.rows[0]
        self.assertEqual(parsed_row.invoice_date, "2026-03-15")
        self.assertEqual(parsed_row.taxable_value, 1_250_000)
        self.assertEqual(parsed_row.cgst, 112_500)
        self.assertEqual(parsed_row.total_gst, 225_000)

    def test_xlsx_fractional_rupees_convert_exactly(self):
        header = _CSV_HEADER.split(",")
        content = xlsx_bytes(header, [[
            GSTIN_INTER, "INV-XLSX-2", "2026-03-16", "2026-03",
            99.99, 0, 0, 17.9982, 17.9982,
        ]])
        # 17.9982 rupees has 4 decimals -> rejected, not rounded.
        parsed = gstr2b.parse_gstr2b(content, "fractional.xlsx")
        self.assertEqual(parsed.rejected_rows, 1)
        self.assertIn("INVALID_MONEY", {i.code for i in parsed.rows[0].errors})

    def test_garbage_xlsx_is_fatal(self):
        with self.assertRaises(ValueError):
            gstr2b.parse_gstr2b(b"not really xlsx", "fake.xlsx")


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="milaan-test-")
        self._old_env = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(Path(self._tmp) / "test_milaan.db")
        import db

        db.init_db()
        self.db = db

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._old_env
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _save(self, rows, name="gstr2b.csv"):
        parsed = gstr2b.parse_gstr2b(csv_bytes(rows), name)
        return parsed, self.db.save_gstr2b_import(parsed)

    def test_valid_fixture_persists(self):
        parsed, saved = self._save([
            row(),
            row(gstin=GSTIN_INTER, invoice_number="INV-G2B-2",
                taxable="5000.00", cgst="0.00", sgst="0.00",
                igst="900.00", total_gst="900.00"),
        ])
        self.assertEqual(saved["total_rows"], 2)
        self.assertEqual(saved["accepted_rows"], 2)
        self.assertEqual(saved["rejected_rows"], 0)

        batch = self.db.get_gstr2b_import(saved["import_id"])
        self.assertEqual(batch["file_hash"], parsed.file_hash)
        self.assertEqual(batch["status"], "completed")

        with self.db.db() as conn:
            rows = conn.execute(
                "SELECT * FROM gstr2b_rows WHERE import_id = ? "
                "ORDER BY row_number", (saved["import_id"],)
            ).fetchall()
            money_types = {
                conn.execute(
                    "SELECT typeof(taxable_value) FROM gstr2b_rows "
                    "WHERE id = ?", (rows[0]["id"],)
                ).fetchone()[0],
                conn.execute(
                    "SELECT typeof(total_gst) FROM gstr2b_rows "
                    "WHERE id = ?", (rows[0]["id"],)
                ).fetchone()[0],
            }
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["taxable_value"], 1_250_000)
        self.assertEqual(money_types, {"integer"})

        display = self.db.get_gstr2b_rows(saved["import_id"])
        self.assertEqual(len(display), 2)
        self.assertEqual(display[0]["Status"], "valid")
        self.assertEqual(display[0]["Taxable (Rs)"], "12500.00")

    def test_rejected_rows_stay_in_audit_trail(self):
        parsed, saved = self._save([
            row(invoice_number="INV-GOOD"),
            row(invoice_number="INV-BAD", taxable="not-a-number"),
        ])
        self.assertEqual(saved["accepted_rows"], 1)
        self.assertEqual(saved["rejected_rows"], 1)
        display = self.db.get_gstr2b_rows(saved["import_id"])
        self.assertEqual(len(display), 2)
        rejected = [r for r in display if r["Status"] == "invalid"]
        self.assertEqual(len(rejected), 1)
        self.assertIn("INVALID_MONEY", rejected[0]["Issues"])

        with self.db.db() as conn:
            stored = conn.execute(
                "SELECT raw_data, errors FROM gstr2b_rows WHERE import_id = ? "
                "AND row_number = 2", (saved["import_id"],)
            ).fetchone()
        self.assertIn("INV-BAD", stored["raw_data"])
        self.assertIn("INVALID_MONEY", stored["errors"])

    def test_file_hash_detection_and_intentional_reimport(self):
        content = csv_bytes([row()])
        parsed = gstr2b.parse_gstr2b(content, "same.csv")
        first = self.db.save_gstr2b_import(parsed)

        prior = self.db.find_gstr2b_imports_by_hash(parsed.file_hash)
        self.assertEqual(len(prior), 1)
        self.assertEqual(prior[0]["id"], first["import_id"])

        # An intentional re-import creates a separate batch (never silent
        # deduplication), while the original stays intact.
        second = self.db.save_gstr2b_import(parsed)
        self.assertNotEqual(first["import_id"], second["import_id"])
        prior = self.db.find_gstr2b_imports_by_hash(parsed.file_hash)
        self.assertEqual(len(prior), 2)
        self.assertEqual(len(self.db.get_gstr2b_rows(first["import_id"])), 1)

    def test_different_content_has_different_hash(self):
        one = gstr2b.parse_gstr2b(csv_bytes([row()]), "a.csv")
        two = gstr2b.parse_gstr2b(
            csv_bytes([row(invoice_number="INV-OTHER")]), "b.csv"
        )
        self.assertNotEqual(one.file_hash, two.file_hash)
        self.db.save_gstr2b_import(one)
        self.assertEqual(self.db.find_gstr2b_imports_by_hash(two.file_hash), [])

    def test_transaction_rollback_leaves_no_partial_batch(self):
        parsed = gstr2b.parse_gstr2b(
            csv_bytes([row(), row(invoice_number="INV-2")]), "rollback.csv"
        )
        original = self.db._insert_gstr2b_row
        calls = {"count": 0}

        def failing_insert(conn, import_id, parsed_row):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("simulated failure")
            return original(conn, import_id, parsed_row)

        self.db._insert_gstr2b_row = failing_insert
        try:
            with self.assertRaises(RuntimeError):
                self.db.save_gstr2b_import(parsed)
        finally:
            self.db._insert_gstr2b_row = original

        with self.db.db() as conn:
            n_imports = conn.execute(
                "SELECT COUNT(*) FROM gstr2b_imports"
            ).fetchone()[0]
            n_rows = conn.execute(
                "SELECT COUNT(*) FROM gstr2b_rows"
            ).fetchone()[0]
        self.assertEqual((n_imports, n_rows), (0, 0))

    def test_import_survives_init_db(self):
        _parsed, saved = self._save([row()])
        self.db.init_db()
        self.assertEqual(self.db.get_gstr2b_import(saved["import_id"])["id"],
                         saved["import_id"])

    def test_import_does_not_touch_purchase_invoices(self):
        self.db.seed_demo_data()
        self.db.revalidate_all_invoices()
        with self.db.db() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM invoices"
            ).fetchone()[0]
            statuses_before = [
                row[0] for row in conn.execute(
                    "SELECT validation_status FROM invoices ORDER BY id"
                ).fetchall()
            ]
        _parsed, saved = self._save([
            row(),
            row(invoice_number="INV-BAD", taxable="-1"),
        ])
        with self.db.db() as conn:
            after = conn.execute(
                "SELECT COUNT(*) FROM invoices"
            ).fetchone()[0]
            statuses_after = [
                row[0] for row in conn.execute(
                    "SELECT validation_status FROM invoices ORDER BY id"
                ).fetchall()
            ]
            n_suppliers = conn.execute(
                "SELECT COUNT(*) FROM suppliers"
            ).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(statuses_before, statuses_after)
        self.assertEqual(n_suppliers, 3)
        self.assertEqual(saved["total_rows"], 2)

    def test_batch_constraint_rejects_inconsistent_counts(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.db() as conn:
                conn.execute(
                    """INSERT INTO gstr2b_imports
                           (file_name, file_hash, file_size, source_format,
                            total_rows, accepted_rows, rejected_rows)
                       VALUES ('x.csv', 'hash', 1, 'csv', 5, 1, 0)"""
                )

    def test_row_money_columns_reject_floats_via_sql(self):
        with self.db.db() as conn:
            conn.execute(
                "INSERT INTO gstr2b_imports (file_name, file_hash, file_size, "
                "source_format, total_rows, accepted_rows, rejected_rows) "
                "VALUES ('x.csv', 'h', 1, 'csv', 1, 0, 1)"
            )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO gstr2b_rows
                           (import_id, row_number, validation_status,
                            raw_data, taxable_value)
                       VALUES (1, 1, 'invalid', '{}', 12.5)"""
                )

    def test_batch_history_ordering(self):
        first = self._save([row()])[1]["import_id"]
        second = self._save([row(invoice_number="INV-2")])[1]["import_id"]
        history = self.db.get_gstr2b_imports()
        self.assertEqual([item["Batch"] for item in history][:2],
                         [second, first])


if __name__ == "__main__":
    unittest.main()
