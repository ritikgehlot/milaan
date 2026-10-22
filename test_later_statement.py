"""Later-statement evidence checks for approved follow-up drafts."""

from test_e2e import WorkflowTestCase, GSTIN_A, gstr2b_row
import db


class LaterStatementVerificationTests(WorkflowTestCase):
    def _approved_missing_draft(self):
        case = self.missing_and_matched_case()
        draft_id = db.generate_followup_drafts(case["batch_id"])["draft_ids"][0]
        db.approve_followup_draft(draft_id, confirmed=True)
        return case, draft_id

    def test_unique_later_row_is_recorded_without_mutating_source_records(self):
        case, draft_id = self._approved_missing_draft()
        later, _, _ = self.import_rows([
            gstr2b_row(
                GSTIN_A,
                "INV-A-MISSING",
                tax_period="2026-04",
            )
        ], "later.csv")

        before_invoice = db.get_invoice(case["missing_invoice"])
        before_results = db.get_reconciliation_results(case["run_id"])
        rows = db.verify_followup_with_later_import(
            draft_id, later["import_id"]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["outcome"], "PRESENT")
        self.assertEqual(rows[0]["later_import_id"], later["import_id"])
        self.assertEqual(db.get_invoice(case["missing_invoice"]), before_invoice)
        self.assertEqual(db.get_reconciliation_results(case["run_id"]), before_results)
        self.assertEqual(db.get_followup_draft(draft_id)["status"], "APPROVED")
        self.assertEqual(
            db.verify_followup_with_later_import(draft_id, later["import_id"]),
            rows,
        )
        self.assertEqual(len(db.get_followup_verifications(draft_id)), 1)

    def test_duplicates_and_amount_differences_are_not_silently_accepted(self):
        case, draft_id = self._approved_missing_draft()
        later, _, _ = self.import_rows([
            gstr2b_row(GSTIN_A, "INV-A-MISSING", total_gst=225_100,
                       taxable=1_250_000, tax_period="2026-04"),
        ], "later-mismatch.csv")
        mismatch = db.verify_followup_with_later_import(
            draft_id, later["import_id"]
        )[0]
        self.assertEqual(mismatch["outcome"], "AMOUNT_MISMATCH")
        self.assertEqual(mismatch["amount_difference_paise"], -100)

        duplicate, _, _ = self.import_rows([
            gstr2b_row(GSTIN_A, "INV-A-MISSING", tax_period="2026-04"),
            gstr2b_row(GSTIN_A, "INV-A-MISSING", tax_period="2026-05"),
        ], "later-duplicate.csv")
        ambiguous = db.verify_followup_with_later_import(
            draft_id, duplicate["import_id"]
        )[0]
        self.assertEqual(ambiguous["outcome"], "NEEDS_REVIEW")
        self.assertIsNone(ambiguous["later_row_id"])

    def test_missing_row_is_scoped_to_selected_batch_and_requires_approved_draft(self):
        case, draft_id = self._approved_missing_draft()
        other, _, _ = self.import_rows([
            gstr2b_row(GSTIN_A, "OTHER-INVOICE", tax_period="2026-04"),
        ], "later-no-match.csv")
        result = db.verify_followup_with_later_import(
            draft_id, other["import_id"]
        )[0]
        self.assertEqual(result["outcome"], "NOT_FOUND")
        self.assertIn("does not establish supplier non-filing", result["reason"])
        with self.assertRaisesRegex(ValueError, "other than the original"):
            db.verify_followup_with_later_import(draft_id, case["batch_id"])

    def test_unapproved_draft_cannot_be_verified(self):
        case = self.missing_and_matched_case()
        draft_id = db.generate_followup_drafts(case["batch_id"])["draft_ids"][0]
        other, _, _ = self.import_rows([
            gstr2b_row(GSTIN_A, "INV-A-MISSING", tax_period="2026-04"),
        ], "later-before-approval.csv")
        with self.assertRaisesRegex(ValueError, "Only approved"):
            db.verify_followup_with_later_import(draft_id, other["import_id"])
