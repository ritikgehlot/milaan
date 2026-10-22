"""Reset and seed the isolated synthetic judge-demo database.

Run from anywhere:
    .venv/bin/python demo/setup_demo.py

This touches only demo/milaan_demo.db and demo/DEMO_GSTR2B.csv. It never
opens milaan.db. Every supplier, GSTIN, phone number and amount below is
fabricated for the demonstration. No message is sent.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DEMO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = DEMO_DIR.parent
DEMO_DB = DEMO_DIR / "milaan_demo.db"
DEMO_CSV = DEMO_DIR / "DEMO_GSTR2B.csv"

if (
    DEMO_DB.name != "milaan_demo.db"
    or DEMO_DB.parent != DEMO_DIR
    or DEMO_DB.resolve().parent != DEMO_DIR
):
    raise RuntimeError("refusing unsafe demo database path")

os.environ["DATABASE_PATH"] = str(DEMO_DB)
sys.path.insert(0, str(PROJECT_ROOT))

import db  # noqa: E402

_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _checksum(first_14: str) -> str:
    total = 0
    for position, character in enumerate(first_14):
        value = _CHARSET.index(character.upper())
        product = value * (1 if position % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _CHARSET[(36 - total % 36) % 36]


def make_gstin(first_14: str) -> str:
    return first_14 + _checksum(first_14)


GSTIN_EXACT = make_gstin("27AAACS1234A1Z")
GSTIN_AMOUNT = make_gstin("29AAACB5678B1Z")
GSTIN_INVALID = make_gstin("24AAACD9012C1Z")
GSTIN_DRAFT = make_gstin("27AAACE3456E1Z")

SUPPLIERS = [
    ("(Synthetic) Meridian Components", GSTIN_EXACT, "9900000101"),
    ("(Synthetic) Northstar Supplies", GSTIN_AMOUNT, "9900000102"),
    ("(Synthetic) Orbit Packaging", GSTIN_INVALID, "9900000103"),
    ("(Synthetic) Solstice Traders", GSTIN_DRAFT, "9900000104"),
]

INVOICES = [
    {
        "gstin": GSTIN_EXACT,
        "number": "DEMO-EXACT-001",
        "date": "2026-03-04",
        "period": "2026-03",
        "taxable": 1_000_000,
        "gst": 180_000,
    },
    {
        "gstin": GSTIN_EXACT,
        "number": "DEMO-MISSING-001",
        "date": "2026-03-06",
        "period": "2026-03",
        "taxable": 1_500_000,
        "gst": 270_000,
    },
    {
        "gstin": GSTIN_AMOUNT,
        "number": "DEMO-AMOUNT-001",
        "date": "2026-03-07",
        "period": "2026-03",
        "taxable": 2_000_000,
        "gst": 360_000,
    },
    {
        "gstin": GSTIN_INVALID,
        "number": "DEMO-INVALID-001",
        "date": "2026-02-30",
        "period": "2026-03",
        "taxable": 500_000,
        "gst": 90_000,
    },
    {
        "gstin": GSTIN_DRAFT,
        "number": "DEMO-MISSING-002",
        "date": "2026-03-09",
        "period": "2026-03",
        "taxable": 800_000,
        "gst": 144_000,
    },
]

GSTR2B_ROWS = [
    (
        GSTIN_EXACT,
        "DEMO-EXACT-001",
        "2026-03-04",
        "2026-03",
        "10000.00",
        "900.00",
        "900.00",
        "0.00",
        "1800.00",
    ),
    (
        GSTIN_AMOUNT,
        "DEMO-AMOUNT-001",
        "2026-03-07",
        "2026-03",
        "20000.00",
        "1825.00",
        "1825.00",
        "0.00",
        "3650.00",
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

CSV_HEADER = (
    "supplier_gstin,invoice_number,invoice_date,tax_period,"
    "taxable_value,cgst,sgst,igst,total_gst"
)


def write_demo_csv() -> None:
    lines = [CSV_HEADER]
    lines.extend(",".join(row) for row in GSTR2B_ROWS)
    DEMO_CSV.write_text("\n".join(lines) + "\n", encoding="utf-8")


def reset_demo_database() -> None:
    if db.get_database_path() != DEMO_DB.resolve():
        raise RuntimeError(
            "refusing to reset demo: DATABASE_PATH does not resolve to "
            "demo/milaan_demo.db"
        )
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(DEMO_DB) + suffix)
        if candidate.exists():
            candidate.unlink()
    db.init_db()

    with db.db() as conn:
        supplier_ids = {}
        for name, gstin, phone in SUPPLIERS:
            supplier_ids[gstin] = conn.execute(
                """INSERT INTO suppliers
                       (supplier_name, gstin, contact_phone)
                   VALUES (?, ?, ?)""",
                (name, gstin, phone),
            ).lastrowid

        for invoice in INVOICES:
            cgst = invoice["gst"] // 2
            sgst = invoice["gst"] - cgst
            conn.execute(
                """INSERT INTO invoices
                       (supplier_id, invoice_number, invoice_date,
                        tax_period, taxable_value, gst_rate, cgst, sgst,
                        igst, total_gst, invoice_total, source)
                   VALUES (?, ?, ?, ?, ?, 18, ?, ?, 0, ?, ?, 'demo-seed')""",
                (
                    supplier_ids[invoice["gstin"]],
                    invoice["number"],
                    invoice["date"],
                    invoice["period"],
                    invoice["taxable"],
                    cgst,
                    sgst,
                    invoice["gst"],
                    invoice["taxable"] + invoice["gst"],
                ),
            )
    db.revalidate_all_invoices()


def main() -> None:
    write_demo_csv()
    reset_demo_database()
    summary = db.get_validation_summary()
    totals = db.get_totals()
    print(f"Demo database reset: {DEMO_DB}")
    print(f"Demo GSTR-2B file:   {DEMO_CSV}")
    print(
        f"Seeded {len(SUPPLIERS)} synthetic suppliers and "
        f"{totals['invoice_count']} invoices: "
        f"{summary['valid']} valid, "
        f"{summary['needs_review']} needs review, "
        f"{summary['invalid']} invalid."
    )
    print("No GSTR-2B batch, reconciliation run, draft or delivery exists.")
    print("No message has been sent.")
    print(
        "\nExpected demo flow: 2 of 3 GSTR-2B rows accepted; 1 matched, "
        "1 amount mismatch, 2 missing, 1 not eligible; 2 clean drafts."
    )
    print("\nStart the isolated demo with:\n")
    print(
        f"  DATABASE_PATH='{DEMO_DB}' "
        ".venv/bin/streamlit run app.py"
    )


if __name__ == "__main__":
    main()
