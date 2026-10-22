"""Tests for the deterministic validation service and its DB integration.

Run: .venv/bin/python -m unittest test_validation -v

All GSTINs here are fabricated. Valid-checksum fixtures are generated
with a reference implementation of the documented GSTN check-digit
algorithm (written independently of validation.py).
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import validation  # noqa: E402

_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def reference_checksum(first_14: str) -> str:
    """Independent reference implementation of the GSTN base-36 check digit."""
    total = 0
    for position, character in enumerate(first_14):
        value = _CHARSET.index(character.upper())
        weight = 1 if position % 2 == 0 else 2
        product = value * weight
        total += product // 36 + product % 36
    return _CHARSET[(36 - total % 36) % 36]


def make_gstin(first_14: str) -> str:
    return first_14 + reference_checksum(first_14)


GSTIN_INTRA = make_gstin("27AAACS1234A1Z")   # synthetic, Maharashtra
GSTIN_INTER = make_gstin("29AAACB5678B1Z")   # synthetic, Karnataka
SEED_PLACEHOLDER_GSTIN = "27FAKE1000F1Z5"    # deliberately fabricated (seed)


def valid_invoice(**overrides) -> dict:
    data = {
        "supplier_gstin": GSTIN_INTRA,
        "invoice_number": "INV-TEST-1",
        "invoice_date": "2026-03-15",
        "tax_period": "2026-03",
        "taxable_value": 1_250_000,
        "gst_rate": 18,
        "cgst": 112_500,
        "sgst": 112_500,
        "igst": 0,
        "total_gst": 225_000,
        "invoice_total": 1_475_000,
    }
    data.update(overrides)
    return data


def codes(result) -> set[str]:
    return {issue.code for issue in result.errors}


def warning_codes(result) -> set[str]:
    return {issue.code for issue in result.warnings}


class GstinValidationTests(unittest.TestCase):
    def test_valid_synthetic_gstin(self):
        check = validation.validate_gstin(GSTIN_INTRA)
        self.assertTrue(check.is_format_valid)
        self.assertEqual(check.errors, ())
        self.assertEqual(check.normalized, GSTIN_INTRA)

    def test_lowercase_and_whitespace_are_normalized(self):
        messy = f" {GSTIN_INTRA[:7]} {GSTIN_INTRA[7:].lower()} "
        check = validation.validate_gstin(messy)
        self.assertTrue(check.is_format_valid, check.errors)
        self.assertEqual(check.normalized, GSTIN_INTRA)

    def test_demo_gstins_are_checksum_valid_and_marked_synthetic(self):
        import db

        for name, gstin, _phone in db.DEMO_SUPPLIERS:
            with self.subTest(gstin=gstin):
                check = validation.validate_gstin(gstin)
                self.assertTrue(check.is_format_valid, check.errors)
                self.assertTrue(name.startswith("(Synthetic)"))

    def test_checksum_failure_rejected(self):
        wrong = "0" if GSTIN_INTRA[-1] != "0" else "1"
        check = validation.validate_gstin(GSTIN_INTRA[:-1] + wrong)
        self.assertFalse(check.is_format_valid)
        self.assertIn("GSTIN_CHECKSUM", {i.code for i in check.errors})

    def test_short_and_garbage_rejected(self):
        for bad in ("27AAACS1234A1Z", "27AAACS1234A1Z22", "27-AAACS-1234A", ""):
            with self.subTest(gstin=bad):
                check = validation.validate_gstin(bad)
                self.assertFalse(check.is_format_valid)
                if bad:
                    self.assertIn("GSTIN_FORMAT", {i.code for i in check.errors})

    def test_unknown_state_code_rejected(self):
        gstin = make_gstin("39AAACS1234A1Z")  # 39 is not a GST state code
        check = validation.validate_gstin(gstin)
        self.assertFalse(check.is_format_valid)
        self.assertIn("GSTIN_STATE_CODE", {i.code for i in check.errors})

    def test_checksum_helper_matches_reference(self):
        for prefix in ("27AAACS1234A1Z", "29AAACB5678B1Z", "24AAACK9012C1Z",
                       "07AAACR5055K1Z"):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    validation.gstin_checksum(prefix),
                    reference_checksum(prefix),
                )

    def test_checksum_helper_rejects_bad_length(self):
        with self.assertRaises(ValueError):
            validation.gstin_checksum("27AAACS1234A1")
        with self.assertRaises(ValueError):
            validation.gstin_checksum("27AAACS1234A1?Z")


class MoneyTests(unittest.TestCase):
    def test_rupees_to_paise_is_exact(self):
        self.assertEqual(validation.rupees_to_paise("1250.50"), 125_050)
        self.assertEqual(validation.rupees_to_paise(Decimal("0.01")), 1)
        self.assertEqual(validation.rupees_to_paise(Decimal("0.10")), 10)
        self.assertEqual(validation.rupees_to_paise("0.29"), 29)
        self.assertEqual(validation.rupees_to_paise(10), 1_000)

    def test_float_money_rejected(self):
        with self.assertRaises(TypeError):
            validation.rupees_to_paise(1250.50)
        with self.assertRaises(TypeError):
            validation.rupees_to_paise(True)

    def test_more_than_two_decimals_rejected(self):
        with self.assertRaises(ValueError):
            validation.rupees_to_paise("1.001")
        with self.assertRaises(ValueError):
            validation.rupees_to_paise(Decimal("0.009"))

    def test_non_finite_rejected(self):
        with self.assertRaises(ValueError):
            validation.rupees_to_paise(Decimal("NaN"))
        with self.assertRaises(ValueError):
            validation.rupees_to_paise(Decimal("Infinity"))

    def test_float_amounts_in_invoice_are_invalid_not_coerced(self):
        result = validation.validate_invoice(
            valid_invoice(taxable_value=1250.0)
        )
        self.assertEqual(result.status, validation.STATUS_INVALID)
        self.assertIn("MONEY_NOT_INTEGER", codes(result))

    def test_bool_is_not_accepted_as_money(self):
        result = validation.validate_invoice(valid_invoice(taxable_value=True))
        self.assertEqual(result.status, validation.STATUS_INVALID)
        self.assertIn("MONEY_NOT_INTEGER", codes(result))


class InvoiceValidationTests(unittest.TestCase):
    def test_valid_intrastate_invoice(self):
        result = validation.validate_invoice(valid_invoice())
        self.assertEqual(result.status, validation.STATUS_VALID)
        self.assertEqual(result.errors, ())
        self.assertEqual(result.warnings, ())
        self.assertTrue(any("NOT live GST registration" in r
                            for r in result.reasons))

    def test_valid_interstate_invoice(self):
        result = validation.validate_invoice(valid_invoice(
            supplier_gstin=GSTIN_INTER,
            cgst=0, sgst=0, igst=225_000,
            total_gst=225_000, invoice_total=1_475_000,
        ))
        self.assertEqual(result.status, validation.STATUS_VALID, result.errors)

    def test_zero_rated_invoice_is_valid(self):
        result = validation.validate_invoice(valid_invoice(
            gst_rate=0, cgst=0, sgst=0, igst=0,
            total_gst=0, invoice_total=1_250_000,
        ))
        self.assertEqual(result.status, validation.STATUS_VALID, result.errors)

    def test_malformed_gstin_makes_invoice_invalid(self):
        result = validation.validate_invoice(
            valid_invoice(supplier_gstin=SEED_PLACEHOLDER_GSTIN)
        )
        self.assertEqual(result.status, validation.STATUS_INVALID)
        self.assertIn("GSTIN_FORMAT", codes(result))

    def test_checksum_failure_makes_invoice_invalid(self):
        wrong = "0" if GSTIN_INTRA[-1] != "0" else "1"
        result = validation.validate_invoice(
            valid_invoice(supplier_gstin=GSTIN_INTRA[:-1] + wrong)
        )
        self.assertEqual(result.status, validation.STATUS_INVALID)
        self.assertIn("GSTIN_CHECKSUM", codes(result))

    def test_missing_gstin_needs_review(self):
        result = validation.validate_invoice(valid_invoice(supplier_gstin=None))
        self.assertEqual(result.status, validation.STATUS_NEEDS_REVIEW)
        self.assertEqual(result.errors, ())
        self.assertIn("GSTIN_MISSING", warning_codes(result))

    def test_both_tax_structures_charged_is_invalid(self):
        result = validation.validate_invoice(valid_invoice(
            igst=112_500, total_gst=337_500, invoice_total=1_587_500,
        ))
        self.assertEqual(result.status, validation.STATUS_INVALID)
        self.assertIn("TAX_STRUCTURE_CONFLICT", codes(result))

    def test_cgst_sgst_mismatch_is_invalid(self):
        result = validation.validate_invoice(valid_invoice(
            cgst=112_501, sgst=112_499,
        ))
        self.assertEqual(result.status, validation.STATUS_INVALID)
        self.assertIn("CGST_SGST_MISMATCH", codes(result))

    def test_total_gst_must_be_sum_of_components(self):
        result = validation.validate_invoice(valid_invoice(
            total_gst=225_001, invoice_total=1_475_001,
        ))
        self.assertEqual(result.status, validation.STATUS_INVALID)
        self.assertIn("TAX_TOTAL_MISMATCH", codes(result))

    def test_invoice_total_must_equal_taxable_plus_gst(self):
        result = validation.validate_invoice(valid_invoice(
            invoice_total=1_475_001,
        ))
        self.assertEqual(result.status, validation.STATUS_INVALID)
        self.assertIn("INVOICE_TOTAL_MISMATCH", codes(result))

    def test_rate_mismatch_is_invalid(self):
        # 12% amounts but gst_rate claims 18%.
        result = validation.validate_invoice(valid_invoice(
            gst_rate=18, cgst=75_000, sgst=75_000,
            total_gst=150_000, invoice_total=1_400_000,
        ))
        self.assertEqual(result.status, validation.STATUS_INVALID)
        self.assertIn("RATE_MISMATCH", codes(result))

    def test_unsupported_rate_is_invalid(self):
        result = validation.validate_invoice(valid_invoice(gst_rate=15))
        self.assertEqual(result.status, validation.STATUS_INVALID)
        self.assertIn("UNSUPPORTED_RATE", codes(result))

    def test_negative_amounts_are_invalid(self):
        for field in validation.MONEY_FIELDS:
            with self.subTest(field=field):
                result = validation.validate_invoice(valid_invoice(**{field: -1}))
                self.assertEqual(result.status, validation.STATUS_INVALID)
                self.assertIn("NEGATIVE_AMOUNT", codes(result))

    def test_rounding_tolerance_needs_review(self):
        # 75 paise at 18% = 13.5 paise; half-up expects 14, booked 13.
        result = validation.validate_invoice(valid_invoice(
            supplier_gstin=GSTIN_INTER,
            taxable_value=75, cgst=0, sgst=0,
            igst=13, total_gst=13, invoice_total=88,
        ))
        self.assertEqual(result.status, validation.STATUS_NEEDS_REVIEW)
        self.assertIn("ROUNDING_TOLERANCE", warning_codes(result))

    def test_rounding_exact_amount_is_valid(self):
        result = validation.validate_invoice(valid_invoice(
            supplier_gstin=GSTIN_INTER,
            taxable_value=75, cgst=0, sgst=0,
            igst=14, total_gst=14, invoice_total=89,
        ))
        self.assertEqual(result.status, validation.STATUS_VALID, result.errors)

    def test_rounding_beyond_tolerance_is_invalid(self):
        result = validation.validate_invoice(valid_invoice(
            supplier_gstin=GSTIN_INTER,
            taxable_value=75, cgst=0, sgst=0,
            igst=12, total_gst=12, invoice_total=87,
        ))
        self.assertEqual(result.status, validation.STATUS_INVALID)
        self.assertIn("RATE_MISMATCH", codes(result))

    def test_required_fields(self):
        result = validation.validate_invoice(valid_invoice(
            invoice_number="   ", invoice_date="", tax_period="",
        ))
        self.assertEqual(result.status, validation.STATUS_INVALID)
        required = [
            i.field for i in result.errors if i.code == "REQUIRED_FIELD"
        ]
        self.assertIn("invoice_number", required)
        self.assertIn("invoice_date", required)
        self.assertIn("tax_period", required)

    def test_invalid_dates(self):
        for bad_date in ("2026-02-30", "15-03-2026", "2026/03/15"):
            with self.subTest(date=bad_date):
                result = validation.validate_invoice(
                    valid_invoice(invoice_date=bad_date)
                )
                self.assertEqual(result.status, validation.STATUS_INVALID)
                self.assertIn("INVALID_DATE", codes(result))

    def test_invalid_tax_periods(self):
        for bad_period in ("2026-13", "2026-00", "202603", "13-2026"):
            with self.subTest(period=bad_period):
                result = validation.validate_invoice(
                    valid_invoice(tax_period=bad_period)
                )
                self.assertEqual(result.status, validation.STATUS_INVALID)
                self.assertIn("INVALID_TAX_PERIOD", codes(result))

    def test_duplicate_invoice_is_invalid(self):
        result = validation.validate_invoice(
            valid_invoice(), duplicate_exists=True
        )
        self.assertEqual(result.status, validation.STATUS_INVALID)
        self.assertIn("DUPLICATE_INVOICE", codes(result))

    def test_as_dict_is_structured(self):
        payload = validation.validate_invoice(valid_invoice()).as_dict()
        self.assertEqual(payload["status"], "valid")
        self.assertEqual(payload["errors"], [])
        self.assertEqual(payload["warnings"], [])
        self.assertTrue(isinstance(payload["reasons"], list))

    def test_can_message_supplier_gate(self):
        self.assertTrue(validation.can_message_supplier("valid"))
        for blocked in ("invalid", "needs_review", "pending", "mismatch", ""):
            with self.subTest(status=blocked):
                self.assertFalse(validation.can_message_supplier(blocked))

    def test_statuses_are_schema_compatible(self):
        import db

        self.assertIn(validation.STATUS_VALID, db.VALIDATION_STATUSES)
        self.assertIn(validation.STATUS_INVALID, db.VALIDATION_STATUSES)
        self.assertIn(validation.STATUS_NEEDS_REVIEW, db.VALIDATION_STATUSES)


class DBValidationIntegrationTests(unittest.TestCase):
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

    def _add_invoice(self, gstin=GSTIN_INTRA, **overrides):
        fields = valid_invoice(**overrides)
        with self.db.db() as conn:
            conn.execute(
                "INSERT INTO suppliers (supplier_name, gstin) VALUES (?, ?)",
                ("Synthetic Validation Supplier", gstin),
            )
            supplier_id = conn.execute(
                "SELECT id FROM suppliers WHERE gstin = ?", (gstin,)
            ).fetchone()[0]
            cursor = conn.execute(
                """INSERT INTO invoices
                       (supplier_id, invoice_number, invoice_date, tax_period,
                        taxable_value, gst_rate, cgst, sgst, igst, total_gst,
                        invoice_total, source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'test')""",
                (supplier_id, fields["invoice_number"], fields["invoice_date"],
                 fields["tax_period"], fields["taxable_value"],
                 fields["gst_rate"], fields["cgst"], fields["sgst"],
                 fields["igst"], fields["total_gst"],
                 fields["invoice_total"]),
            )
            return cursor.lastrowid

    def _stored_status(self, invoice_id):
        with self.db.db() as conn:
            return conn.execute(
                "SELECT validation_status FROM invoices WHERE id = ?",
                (invoice_id,),
            ).fetchone()[0]

    def test_valid_invoice_persists_valid_status(self):
        invoice_id = self._add_invoice()
        result = self.db.revalidate_invoice(invoice_id)
        self.assertEqual(result.status, validation.STATUS_VALID)
        self.assertEqual(self._stored_status(invoice_id), "valid")

    def test_fake_gstin_persists_invalid_status(self):
        invoice_id = self._add_invoice(gstin=SEED_PLACEHOLDER_GSTIN)
        result = self.db.revalidate_invoice(invoice_id)
        self.assertEqual(result.status, validation.STATUS_INVALID)
        self.assertIn("GSTIN_FORMAT", codes(result))
        self.assertEqual(self._stored_status(invoice_id), "invalid")

    def test_seed_data_validates_except_intentional_invalid_record(self):
        self.db.seed_demo_data()
        entries = self.db.revalidate_all_invoices()
        self.assertEqual(len(entries), 6)
        statuses = [entry["result"].status for entry in entries]
        self.assertEqual(statuses.count(validation.STATUS_VALID), 5)
        self.assertEqual(statuses.count(validation.STATUS_INVALID), 1)
        summary = self.db.get_validation_summary()
        self.assertEqual(summary["valid"], 5)
        self.assertEqual(summary["invalid"], 1)
        invalid = [
            entry for entry in entries
            if entry["result"].status == validation.STATUS_INVALID
        ]
        self.assertEqual(invalid[0]["invoice_number"], "INV-DEMO-201")

    def test_update_rejects_unknown_status(self):
        invoice_id = self._add_invoice()
        with self.assertRaises(ValueError):
            self.db.update_invoice_validation(invoice_id, "definitely_bogus")
        self.assertEqual(self._stored_status(invoice_id), "pending")

    def test_all_schema_statuses_are_persistable(self):
        invoice_id = self._add_invoice()
        for status in self.db.VALIDATION_STATUSES:
            with self.subTest(status=status):
                self.db.update_invoice_validation(invoice_id, status)
                self.assertEqual(self._stored_status(invoice_id), status)

    def test_update_missing_invoice_raises(self):
        with self.assertRaises(KeyError):
            self.db.update_invoice_validation(999_999, "valid")

    def test_revalidate_missing_invoice_raises(self):
        with self.assertRaises(KeyError):
            self.db.revalidate_invoice(999_999)

    def test_duplicate_detection_uses_natural_key(self):
        invoice_id = self._add_invoice()
        with self.db.db() as conn:
            row = conn.execute(
                "SELECT supplier_id, invoice_number, tax_period FROM invoices "
                "WHERE id = ?", (invoice_id,)
            ).fetchone()
        self.assertTrue(self.db.invoice_exists(
            row["supplier_id"], row["invoice_number"], row["tax_period"]
        ))
        self.assertFalse(self.db.invoice_exists(
            row["supplier_id"], row["invoice_number"], row["tax_period"],
            exclude_invoice_id=invoice_id,
        ))
        self.assertFalse(self.db.invoice_exists(
            row["supplier_id"], row["invoice_number"], "2026-04"
        ))

    def test_revalidation_status_survives_init_db(self):
        self.db.seed_demo_data()
        self.db.revalidate_all_invoices()
        self.db.init_db()
        summary = self.db.get_validation_summary()
        self.assertEqual(summary["valid"], 5)
        self.assertEqual(summary["invalid"], 1)
        self.assertEqual(summary["pending"], 0)


class ValidationStatusMigrationTests(unittest.TestCase):
    """A database created by Task 2 (no needs_review in the CHECK) is
    rebuilt in place without losing rows."""

    OLD_INVOICES_DDL = """
    CREATE TABLE IF NOT EXISTS invoices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        supplier_id INTEGER NOT NULL,
        invoice_number TEXT NOT NULL,
        invoice_date TEXT NOT NULL,
        tax_period TEXT NOT NULL,
        taxable_value INTEGER NOT NULL,
        gst_rate INTEGER NOT NULL,
        cgst INTEGER NOT NULL DEFAULT 0,
        sgst INTEGER NOT NULL DEFAULT 0,
        igst INTEGER NOT NULL DEFAULT 0,
        total_gst INTEGER NOT NULL DEFAULT 0,
        invoice_total INTEGER NOT NULL,
        source TEXT NOT NULL DEFAULT 'manual',
        validation_status TEXT NOT NULL DEFAULT 'pending',
        case_status TEXT NOT NULL DEFAULT 'open',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id) ON DELETE CASCADE,
        UNIQUE (supplier_id, invoice_number, tax_period),
        CHECK (taxable_value >= 0 AND cgst >= 0 AND sgst >= 0 AND igst >= 0
               AND total_gst >= 0 AND invoice_total >= 0),
        CHECK (cgst + sgst + igst = total_gst),
        CHECK (gst_rate IN (0, 5, 12, 18, 28)),
        CHECK (validation_status IN ('pending', 'valid', 'invalid', 'mismatch')),
        CHECK (case_status IN ('open', 'reconciled', 'follow_up', 'resolved')),
        CHECK (typeof(taxable_value) = 'integer' AND typeof(gst_rate) = 'integer'
               AND typeof(cgst) = 'integer' AND typeof(sgst) = 'integer'
               AND typeof(igst) = 'integer' AND typeof(total_gst) = 'integer'
               AND typeof(invoice_total) = 'integer')
    );
    """

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="milaan-test-")
        self._old_env = os.environ.get("DATABASE_PATH")
        self._path = Path(self._tmp) / "legacy_task2.db"
        os.environ["DATABASE_PATH"] = str(self._path)
        conn = sqlite3.connect(self._path)
        try:
            conn.execute(
                """CREATE TABLE suppliers (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       supplier_name TEXT NOT NULL,
                       gstin TEXT,
                       contact_phone TEXT,
                       created_at TEXT NOT NULL DEFAULT (datetime('now'))
                   )"""
            )
            conn.executescript(self.OLD_INVOICES_DDL)
            conn.execute(
                "INSERT INTO suppliers (supplier_name, gstin) VALUES (?, ?)",
                ("Task 2 Supplier", None),
            )
            conn.execute(
                """INSERT INTO invoices
                       (supplier_id, invoice_number, invoice_date, tax_period,
                        taxable_value, gst_rate, cgst, sgst, igst, total_gst,
                        invoice_total, validation_status)
                   VALUES (1, 'INV-OLD-1', '2026-01-10', '2026-01',
                           100000, 18, 9000, 9000, 0, 18000, 118000,
                           'pending')"""
            )
            conn.commit()
        finally:
            conn.close()

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._old_env
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_old_check_is_rebuilt_and_rows_preserved(self):
        import db

        db.init_db()

        with db.db() as conn:
            sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='invoices'"
            ).fetchone()["sql"]
            rows = conn.execute("SELECT * FROM invoices").fetchall()
        self.assertIn("needs_review", sql)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["invoice_number"], "INV-OLD-1")
        self.assertEqual(rows[0]["validation_status"], "pending")

        db.update_invoice_validation(rows[0]["id"], "needs_review")
        with db.db() as conn:
            status = conn.execute(
                "SELECT validation_status FROM invoices WHERE id = ?",
                (rows[0]["id"],),
            ).fetchone()[0]
        self.assertEqual(status, "needs_review")

    def test_bogus_status_still_rejected_after_migration(self):
        import db

        db.init_db()
        with self.assertRaises(sqlite3.IntegrityError):
            with db.db() as conn:
                conn.execute(
                    "UPDATE invoices SET validation_status = 'bogus' WHERE id = 1"
                )


class AppValidationFlowTests(unittest.TestCase):
    """Clicks the real Streamlit buttons via streamlit.testing.v1.AppTest."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="milaan-test-")
        self._old_env = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(Path(self._tmp) / "test_milaan.db")

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._old_env
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_seed_then_validate_button_updates_ui(self):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(Path(__file__).parent / "app.py"),
                               default_timeout=30)
        at.run()
        self.assertEqual(at.exception, [])

        at.button(key="seed_demo").click().run()
        self.assertEqual(at.exception, [])

        at.button(key="validate_all").click().run()
        self.assertEqual(at.exception, [])

        metrics = {m.label: str(m.value) for m in at.metric}
        self.assertEqual(metrics.get("Invoices"), "6")
        self.assertEqual(metrics.get("Valid"), "5")
        self.assertEqual(metrics.get("Invalid"), "1")
        self.assertEqual(metrics.get("Needs review"), "0")

        # Validation failures are visible, not swallowed.
        self.assertTrue(any("Validated 6 invoice(s)" in s.value
                            for s in at.success))

        import db

        summary = db.get_validation_summary()
        self.assertEqual(summary["valid"], 5)
        self.assertEqual(summary["invalid"], 1)
        self.assertEqual(summary["pending"], 0)


if __name__ == "__main__":
    unittest.main()
