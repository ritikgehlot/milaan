"""Tests for deterministic supplier follow-up preparation (Task 6).

Run: .venv/bin/python -m unittest test_followups -v

All suppliers, invoices and GSTR-2B rows are synthetic. No message is
sent by any code path under test.
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
import followups  # noqa: E402
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
GSTIN_D = make_gstin("27AAACE3456D1Z")
PERIOD = "2026-03"


def rupees(paise: int) -> str:
    return f"{paise // 100}.{paise % 100:02d}"


def csv_bytes(rows) -> bytes:
    lines = [_CSV_HEADER]
    for values in rows:
        lines.append(",".join(str(value) for value in values))
    return ("\n".join(lines) + "\n").encode("utf-8")


def gstr2b_row(gstin, invoice_number, total_gst_paise=225_000,
               tax_period=PERIOD):
    cgst = total_gst_paise // 2
    return [
        gstin,
        invoice_number,
        "2026-03-05",
        tax_period,
        "12500.00",
        rupees(cgst),
        rupees(total_gst_paise - cgst),
        "0.00",
        rupees(total_gst_paise),
    ]


def candidate(**overrides):
    values = {
        "invoice_id": 1,
        "invoice_number": "INV-1",
        "invoice_date": "2026-03-05",
        "tax_period": PERIOD,
        "taxable_value_paise": 1_250_000,
        "total_gst_paise": 225_000,
        "supplier_id": 10,
        "supplier_name": "Alpha Traders",
        "supplier_gstin": GSTIN_A,
        "contact_phone": "9999999999",
        "validation_status": "valid",
        "result_id": 100,
        "match_status": "MISSING_FROM_2B",
        "match_method": "none",
        "gstr2b_row_id": None,
        "amount_difference_paise": None,
        "reason_codes": ("NO_2B_ROW_FOUND",),
    }
    values.update(overrides)
    return followups.CandidateInvoice(**values)


class PlanningTests(unittest.TestCase):
    def test_clean_missing_invoice_becomes_draft(self):
        plans, excluded = followups.plan_followup_drafts(
            [candidate()], run_id=5, import_id=3
        )
        self.assertEqual(excluded, [])
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(plan.status, "DRAFT")
        self.assertTrue(plan.channel_available)
        self.assertEqual(plan.review_reasons, ())
        self.assertEqual(plan.total_gst_paise, 225_000)
        self.assertIn("INV-1", plan.draft_text)
        self.assertIn("reconciliation run #5", plan.draft_text)
        self.assertIn("batch #3", plan.draft_text)

    def test_every_non_missing_status_is_excluded(self):
        cases = [
            candidate(result_id=1, match_status="MATCHED", match_method="exact",
                      gstr2b_row_id=7, amount_difference_paise=0),
            candidate(result_id=2, match_status="AMOUNT_MISMATCH",
                      match_method="exact", gstr2b_row_id=8,
                      amount_difference_paise=100),
            candidate(result_id=3, match_status="NEEDS_REVIEW"),
            candidate(result_id=4, match_status="NOT_ELIGIBLE"),
        ]
        plans, excluded = followups.plan_followup_drafts(
            cases, run_id=5, import_id=3
        )
        self.assertEqual(plans, [])
        self.assertEqual(len(excluded), 4)
        reasons = {item.reason for item in excluded}
        self.assertIn("MATCH_STATUS_MATCHED", reasons)
        self.assertIn("MATCH_STATUS_AMOUNT_MISMATCH", reasons)
        self.assertIn("MATCH_STATUS_NEEDS_REVIEW", reasons)
        self.assertIn("MATCH_STATUS_NOT_ELIGIBLE", reasons)

    def test_invalid_or_unreviewed_invoice_is_excluded(self):
        for status in ("invalid", "needs_review", "pending"):
            with self.subTest(status=status):
                plans, excluded = followups.plan_followup_drafts(
                    [candidate(validation_status=status)],
                    run_id=5,
                    import_id=3,
                )
                self.assertEqual(plans, [])
                self.assertEqual(
                    excluded[0].reason, f"INVOICE_NOT_VALID:{status}"
                )

    def test_ambiguous_missing_evidence_is_excluded(self):
        variants = [
            candidate(gstr2b_row_id=11),
            candidate(amount_difference_paise=0),
            candidate(match_method="candidate_amount"),
            candidate(reason_codes=(
                "NO_2B_ROW_FOUND", "AMBIGUOUS_CANDIDATES"
            )),
            candidate(invoice_number=""),
        ]
        plans, excluded = followups.plan_followup_drafts(
            variants, run_id=5, import_id=3
        )
        self.assertEqual(plans, [])
        self.assertEqual(len(excluded), len(variants))

    def test_multiple_invoices_group_into_one_supplier_draft(self):
        plans, excluded = followups.plan_followup_drafts(
            [
                candidate(invoice_id=1, invoice_number="INV-1",
                          invoice_date="2026-03-05",
                          taxable_value_paise=1_000_000,
                          total_gst_paise=180_000),
                candidate(invoice_id=2, invoice_number="INV-2",
                          invoice_date="2026-03-20",
                          taxable_value_paise=500_000,
                          total_gst_paise=90_000,
                          result_id=101),
            ],
            run_id=5,
            import_id=3,
        )
        self.assertEqual(excluded, [])
        self.assertEqual(len(plans), 1)
        plan = plans[0]
        self.assertEqual(len(plan.invoices), 2)
        self.assertEqual(plan.total_taxable_value_paise, 1_500_000)
        self.assertEqual(plan.total_gst_paise, 270_000)
        self.assertIn("INV-1", plan.draft_text)
        self.assertIn("INV-2", plan.draft_text)
        self.assertIn("Rs 10000.00", plan.draft_text)
        self.assertIn("Rs 2700.00", plan.draft_text)

    def test_draft_wording_is_neutral_and_caveated(self):
        plan = followups.plan_followup_drafts(
            [candidate()], run_id=5, import_id=3
        )[0][0]
        text = plan.draft_text.lower()
        self.assertIn("please verify", text)
        self.assertIn("does not imply any default", text)
        self.assertIn("does not confirm", text)
        self.assertIn("not tax advice", text)
        for forbidden in (
            "failed to",
            "you have not filed",
            "non-filing",
            "you must pay",
            "guarantee",
            "recover your",
            "fraud",
            "liable",
        ):
            self.assertNotIn(forbidden, text)
        for channel in ("whatsapp", "email", "sms"):
            self.assertNotIn(channel, text)

    def test_missing_phone_marks_channel_unavailable(self):
        plan = followups.plan_followup_drafts(
            [candidate(contact_phone=None)], run_id=5, import_id=3
        )[0][0]
        self.assertFalse(plan.channel_available)
        self.assertEqual(plan.status, "NEEDS_REVIEW")
        self.assertIn(
            "CONTACT_PHONE_MISSING", plan.review_reasons
        )
        self.assertIsNone(plan.contact_phone)

    def test_duplicate_supplier_gstin_requires_review(self):
        plans, _ = followups.plan_followup_drafts(
            [
                candidate(invoice_id=1, supplier_id=10,
                          supplier_gstin=GSTIN_A.lower()),
                candidate(invoice_id=2, supplier_id=11,
                          supplier_gstin=GSTIN_A),
            ],
            run_id=5,
            import_id=3,
        )
        self.assertEqual(len(plans), 2)
        for plan in plans:
            self.assertEqual(plan.status, "NEEDS_REVIEW")
            self.assertIn("DUPLICATE_SUPPLIER_GSTIN", plan.review_reasons)


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

    def add_supplier(self, name, gstin, phone="9999999999"):
        with db.db() as conn:
            cursor = conn.execute(
                """INSERT INTO suppliers (supplier_name, gstin, contact_phone)
                   VALUES (?, ?, ?)""",
                (name, gstin, phone),
            )
            return cursor.lastrowid

    def add_invoice(self, supplier_id, number, total_gst=225_000,
                    status="valid", period=PERIOD,
                    invoice_date="2026-03-05"):
        taxable = 1_250_000
        with db.db() as conn:
            cursor = conn.execute(
                """INSERT INTO invoices
                       (supplier_id, invoice_number, invoice_date, tax_period,
                        taxable_value, gst_rate, cgst, sgst, igst, total_gst,
                        invoice_total, validation_status)
                   VALUES (?, ?, ?, ?, ?, 18, ?, 0, 0, ?, ?, ?)""",
                (
                    supplier_id,
                    number,
                    invoice_date,
                    period,
                    taxable,
                    total_gst,
                    total_gst,
                    taxable + total_gst,
                    status,
                ),
            )
            return cursor.lastrowid

    def import_rows(self, rows):
        parsed = gstr2b.parse_gstr2b(csv_bytes(rows), "followup.csv")
        return db.save_gstr2b_import(parsed)["import_id"]

    def prepare_case(self, phone="9999999999", invoice_count=1):
        """Supplier A invoices are missing; supplier B has a real match."""
        supplier_a = self.add_supplier("Alpha Traders", GSTIN_A, phone)
        invoice_ids = [
            self.add_invoice(
                supplier_a,
                f"INV-A-{index}",
                total_gst=225_000 + index,
                invoice_date=f"2026-03-{index + 1:02d}",
            )
            for index in range(invoice_count)
        ]
        supplier_b = self.add_supplier("Beta Traders", GSTIN_B, "8888888888")
        self.add_invoice(supplier_b, "INV-B-1", total_gst=90_000)
        batch_id = self.import_rows([
            gstr2b_row(GSTIN_B, "INV-B-1", total_gst_paise=90_000)
        ])
        summary = db.reconcile_gstr2b_import(batch_id)
        return {
            "supplier_a": supplier_a,
            "supplier_b": supplier_b,
            "invoice_ids": invoice_ids,
            "batch_id": batch_id,
            "run_id": summary["run_id"],
        }

    def draft_count(self):
        with db.db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM followup_drafts"
            ).fetchone()[0]


class GenerationTests(DatabaseTestCase):
    def test_only_missing_invoices_generate_drafts(self):
        case = self.prepare_case()
        result = db.generate_followup_drafts(case["batch_id"])
        self.assertEqual(result["created_drafts"], 1)
        self.assertEqual(result["eligible_invoices"], 1)

        draft = db.get_followup_draft(result["draft_ids"][0])
        self.assertEqual(draft["supplier_id"], case["supplier_a"])
        self.assertEqual(draft["status"], "DRAFT")
        self.assertIn("MISSING_FROM_2B", draft["inclusion_reason"])
        invoices = db.get_followup_draft_invoices(draft["id"])
        self.assertEqual(len(invoices), 1)
        self.assertEqual(invoices[0]["Invoice #"], case["invoice_ids"][0])
        self.assertEqual(invoices[0]["GST (Rs)"], "2250.00")
        self.assertEqual(invoices[0]["Reason"],
                         "MISSING_FROM_2B:NO_2B_ROW_FOUND")

        preview = db.get_followup_eligibility(case["batch_id"])
        excluded_reasons = {
            item["invoice_id"]: item["reason"]
            for item in preview["excluded"]
        }
        with db.db() as conn:
            beta_invoice = conn.execute(
                "SELECT id FROM invoices WHERE supplier_id = ?",
                (case["supplier_b"],),
            ).fetchone()[0]
        self.assertEqual(
            excluded_reasons[beta_invoice], "MATCH_STATUS_MATCHED"
        )

    def test_invalidated_invoice_is_excluded_from_drafts(self):
        case = self.prepare_case(invoice_count=2)
        db.update_invoice_validation(case["invoice_ids"][0], "invalid")
        preview = db.get_followup_eligibility(case["batch_id"])
        self.assertEqual(len(preview["plans"]), 1)
        self.assertEqual(len(preview["plans"][0]["invoices"]), 1)
        self.assertEqual(
            preview["plans"][0]["invoices"][0]["invoice_id"],
            case["invoice_ids"][1],
        )
        reasons = {item["reason"] for item in preview["excluded"]}
        self.assertIn("INVOICE_NOT_VALID:invalid", reasons)

    def test_grouping_and_totals_are_persisted(self):
        case = self.prepare_case(invoice_count=2)
        result = db.generate_followup_drafts(case["batch_id"])
        drafts = db.get_followup_drafts(gstr2b_import_id=case["batch_id"])
        self.assertEqual(len(drafts), 1)
        self.assertEqual(drafts[0]["Invoices"], 2)
        self.assertEqual(drafts[0]["GST (Rs)"], "4500.01")
        draft = db.get_followup_draft(result["draft_ids"][0])
        self.assertEqual(draft["total_taxable_value_paise"], 2_500_000)
        self.assertEqual(draft["total_gst_paise"], 450_001)

    def test_repeated_generation_does_not_duplicate(self):
        case = self.prepare_case()
        first = db.generate_followup_drafts(case["batch_id"])
        second = db.generate_followup_drafts(case["batch_id"])
        self.assertEqual(first["created_drafts"], 1)
        self.assertEqual(second["created_drafts"], 0)
        self.assertEqual(second["existing_drafts"], 1)
        self.assertEqual(self.draft_count(), 1)
        with db.db() as conn:
            associations = conn.execute(
                "SELECT COUNT(*) FROM followup_draft_invoices"
            ).fetchone()[0]
        self.assertEqual(associations, 1)

    def test_regeneration_preserves_edits(self):
        case = self.prepare_case()
        summary = db.generate_followup_drafts(case["batch_id"])
        draft_id = summary["draft_ids"][0]
        db.update_followup_draft_text(draft_id, "Edited text kept intact.")
        db.generate_followup_drafts(case["batch_id"])
        self.assertEqual(
            db.get_followup_draft(draft_id)["draft_text"],
            "Edited text kept intact.",
        )

    def test_requires_a_reconciliation_run(self):
        supplier_b = self.add_supplier("Beta Traders", GSTIN_B)
        self.add_invoice(supplier_b, "INV-B-1", total_gst=90_000)
        batch_id = self.import_rows([
            gstr2b_row(GSTIN_B, "INV-B-1", total_gst_paise=90_000)
        ])
        with self.assertRaises(ValueError):
            db.generate_followup_drafts(batch_id)

    def test_refuses_when_nothing_is_eligible(self):
        supplier_b = self.add_supplier("Beta Traders", GSTIN_B)
        self.add_invoice(supplier_b, "INV-B-1", total_gst=90_000)
        batch_id = self.import_rows([
            gstr2b_row(GSTIN_B, "INV-B-1", total_gst_paise=90_000)
        ])
        db.reconcile_gstr2b_import(batch_id)
        with self.assertRaises(ValueError):
            db.generate_followup_drafts(batch_id)
        self.assertEqual(self.draft_count(), 0)

    def test_source_records_are_not_mutated(self):
        case = self.prepare_case()
        conn = db.get_connection()
        try:
            invoices_before = [
                tuple(row) for row in conn.execute(
                    "SELECT * FROM invoices ORDER BY id"
                )
            ]
            results_before = [
                tuple(row) for row in conn.execute(
                    "SELECT * FROM reconciliation_results ORDER BY id"
                )
            ]
        finally:
            conn.close()
        db.generate_followup_drafts(case["batch_id"])
        conn = db.get_connection()
        try:
            invoices_after = [
                tuple(row) for row in conn.execute(
                    "SELECT * FROM invoices ORDER BY id"
                )
            ]
            results_after = [
                tuple(row) for row in conn.execute(
                    "SELECT * FROM reconciliation_results ORDER BY id"
                )
            ]
        finally:
            conn.close()
        self.assertEqual(invoices_before, invoices_after)
        self.assertEqual(results_before, results_after)

    def test_failed_insert_rolls_back_everything(self):
        self.prepare_case(invoice_count=1)
        second_supplier = self.add_supplier("Gamma Traders", GSTIN_C)
        self.add_invoice(second_supplier, "INV-C-1", total_gst=50_000)
        case_batch = db.get_gstr2b_imports()[0]["Batch"]
        db.reconcile_gstr2b_import(case_batch)

        calls = {"count": 0}
        original = db._insert_followup_draft

        def flaky(conn, plan, run_id, import_id):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("simulated draft insert failure")
            return original(conn, plan, run_id, import_id)

        with mock.patch.object(
            db, "_insert_followup_draft", side_effect=flaky
        ):
            with self.assertRaises(RuntimeError):
                db.generate_followup_drafts(case_batch)

        with db.db() as conn:
            for table in (
                "followup_drafts",
                "followup_draft_invoices",
                "followup_draft_events",
            ):
                self.assertEqual(
                    conn.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0],
                    0,
                    table,
                )


class StatusAndHistoryTests(DatabaseTestCase):
    def test_latest_run_is_used_and_old_drafts_become_stale(self):
        case = self.prepare_case()
        first = db.generate_followup_drafts(case["batch_id"])
        first_draft = first["draft_ids"][0]

        second_run = db.reconcile_gstr2b_import(case["batch_id"])
        preview = db.get_followup_eligibility(case["batch_id"])
        self.assertEqual(preview["run"]["id"], second_run["run_id"])

        second = db.generate_followup_drafts(case["batch_id"])
        self.assertEqual(second["created_drafts"], 1)
        self.assertEqual(self.draft_count(), 2)

        with self.assertRaises(ValueError) as caught:
            db.approve_followup_draft(first_draft, confirmed=True)
        self.assertIn("STALE_RECONCILIATION_RUN", str(caught.exception))
        self.assertEqual(
            db.get_followup_draft(first_draft)["status"], "DRAFT"
        )

    def test_edit_persists_and_records_event(self):
        case = self.prepare_case()
        draft_id = db.generate_followup_drafts(
            case["batch_id"]
        )["draft_ids"][0]
        db.update_followup_draft_text(draft_id, "Please verify these entries.")
        draft = db.get_followup_draft(draft_id)
        self.assertEqual(draft["draft_text"], "Please verify these entries.")
        events = db.get_followup_draft_events(draft_id)
        self.assertEqual([event["Event"] for event in events],
                         ["CREATED", "EDITED"])
        self.assertEqual(events[-1]["To"], "DRAFT")

    def test_approval_requires_confirmation_and_persists(self):
        case = self.prepare_case()
        draft_id = db.generate_followup_drafts(
            case["batch_id"]
        )["draft_ids"][0]
        with self.assertRaises(ValueError):
            db.approve_followup_draft(draft_id, confirmed=False)
        self.assertEqual(db.get_followup_draft(draft_id)["status"], "DRAFT")

        db.approve_followup_draft(draft_id, confirmed=True)
        draft = db.get_followup_draft(draft_id)
        self.assertEqual(draft["status"], "APPROVED")
        events = db.get_followup_draft_events(draft_id)
        self.assertEqual(events[-1]["Event"], "STATUS_CHANGED")
        self.assertEqual(events[-1]["From"], "DRAFT")
        self.assertEqual(events[-1]["To"], "APPROVED")
        self.assertIn("no message sent", events[-1]["Note"].lower())
        with self.assertRaises(ValueError):
            db.update_followup_draft_text(draft_id, "too late")

    def test_skip_persists_and_blocks_approval(self):
        case = self.prepare_case()
        draft_id = db.generate_followup_drafts(
            case["batch_id"]
        )["draft_ids"][0]
        db.skip_followup_draft(draft_id)
        self.assertEqual(db.get_followup_draft(draft_id)["status"], "SKIPPED")
        self.assertEqual(
            db.get_followup_draft_events(draft_id)[-1]["To"], "SKIPPED"
        )
        with self.assertRaises(ValueError):
            db.approve_followup_draft(draft_id, confirmed=True)
        with self.assertRaises(ValueError):
            db.skip_followup_draft(draft_id)

    def test_missing_contact_cannot_be_approved(self):
        case = self.prepare_case(phone=None)
        draft_id = db.generate_followup_drafts(
            case["batch_id"]
        )["draft_ids"][0]
        draft = db.get_followup_draft(draft_id)
        self.assertEqual(draft["status"], "NEEDS_REVIEW")
        self.assertFalse(draft["channel_available"])
        self.assertIn("CONTACT_PHONE_MISSING", draft["review_reasons"])
        with self.assertRaises(ValueError):
            db.approve_followup_draft(draft_id, confirmed=True)

    def test_approval_blocked_after_invoice_invalidated(self):
        case = self.prepare_case()
        draft_id = db.generate_followup_drafts(
            case["batch_id"]
        )["draft_ids"][0]
        db.update_invoice_validation(case["invoice_ids"][0], "invalid")
        with self.assertRaises(ValueError) as caught:
            db.approve_followup_draft(draft_id, confirmed=True)
        self.assertIn("INVOICE_NOT_VALID", str(caught.exception))

    def test_approval_blocked_if_contact_removed(self):
        case = self.prepare_case()
        draft_id = db.generate_followup_drafts(
            case["batch_id"]
        )["draft_ids"][0]
        with db.db() as conn:
            conn.execute(
                "UPDATE suppliers SET contact_phone = NULL WHERE id = ?",
                (case["supplier_a"],),
            )
        with self.assertRaises(ValueError) as caught:
            db.approve_followup_draft(draft_id, confirmed=True)
        self.assertIn("CONTACT_PHONE_REMOVED", str(caught.exception))

    def test_foreign_keys_uniqueness_and_status_checks(self):
        case = self.prepare_case()
        draft_id = db.generate_followup_drafts(
            case["batch_id"]
        )["draft_ids"][0]

        conn = db.get_connection()
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO followup_drafts
                           (supplier_id, reconciliation_run_id,
                            gstr2b_import_id, supplier_name, draft_text,
                            inclusion_reason)
                       VALUES (99999, ?, ?, 'X', 'text', 'reason')""",
                    (case["run_id"], case["batch_id"]),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO followup_drafts
                           (supplier_id, reconciliation_run_id,
                            gstr2b_import_id, supplier_name, draft_text,
                            inclusion_reason, status)
                       VALUES (?, ?, ?, 'X', 'text', 'reason', 'SENT')""",
                    (case["supplier_a"], case["run_id"],
                     case["batch_id"]),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO followup_drafts
                           (supplier_id, reconciliation_run_id,
                            gstr2b_import_id, supplier_name, draft_text,
                            inclusion_reason)
                       VALUES (?, ?, ?, 'Duplicate', 'text', 'reason')""",
                    (case["supplier_a"], case["run_id"],
                     case["batch_id"]),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    """INSERT INTO followup_draft_invoices
                           (draft_id, invoice_id, reconciliation_result_id,
                            invoice_number, invoice_date, tax_period,
                            taxable_value_paise, total_gst_paise, reason)
                       VALUES (99999, ?, 1, 'X', '2026-03-05', '2026-03',
                               0, 0, 'X')""",
                    (case["invoice_ids"][0],),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute(
                    "UPDATE followup_drafts SET status = 'SENT' WHERE id = ?",
                    (draft_id,),
                )
        finally:
            conn.close()

    def test_no_outbound_send_function_exists(self):
        tokens = ("send", "whatsapp", "email", "smtp", "twilio")
        for module in (db, followups):
            offenders = [
                name for name in dir(module)
                if callable(getattr(module, name))
                and any(token in name.lower() for token in tokens)
            ]
            self.assertEqual(offenders, [], module.__name__)
        self.assertFalse(hasattr(followups, "send_message"))
        self.assertFalse(hasattr(db, "send_message"))


class UITests(DatabaseTestCase):
    def test_ui_generates_and_previews_drafts_without_sending(self):
        from streamlit.testing.v1 import AppTest

        case = self.prepare_case()
        at = AppTest.from_file(
            str(Path(__file__).parent / "app.py"), default_timeout=30
        )
        at.run()
        self.assertEqual(at.exception, [])
        at.switch_page("pages/6_followups.py").run()
        self.assertEqual(at.exception, [])
        at.button(key="generate_followups").click().run()
        self.assertEqual(at.exception, [])
        self.assertTrue(
            any("draft(s) created" in item.value for item in at.success),
            [item.value for item in at.success],
        )
        warning_text = " ".join(item.value for item in at.warning)
        self.assertIn("No message has been sent", warning_text)
        approve_buttons = [
            button for button in at.button
            if (button.key or "").startswith("followup_approve_")
        ]
        self.assertTrue(approve_buttons)
        text_areas = [
            area for area in at.text_area
            if (area.key or "").startswith("followup_text_")
        ]
        self.assertTrue(text_areas)
        self.assertEqual(self.draft_count(), 1)


if __name__ == "__main__":
    unittest.main()
