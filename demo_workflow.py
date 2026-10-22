"""Deterministic synthetic demo workspace.

The demo is built through the real pipeline: the invoices are validated by
``validation``/``db``, the GSTR-2B CSV is parsed by ``gstr2b``, stored by
``db.save_gstr2b_import`` and reconciled by ``db.reconcile_gstr2b_import``.
Nothing is hardcoded in the UI and no message is sent.

Expected result on a clean database:

* 6 purchase invoices: 5 valid, 1 invalid (non-existent 31 April date)
* 1 GSTR-2B batch: 2 accepted rows, 1 rejected row
* 1 reconciliation run: 1 matched, 1 amount mismatch, 3 missing,
  1 not eligible (the invalid invoice)
"""

from __future__ import annotations

import csv
import io

import db
import gstr2b

DEMO_FILE_NAME = "MILAAN_DEMO_GSTR2B.csv"
DEMO_HEADER = (
    "supplier_gstin",
    "invoice_number",
    "invoice_date",
    "tax_period",
    "taxable_value",
    "cgst",
    "sgst",
    "igst",
    "total_gst",
)


def _rupees(paise: int) -> str:
    return f"{paise // 100}.{paise % 100:02d}"


def demo_gstr2b_content() -> bytes:
    """Build a synthetic GSTR-2B CSV that exercises match and mismatch."""
    invoices = {
        (row[0], row[1], row[3]): row for row in db.DEMO_INVOICES
    }
    exact = invoices[("27AAACS1234A1Z2", "INV-DEMO-001", "2026-03")]
    mismatch = invoices[("29AAACB5678B1ZP", "INV-DEMO-101", "2026-03")]

    rows = [
        (
            exact[0],
            exact[1],
            exact[2],
            exact[3],
            _rupees(exact[4]),
            _rupees(exact[6]),
            _rupees(exact[7]),
            _rupees(exact[8]),
            _rupees(exact[9]),
        ),
        (
            mismatch[0],
            mismatch[1],
            mismatch[2],
            mismatch[3],
            _rupees(mismatch[4]),
            _rupees(mismatch[6] + 2_500),
            _rupees(mismatch[7] + 2_500),
            _rupees(mismatch[8]),
            _rupees(mismatch[9] + 5_000),
        ),
        (
            "27AAACS1234A1ZZ",
            "DEMO-BAD-ROW",
            "2026-03-08",
            "2026-03",
            "1000.00",
            "90.00",
            "90.00",
            "0.00",
            "-180.00",
        ),
    ]

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(DEMO_HEADER)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def load_demo_workspace() -> dict:
    """Seed, validate, import and reconcile the synthetic demo idempotently.

    Repeated calls reuse the same content hash and existing run instead of
    creating duplicate batches or runs. Drafts are not generated here; the
    user decides whether to prepare follow-ups.
    """
    seeded = db.seed_demo_data()
    validation_entries = db.revalidate_all_invoices()
    validation_counts = {status: 0 for status in ("valid", "invalid",
                                                  "needs_review")}
    for entry in validation_entries:
        validation_counts[entry["result"].status] += 1

    content = demo_gstr2b_content()
    file_hash = gstr2b.sha256_hex(content)
    existing = db.find_gstr2b_imports_by_hash(file_hash)
    imported = False
    if existing:
        batch_id = existing[0]["id"]
    else:
        parsed = gstr2b.parse_gstr2b(content, DEMO_FILE_NAME)
        saved = db.save_gstr2b_import(parsed)
        batch_id = saved["import_id"]
        imported = True

    run = db.get_latest_reconciliation_run_for_import(batch_id)
    reconciled = False
    if run is None:
        summary = db.reconcile_gstr2b_import(batch_id, 0)
        run_id = summary["run_id"]
        reconciled = True
    else:
        run_id = run["id"]

    return {
        "seeded": seeded,
        "validation": validation_counts,
        "batch_id": batch_id,
        "imported": imported,
        "run_id": run_id,
        "reconciled": reconciled,
    }
