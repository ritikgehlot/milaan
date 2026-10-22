"""Deterministic synthetic evaluation for MILAAN's matching workflow.

Run: .venv/bin/python benchmark.py

The benchmark uses only synthetic suppliers, invoices and GSTR-2B rows.
It executes the real validation, parsing, persistence, reconciliation and
follow-up services against a temporary SQLite database. Expected labels
come from the dataset rules below; measured results always come from the
services, never from the labels.

Matching task and positive class
================================
Positive class: a valid purchase invoice whose GSTR-2B natural key
(normalized GSTIN + invoice number + tax period) has exactly one usable
GSTR-2B row and whose total GST equals that row exactly at tolerance 0.
The expected status for that case is MATCHED.

Headline precision/recall uses only clear positives and clear negatives
(amount mismatch, missing, GSTIN difference, period mismatch). Cases
whose correct product outcome is manual review, and invalid invoices,
are excluded from headline precision/recall and reported separately.

Simple baseline
===============
The baseline matches on normalized GSTIN + normalized invoice number
only, ignoring tax period, duplicate candidates and amount differences.
It uses the same rows and the same headline cases. It is intentionally
simple and is not the product's matching policy.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import db
import followups
import gstr2b
import reconciliation

PERIOD = "2026-03"
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

SUPPLIERS = {
    "A": {
        "name": "(Synthetic) Alpha Traders",
        "gstin": GSTIN_A,
        "phone": "9900000011",
    },
    "B": {
        "name": "(Synthetic) Beta Traders",
        "gstin": GSTIN_B,
        "phone": "9900000022",
    },
    "C": {
        "name": "(Synthetic) Gamma Traders",
        "gstin": GSTIN_C,
        "phone": None,
    },
}

INVOICES = [
    {
        "key": "exact_1",
        "supplier": "A",
        "number": "INV-BEN-EXACT-1",
        "date": "2026-03-05",
        "period": PERIOD,
        "taxable": 1_250_000,
        "gst": 225_000,
        "expected": "MATCHED",
        "category": "positive",
        "followup": False,
        "clean_draft": False,
    },
    {
        "key": "exact_2",
        "supplier": "B",
        "number": "INV-BEN-EXACT-2",
        "date": "2026-03-06",
        "period": PERIOD,
        "taxable": 500_000,
        "gst": 90_000,
        "expected": "MATCHED",
        "category": "positive",
        "followup": False,
        "clean_draft": False,
    },
    {
        "key": "exact_3",
        "supplier": "B",
        "number": "INV-BEN-EXACT-3",
        "date": "2026-03-07",
        "period": PERIOD,
        "taxable": 750_000,
        "gst": 135_000,
        "expected": "MATCHED",
        "category": "positive",
        "followup": False,
        "clean_draft": False,
    },
    {
        "key": "amount_1",
        "supplier": "A",
        "number": "INV-BEN-AMOUNT-1",
        "date": "2026-03-08",
        "period": PERIOD,
        "taxable": 1_250_000,
        "gst": 225_000,
        "expected": "AMOUNT_MISMATCH",
        "category": "negative",
        "followup": False,
        "clean_draft": False,
    },
    {
        "key": "amount_2",
        "supplier": "B",
        "number": "INV-BEN-AMOUNT-2",
        "date": "2026-03-09",
        "period": PERIOD,
        "taxable": 250_000,
        "gst": 45_000,
        "expected": "AMOUNT_MISMATCH",
        "category": "negative",
        "followup": False,
        "clean_draft": False,
    },
    {
        "key": "missing_1",
        "supplier": "A",
        "number": "INV-BEN-MISSING-1",
        "date": "2026-03-10",
        "period": PERIOD,
        "taxable": 300_000,
        "gst": 54_000,
        "expected": "MISSING_FROM_2B",
        "category": "negative",
        "followup": True,
        "clean_draft": True,
    },
    {
        "key": "missing_phone",
        "supplier": "C",
        "number": "INV-BEN-MISSING-PHONE",
        "date": "2026-03-11",
        "period": PERIOD,
        "taxable": 500_000,
        "gst": 90_000,
        "expected": "MISSING_FROM_2B",
        "category": "negative",
        "followup": True,
        "clean_draft": False,
    },
    {
        "key": "gstin_diff",
        "supplier": "A",
        "number": "INV-BEN-GSTIN-1",
        "date": "2026-03-12",
        "period": PERIOD,
        "taxable": 400_000,
        "gst": 72_000,
        "expected": "MISSING_FROM_2B",
        "category": "negative",
        "followup": True,
        "clean_draft": True,
    },
    {
        "key": "period_diff",
        "supplier": "A",
        "number": "INV-BEN-PERIOD-1",
        "date": "2026-04-02",
        "period": "2026-04",
        "taxable": 350_000,
        "gst": 63_000,
        "expected": "MISSING_FROM_2B",
        "category": "negative",
        "followup": True,
        "clean_draft": True,
    },
    {
        "key": "number_diff",
        "supplier": "A",
        "number": "INV-BEN-NUMBER-1",
        "date": "2026-03-13",
        "period": PERIOD,
        "taxable": 150_000,
        "gst": 27_000,
        "expected": "NEEDS_REVIEW",
        "category": "ambiguous",
        "followup": False,
        "clean_draft": False,
    },
    {
        "key": "ambiguous",
        "supplier": "A",
        "number": "INV-BEN-AMBIG-1",
        "date": "2026-03-14",
        "period": PERIOD,
        "taxable": 200_000,
        "gst": 36_000,
        "expected": "NEEDS_REVIEW",
        "category": "ambiguous",
        "followup": False,
        "clean_draft": False,
    },
    {
        "key": "dup_purchase_1",
        "supplier": "A",
        "number": "INV-BEN-DUP",
        "date": "2026-03-15",
        "period": PERIOD,
        "taxable": 100_000,
        "gst": 18_000,
        "expected": "NEEDS_REVIEW",
        "category": "ambiguous",
        "followup": False,
        "clean_draft": False,
    },
    {
        "key": "dup_purchase_2",
        "supplier": "A",
        "number": "inv-ben-dup",
        "date": "2026-03-15",
        "period": PERIOD,
        "taxable": 100_000,
        "gst": 18_000,
        "expected": "NEEDS_REVIEW",
        "category": "ambiguous",
        "followup": False,
        "clean_draft": False,
    },
    {
        "key": "dup_2b",
        "supplier": "A",
        "number": "INV-BEN-DUP2B",
        "date": "2026-03-16",
        "period": PERIOD,
        "taxable": 250_000,
        "gst": 45_000,
        "expected": "NEEDS_REVIEW",
        "category": "ambiguous",
        "followup": False,
        "clean_draft": False,
    },
    {
        "key": "invalid",
        "supplier": "C",
        "number": "INV-BEN-INVALID",
        "date": "2026-02-30",
        "period": PERIOD,
        "taxable": 250_000,
        "gst": 45_000,
        "expected": "NOT_ELIGIBLE",
        "category": "invalid",
        "followup": False,
        "clean_draft": False,
    },
]

GSTR2B_ROWS = [
    ("A", "INV-BEN-EXACT-1", "2026-03-05", PERIOD, 1_250_000, 225_000),
    ("A", "INV-BEN-AMOUNT-1", "2026-03-08", PERIOD, 1_250_000, 225_100),
    ("B", "INV-BEN-GSTIN-1", "2026-03-12", PERIOD, 400_000, 72_000),
    ("A", "INV-BEN-NUMBER-2", "2026-03-13", PERIOD, 150_000, 27_000),
    ("A", "INV-BEN-PERIOD-1", "2026-03-14", PERIOD, 350_000, 63_000),
    ("A", "INV-BEN-AMBIG-X", "2026-03-14", PERIOD, 200_000, 36_000),
    ("A", "INV-BEN-AMBIG-Y", "2026-03-14", PERIOD, 200_000, 36_000),
    ("A", "INV-BEN-DUP", "2026-03-15", PERIOD, 100_000, 18_000),
    ("A", "INV-BEN-DUP2B", "2026-03-16", PERIOD, 250_000, 45_000),
    ("A", " inv-ben-dup2b ", "2026-03-16", PERIOD, 250_000, 45_000),
    ("B", "INV-BEN-EXACT-2", "2026-03-06", PERIOD, 500_000, 90_000),
    ("B", "INV-BEN-AMOUNT-2", "2026-03-09", PERIOD, 250_000, 45_050),
    ("B", "INV-BEN-EXACT-3", "2026-03-07", PERIOD, 750_000, 135_000),
    ("C", "INV-BEN-INVALID", "2026-03-17", PERIOD, 250_000, 45_000),
]


def rupees(paise: int) -> str:
    return f"{paise // 100}.{paise % 100:02d}"


def _csv_bytes() -> bytes:
    lines = [_CSV_HEADER]
    for supplier_key, number, date, period, taxable, gst in GSTR2B_ROWS:
        cgst = gst // 2
        sgst = gst - cgst
        lines.append(",".join([
            SUPPLIERS[supplier_key]["gstin"],
            number,
            date,
            period,
            rupees(taxable),
            rupees(cgst),
            rupees(sgst),
            "0.00",
            rupees(gst),
        ]))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _insert_supplier(conn, spec):
    return conn.execute(
        """INSERT INTO suppliers (supplier_name, gstin, contact_phone)
           VALUES (?, ?, ?)""",
        (spec["name"], spec["gstin"], spec["phone"]),
    ).lastrowid


def _insert_invoice(conn, supplier_id, invoice):
    cgst = invoice["gst"] // 2
    sgst = invoice["gst"] - cgst
    return conn.execute(
        """INSERT INTO invoices
               (supplier_id, invoice_number, invoice_date, tax_period,
                taxable_value, gst_rate, cgst, sgst, igst, total_gst,
                invoice_total, source)
           VALUES (?, ?, ?, ?, ?, 18, ?, ?, 0, ?, ?, 'benchmark')""",
        (
            supplier_id,
            invoice["number"],
            invoice["date"],
            invoice["period"],
            invoice["taxable"],
            cgst,
            sgst,
            invoice["gst"],
            invoice["taxable"] + invoice["gst"],
        ),
    ).lastrowid


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _headline_counts(rows, baseline=False):
    tp = fp = fn = tn = 0
    for row in rows:
        expected_positive = row["expected"] == reconciliation.STATUS_MATCHED
        if baseline:
            actual_positive = row["baseline_match"]
        else:
            actual_positive = (
                row["actual"] == reconciliation.STATUS_MATCHED
            )
        if expected_positive and actual_positive:
            tp += 1
        elif expected_positive and not actual_positive:
            fn += 1
        elif not expected_positive and actual_positive:
            fp += 1
        else:
            tn += 1
    return {
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(tp, tp + fn),
    }


def _baseline_matches(invoice, rows_by_supplier):
    gstin = reconciliation.normalize_gstin(
        SUPPLIERS[invoice["supplier"]]["gstin"]
    )
    number = reconciliation.normalize_invoice_number(invoice["number"])
    return any(
        reconciliation.normalize_invoice_number(row.invoice_number) == number
        for row in rows_by_supplier.get(gstin, [])
    )


def run_benchmark() -> dict:
    """Execute the benchmark against a fresh temporary database."""
    tmp = tempfile.mkdtemp(prefix="milaan-benchmark-")
    old_env = os.environ.get("DATABASE_PATH")
    os.environ["DATABASE_PATH"] = str(Path(tmp) / "benchmark.db")
    try:
        db.init_db()
        invoice_ids = {}
        with db.db() as conn:
            supplier_ids = {
                key: _insert_supplier(conn, spec)
                for key, spec in SUPPLIERS.items()
            }
            for invoice in INVOICES:
                invoice_ids[invoice["key"]] = _insert_invoice(
                    conn,
                    supplier_ids[invoice["supplier"]],
                    invoice,
                )
        db.revalidate_all_invoices()

        content = _csv_bytes()
        parsed = gstr2b.parse_gstr2b(content, "benchmark_gstr2b.csv")
        first_import = db.save_gstr2b_import(parsed)
        second_import = db.save_gstr2b_import(parsed)
        batch_id = first_import["import_id"]

        first_run = db.reconcile_gstr2b_import(batch_id)
        repeated_run = db.reconcile_gstr2b_import(batch_id)
        duplicate_batch_run = db.reconcile_gstr2b_import(
            second_import["import_id"]
        )

        first_results = db.get_reconciliation_results(first_run["run_id"])
        repeated_results = db.get_reconciliation_results(
            repeated_run["run_id"]
        )
        results = db.get_reconciliation_results(repeated_run["run_id"])

        result_by_invoice = {
            row["Invoice #"]: row for row in results
        }
        invoice_by_id = {
            invoice_ids[item["key"]]: item for item in INVOICES
        }
        measured_rows = []
        for invoice_id, invoice in invoice_by_id.items():
            result = result_by_invoice[invoice_id]
            measured_rows.append({
                "invoice_id": invoice_id,
                "key": invoice["key"],
                "invoice_number": invoice["number"],
                "category": invoice["category"],
                "expected": invoice["expected"],
                "actual": result["Status"],
                "reason": result["Reasons"],
                "followup_expected": invoice["followup"],
                "clean_draft_expected": invoice["clean_draft"],
            })

        eligibility = db.get_followup_eligibility(batch_id)
        planned_ids = {
            item["invoice_id"]
            for plan in eligibility["plans"]
            for item in plan["invoices"]
        }
        for row in measured_rows:
            row["followup_actual"] = row["invoice_id"] in planned_ids
        generated = db.generate_followup_drafts(batch_id)
        drafts = [
            db.get_followup_draft(draft_id)
            for draft_id in generated["draft_ids"]
        ]
        draft_by_invoice = {}
        for draft in drafts:
            for invoice in db.get_followup_draft_invoices(draft["id"]):
                draft_by_invoice[invoice["Invoice #"]] = draft["status"]
        for row in measured_rows:
            row["draft_status"] = draft_by_invoice.get(row["invoice_id"])

        rows_by_supplier = {}
        for parsed_row in parsed.rows:
            gstin = reconciliation.normalize_gstin(
                parsed_row.supplier_gstin
            )
            rows_by_supplier.setdefault(gstin, []).append(parsed_row)
        for row in measured_rows:
            invoice = next(
                item for item in INVOICES if item["key"] == row["key"]
            )
            row["baseline_match"] = _baseline_matches(
                invoice, rows_by_supplier
            )

        headline = [
            row for row in measured_rows
            if row["category"] in ("positive", "negative")
        ]
        actual = _headline_counts(headline)
        baseline = _headline_counts(headline, baseline=True)

        ambiguous = [
            row for row in measured_rows if row["category"] == "ambiguous"
        ]
        invalid = [
            row for row in measured_rows if row["category"] == "invalid"
        ]
        amount_expected = [
            row for row in measured_rows
            if row["expected"] == reconciliation.STATUS_AMOUNT_MISMATCH
        ]
        amount_detected = [
            row for row in amount_expected
            if row["actual"] == reconciliation.STATUS_AMOUNT_MISMATCH
        ]

        followup_correct = sum(
            row["followup_expected"] == row["followup_actual"]
            for row in measured_rows
        )
        clean_expected = [
            row for row in measured_rows if row["clean_draft_expected"]
        ]
        clean_drafted = [
            row for row in clean_expected
            if row["draft_status"] == followups.STATUS_DRAFT
        ]
        phone_review = [
            row for row in measured_rows
            if row["followup_expected"] and not row["clean_draft_expected"]
        ]

        first_repeated_identical = first_results == repeated_results
        batch_runs = [
            run for run in db.get_reconciliation_runs()
            if run["Batch"] == batch_id
        ]
        duplicate_imports = db.find_gstr2b_imports_by_hash(
            parsed.file_hash
        )

        metrics = {
            "invoices_total": len(INVOICES),
            "gstr2b_rows_total": parsed.total_rows,
            "gstr2b_rows_accepted": parsed.accepted_rows,
            "gstr2b_rows_rejected": parsed.rejected_rows,
            "expected_matches": len([
                row for row in measured_rows
                if row["expected"] == reconciliation.STATUS_MATCHED
            ]),
            "actual_matches": len([
                row for row in measured_rows
                if row["actual"] == reconciliation.STATUS_MATCHED
            ]),
            "headline_cases": len(headline),
            "true_positives": actual["tp"],
            "false_positives": actual["fp"],
            "false_negatives": actual["fn"],
            "true_negatives": actual["tn"],
            "precision": actual["precision"],
            "recall": actual["recall"],
            "exact_match_rate": _ratio(actual["tp"], actual["tp"] + actual["fn"]),
            "amount_mismatch_expected": len(amount_expected),
            "amount_mismatch_detected": len(amount_detected),
            "amount_mismatch_detection_rate": _ratio(
                len(amount_detected), len(amount_expected)
            ),
            "invalid_records_expected": len(invalid),
            "invalid_records_excluded": len([
                row for row in invalid
                if row["actual"] == reconciliation.STATUS_NOT_ELIGIBLE
                and not row["followup_actual"]
            ]),
            "invalid_record_exclusion_rate": _ratio(
                len([
                    row for row in invalid
                    if row["actual"] == reconciliation.STATUS_NOT_ELIGIBLE
                    and not row["followup_actual"]
                ]),
                len(invalid),
            ),
            "ambiguous_cases": len(ambiguous),
            "ambiguous_actual_review": len([
                row for row in ambiguous
                if row["actual"] == reconciliation.STATUS_NEEDS_REVIEW
            ]),
            "ambiguous_auto_accepted": len([
                row for row in ambiguous
                if row["actual"] == reconciliation.STATUS_MATCHED
            ]),
            "followup_decisions_correct": followup_correct,
            "followup_decisions_total": len(measured_rows),
            "followup_decision_accuracy": _ratio(
                followup_correct, len(measured_rows)
            ),
            "clean_followup_invoices_expected": len(clean_expected),
            "clean_followup_invoices_drafted": len(clean_drafted),
            "draft_groups_generated": len(drafts),
            "clean_draft_groups": len([
                draft for draft in drafts
                if draft["status"] == followups.STATUS_DRAFT
            ]),
            "needs_review_draft_groups": len([
                draft for draft in drafts
                if draft["status"] == followups.STATUS_NEEDS_REVIEW
            ]),
            "phone_missing_review_expected": len(phone_review),
            "phone_missing_review_actual": len([
                row for row in phone_review
                if row["draft_status"] == followups.STATUS_NEEDS_REVIEW
            ]),
            "duplicate_import_batches": len(duplicate_imports),
            "duplicate_import_batch_ids_distinct": (
                first_import["import_id"] != second_import["import_id"]
            ),
            "repeated_run_results_identical": first_repeated_identical,
            "first_run_id": first_run["run_id"],
            "repeated_run_id": repeated_run["run_id"],
            "batch_run_history_count": len(batch_runs),
            "duplicate_batch_run_id": duplicate_batch_run["run_id"],
            "baseline_true_positives": baseline["tp"],
            "baseline_false_positives": baseline["fp"],
            "baseline_false_negatives": baseline["fn"],
            "baseline_true_negatives": baseline["tn"],
            "baseline_precision": baseline["precision"],
            "baseline_recall": baseline["recall"],
            "baseline_ambiguous_auto_accepts": len([
                row for row in ambiguous if row["baseline_match"]
            ]),
            "ocr_accuracy": None,
            "extraction_pipeline": False,
        }
        return {
            "dataset": {
                "suppliers": len(SUPPLIERS),
                "invoices": len(INVOICES),
                "gstr2b_rows": len(GSTR2B_ROWS),
                "description": (
                    "Synthetic exact, mismatch, missing, invalid, "
                    "duplicate and ambiguous cases"
                ),
            },
            "metrics": metrics,
            "per_invoice": measured_rows,
        }
    finally:
        if old_env is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = old_env
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    print(json.dumps(run_benchmark(), indent=2))
