"""Smoke tests for the MILAAN SQLite layer (stdlib unittest, no extra deps).

Run: .venv/bin/python -m unittest test_db -v
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

EXPECTED_SEED_SUPPLIERS = 3
EXPECTED_SEED_INVOICES = 6
EXPECTED_SEED_TOTAL_GST_PAISE = 1_604_886


class DBTests(unittest.TestCase):
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

    def test_default_path_when_env_unset(self):
        import db

        os.environ.pop("DATABASE_PATH", None)
        try:
            self.assertEqual(db.get_database_path().name, "milaan.db")
        finally:
            os.environ["DATABASE_PATH"] = str(Path(self._tmp) / "test_milaan.db")

    def test_env_value_respected(self):
        import db

        self.assertEqual(
            str(db.get_database_path()),
            str((Path(self._tmp) / "test_milaan.db").resolve()),
        )

    def test_relative_database_path_is_resolved_from_project_root(self):
        import db

        os.environ["DATABASE_PATH"] = "demo/relative-test.db"
        self.assertEqual(
            db.get_database_path(),
            (Path(__file__).parent / "demo" / "relative-test.db").resolve(),
        )

    def test_empty_database_path_is_a_configuration_error(self):
        import db

        os.environ["DATABASE_PATH"] = "  "
        with self.assertRaisesRegex(ValueError, "DATABASE_PATH must not be empty"):
            db.get_database_path()


class SupplierInvoiceSchemaTests(unittest.TestCase):
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

    def _add_supplier(self, name="Test Supplies Pvt Ltd", gstin="27ABCDE1234F1Z5"):
        with self.db.db() as conn:
            conn.execute(
                "INSERT INTO suppliers (supplier_name, gstin) VALUES (?, ?)",
                (name, gstin),
            )
            return conn.execute(
                "SELECT id FROM suppliers WHERE gstin = ?", (gstin,)
            ).fetchone()[0]

    def _supplier_id(self):
        if not hasattr(self, "_cached_supplier_id"):
            self._cached_supplier_id = self._add_supplier()
        return self._cached_supplier_id

    def _insert_invoice(self, conn, overrides=None):
        supplier_id = self._supplier_id()
        fields = {
            "supplier_id": supplier_id,
            "invoice_number": "INV-TEST-1",
            "invoice_date": "2026-01-01",
            "tax_period": "2026-01",
            "taxable_value": 1_250_000,
            "gst_rate": 18,
            "cgst": 112_500,
            "sgst": 112_500,
            "igst": 0,
            "total_gst": 225_000,
            "invoice_total": 1_475_000,
            "validation_status": "valid",
            "case_status": "open",
        }
        fields.update(overrides or {})
        conn.execute(
            """INSERT INTO invoices
                   (supplier_id, invoice_number, invoice_date, tax_period,
                    taxable_value, gst_rate, cgst, sgst, igst, total_gst,
                    invoice_total, validation_status, case_status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                fields["supplier_id"], fields["invoice_number"],
                fields["invoice_date"], fields["tax_period"],
                fields["taxable_value"], fields["gst_rate"],
                fields["cgst"], fields["sgst"], fields["igst"],
                fields["total_gst"], fields["invoice_total"],
                fields["validation_status"], fields["case_status"],
            ),
        )

    # --- schema / Task 1 preserved tests ------------------------------------

    def test_init_and_ready(self):
        self.assertTrue(self.db.is_db_ready())

    def test_persistence_across_connections(self):
        self._add_supplier()
        with self.db.db() as conn:
            rows = conn.execute(
                "SELECT supplier_name, gstin FROM suppliers"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["supplier_name"], "Test Supplies Pvt Ltd")
        self.assertEqual(rows[0]["gstin"], "27ABCDE1234F1Z5")

    def test_init_db_repeatable_preserves_records(self):
        with self.db.db() as conn:
            self._insert_invoice(conn)  # also creates the supplier
        self.db.init_db()
        self.db.init_db()
        with self.db.db() as conn:
            n_suppliers = conn.execute(
                "SELECT COUNT(*) FROM suppliers"
            ).fetchone()[0]
            n_invoices = conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0]
        self.assertEqual((n_suppliers, n_invoices), (1, 1))

    # --- constraints ---------------------------------------------------------

    def test_foreign_key_enforcement(self):
        with self.db.db() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO invoices
                           (supplier_id, invoice_number, invoice_date, tax_period,
                            taxable_value, gst_rate, cgst, sgst, igst, total_gst,
                            invoice_total)
                       VALUES (99999, 'INV-X', '2026-01-01', '2026-01',
                               100, 18, 9, 9, 0, 18, 118)"""
                )

    def test_unique_invoice_per_supplier_and_period(self):
        with self.db.db() as conn:
            self._insert_invoice(conn)
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_invoice(conn)
        # Same number for a different period is allowed.
        with self.db.db() as conn:
            self._insert_invoice(conn, {"tax_period": "2026-02"})

    def test_monetary_columns_reject_floats(self):
        with self.db.db() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_invoice(conn, {"taxable_value": 1250.5})

    def test_monetary_columns_store_integers(self):
        with self.db.db() as conn:
            self._insert_invoice(conn)
            types = {
                row[0]
                for row in conn.execute(
                    """SELECT DISTINCT typeof(taxable_value) FROM invoices
                       UNION SELECT DISTINCT typeof(cgst) FROM invoices
                       UNION SELECT DISTINCT typeof(sgst) FROM invoices
                       UNION SELECT DISTINCT typeof(igst) FROM invoices
                       UNION SELECT DISTINCT typeof(total_gst) FROM invoices
                       UNION SELECT DISTINCT typeof(invoice_total) FROM invoices
                       UNION SELECT DISTINCT typeof(gst_rate) FROM invoices"""
                ).fetchall()
            }
        self.assertEqual(types, {"integer"})

    def test_total_gst_must_equal_cgst_plus_sgst_plus_igst(self):
        with self.db.db() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_invoice(conn, {"total_gst": 999})

    def test_negative_amount_rejected(self):
        with self.db.db() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_invoice(conn, {"taxable_value": -1})

    def test_invalid_validation_status_rejected(self):
        with self.db.db() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_invoice(conn, {"validation_status": "bogus"})

    def test_invalid_case_status_rejected(self):
        with self.db.db() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                self._insert_invoice(conn, {"case_status": "bogus"})

    def test_duplicate_supplier_gstin_rejected(self):
        self._add_supplier(gstin="27ZZZZ1234F1Z5")
        with self.db.db() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO suppliers (supplier_name, gstin) VALUES (?, ?)",
                    ("Duplicate GSTIN Ltd", "27ZZZZ1234F1Z5"),
                )

    def test_format_paise_uses_integer_arithmetic(self):
        self.assertEqual(self.db.format_paise(0), "0.00")
        self.assertEqual(self.db.format_paise(1_604_886), "16048.86")
        self.assertEqual(self.db.format_paise(-5), "-0.05")
        with self.assertRaises(TypeError):
            self.db.format_paise(1.5)  # type: ignore[arg-type]


class SeedTests(unittest.TestCase):
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

    def _counts(self):
        with self.db.db() as conn:
            return (
                conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0],
                conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],
            )

    def test_seed_inserts_expected_rows(self):
        added = self.db.seed_demo_data()
        self.assertEqual(
            added,
            {"suppliers": EXPECTED_SEED_SUPPLIERS, "invoices": EXPECTED_SEED_INVOICES},
        )
        self.assertEqual(self._counts(), (EXPECTED_SEED_SUPPLIERS, EXPECTED_SEED_INVOICES))

    def test_seed_is_idempotent(self):
        first = self.db.seed_demo_data()
        second = self.db.seed_demo_data()
        self.assertEqual(second, {"suppliers": 0, "invoices": 0})
        self.assertEqual(first, {"suppliers": EXPECTED_SEED_SUPPLIERS,
                                 "invoices": EXPECTED_SEED_INVOICES})
        self.assertEqual(self._counts(), (EXPECTED_SEED_SUPPLIERS, EXPECTED_SEED_INVOICES))

    def test_seed_data_marked_synthetic(self):
        self.db.seed_demo_data()
        with self.db.db() as conn:
            suppliers = conn.execute(
                "SELECT supplier_name, gstin FROM suppliers ORDER BY id"
            ).fetchall()
            sources = conn.execute(
                "SELECT DISTINCT source FROM invoices"
            ).fetchall()
        for row in suppliers:
            self.assertTrue(row["supplier_name"].startswith("(Synthetic)"))
        self.assertEqual(
            [row["source"] for row in sources],
            ["demo-seed"],
        )

    def test_seed_includes_one_deliberately_invalid_invoice(self):
        self.db.seed_demo_data()
        with self.db.db() as conn:
            statuses = {
                row["validation_status"]
                for row in conn.execute(
                    "SELECT validation_status FROM invoices"
                ).fetchall()
            }
        self.assertIn("invalid", statuses)
        with self.db.db() as conn:
            invalid_count = conn.execute(
                "SELECT COUNT(*) FROM invoices WHERE validation_status = 'invalid'"
            ).fetchone()[0]
        self.assertEqual(invalid_count, 1)

    def test_seed_totals_match_expected_gst(self):
        self.db.seed_demo_data()
        totals = self.db.get_totals()
        self.assertEqual(totals["invoice_count"], EXPECTED_SEED_INVOICES)
        self.assertEqual(totals["total_gst_paise"], EXPECTED_SEED_TOTAL_GST_PAISE)

    def test_seed_is_idempotent_after_record_deletion(self):
        self.db.seed_demo_data()
        with self.db.db() as conn:
            conn.execute("DELETE FROM suppliers WHERE gstin = '27AAACS1234A1Z2'")
        # Only supplier 1's three invoices were cascade-deleted.
        self.assertEqual(self._counts(), (2, 3))
        added = self.db.seed_demo_data()  # repopulates what is missing
        self.assertEqual(added, {"suppliers": 1, "invoices": 3})
        self.assertEqual(self._counts(), (EXPECTED_SEED_SUPPLIERS,
                                          EXPECTED_SEED_INVOICES))

    def test_seed_rolls_back_all_or_nothing(self):
        # Inject a bad invoice (negative amount) into a copy of the seed
        # list; the whole seed transaction must roll back and raise.
        bad = ("27AAACS1234A1Z2", "INV-BAD", "2026-05-01", "2026-05",
               -1, 18, 0, 0, 0, 0, -1, "valid")
        original = self.db.DEMO_INVOICES
        self.db.DEMO_INVOICES = list(original) + [bad]
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.seed_demo_data()
        finally:
            self.db.DEMO_INVOICES = original
        self.assertEqual(self._counts(), (0, 0))


class LegacyMigrationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="milaan-test-")
        self._old_env = os.environ.get("DATABASE_PATH")

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._old_env
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_task1_schema_is_migrated_and_data_preserved(self):
        legacy_path = Path(self._tmp) / "legacy.db"
        conn = sqlite3.connect(legacy_path)
        try:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(
                """CREATE TABLE suppliers (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       name TEXT NOT NULL,
                       gstin TEXT,
                       created_at TEXT NOT NULL DEFAULT (datetime('now'))
                   )"""
            )
            conn.execute(
                "INSERT INTO suppliers (name, gstin) VALUES (?, ?)",
                ("Legacy Supplier", "27LEGACY0000Z1X2"),
            )
            conn.commit()
        finally:
            conn.close()

        os.environ["DATABASE_PATH"] = str(legacy_path)
        import db

        db.init_db()  # should migrate, not drop the legacy row

        self.assertTrue(db.is_db_ready())
        with db.db() as conn:
            rows = conn.execute(
                "SELECT supplier_name, gstin, contact_phone FROM suppliers"
            ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["supplier_name"], "Legacy Supplier")
        self.assertEqual(rows[0]["gstin"], "27LEGACY0000Z1X2")
        self.assertIsNone(rows[0]["contact_phone"])


class AppSmokeTests(unittest.TestCase):
    """Exercises app.py's real Streamlit flow, including clicking the seed
    button via streamlit.testing.v1.AppTest."""

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

    def test_app_runs_without_exception(self):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(Path(__file__).parent / "app.py"),
                               default_timeout=30)
        at.run()
        self.assertEqual(at.exception, [])

    def test_seed_button_twice_does_not_duplicate(self):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(str(Path(__file__).parent / "app.py"),
                               default_timeout=30)
        at.run()
        self.assertEqual(at.exception, [])

        seed = at.button(key="seed_demo")
        self.assertIsNotNone(seed)
        seed.click().run()
        self.assertEqual(at.exception, [])
        # Success message on first click.
        self.assertTrue(len(at.success) == 1, at.success[0].value if at.success else "no message")

        at.button(key="seed_demo").click().run()
        self.assertEqual(at.exception, [])
        # Metrics show the seeded values after both clicks.
        # Metrics show the seeded values after both clicks (streamlit
        # exposes metric values as display strings).
        metrics = {m.label: str(m.value) for m in at.metric}
        self.assertEqual(metrics.get("Invoices"), "6")

        import db
        totals = db.get_totals()
        self.assertEqual(totals["invoice_count"], 6)
        self.assertEqual(totals["total_gst_paise"], EXPECTED_SEED_TOTAL_GST_PAISE)

        with db.db() as conn:
            n_suppliers = conn.execute("SELECT COUNT(*) FROM suppliers").fetchone()[0]
        self.assertEqual(n_suppliers, 3)


if __name__ == "__main__":
    unittest.main()
