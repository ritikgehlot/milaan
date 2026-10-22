"""GSTR-2B ingestion for the agent.

Accepts:
* the GST portal's GSTR-2B JSON download (docdata.b2b[].inv[] layout), and
* MILAAN's normalized CSV/XLSX schema (reuses the tested gstr2b.py parser).

The portal JSON parser follows GSTN's published GSTR-2B JSON structure. It
has been tested on synthetic files in that structure, not on a live export;
this is disclosed in the README.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

import gstr2b

from .extract import clean_gstin, money_to_paise


@dataclass
class Parsed2B:
    rows: list[dict]
    rejected: list[dict]
    source_format: str
    file_hash: str
    return_periods: list[str]


def _rtnprd_to_period(value: str | None) -> str | None:
    """'102026' (MMYYYY) -> '2026-10'."""
    if not value:
        return None
    v = str(value).strip()
    if len(v) == 6 and v.isdigit():
        return f"{v[2:]}-{v[:2]}"
    if len(v) == 7 and v[4] == "-":
        return v
    return None


def _dmy_to_iso(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(str(value).strip(), fmt).date().isoformat()
        except ValueError:
            pass
    return None


def _num_to_paise(value) -> int | None:
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return money_to_paise(f"{value:.2f}")
    return money_to_paise(value)


def parse_portal_json(content: bytes) -> tuple[list[dict], list[dict], list[str]]:
    doc = json.loads(content.decode("utf-8-sig"))
    data = doc.get("data", doc)
    return_period = _rtnprd_to_period(data.get("rtnprd"))
    docdata = data.get("docdata") or {}
    # b2b = invoices; b2ba = amended invoices (latest values win in matching)
    b2b = list(docdata.get("b2b") or []) + list(docdata.get("b2ba") or [])
    rows, rejected = [], []
    for supplier in b2b:
        ctin = clean_gstin(supplier.get("ctin"))
        name = supplier.get("trdnm")
        sup_period = _rtnprd_to_period(supplier.get("supprd")) or return_period
        for inv in supplier.get("inv") or []:
            items = inv.get("items") or inv.get("itms") or []
            def total(key):
                if key in inv:
                    return _num_to_paise(inv.get(key))
                acc = 0
                for it in items:
                    det = it.get("itm_det", it)
                    p = _num_to_paise(det.get(key))
                    if p is None:
                        return None
                    acc += p
                return acc
            txval, cgst, sgst, igst = total("txval"), total("cgst"), total("sgst"), total("igst")
            inv_date = _dmy_to_iso(inv.get("dt"))
            row = {
                "supplier_gstin": ctin, "supplier_name": name,
                "invoice_number": str(inv.get("inum") or "").strip() or None,
                "invoice_date": inv_date,
                "tax_period": inv_date[:7] if inv_date else sup_period,
                "return_period": return_period or sup_period,
                "taxable_paise": txval,
                "total_gst_paise": None if None in (cgst, sgst, igst) else cgst + sgst + igst,
                "itc_available": str(inv.get("itcavl", "Y")).upper() != "N",
                "itc_reason": inv.get("rsn") or "",
            }
            missing = [k for k in ("supplier_gstin", "invoice_number", "return_period", "total_gst_paise")
                       if row.get(k) in (None, "")]
            (rejected if missing else rows).append({**row, "missing": missing} if missing else row)
    return rows, rejected, [return_period] if return_period else []


def parse_normalized(content: bytes, file_name: str) -> tuple[list[dict], list[dict], list[str], str]:
    parsed = gstr2b.parse_gstr2b(content, file_name)
    rows, rejected = [], []
    for r in parsed.rows:
        row = {
            "supplier_gstin": clean_gstin(r.supplier_gstin), "supplier_name": None,
            "invoice_number": r.invoice_number, "invoice_date": r.invoice_date,
            "tax_period": (r.invoice_date or "")[:7] or r.tax_period,
            "return_period": r.tax_period,
            "taxable_paise": r.taxable_value, "total_gst_paise": r.total_gst,
            "itc_available": True,
        }
        if r.validation_status == "valid":
            rows.append(row)
        else:
            rejected.append({**row, "errors": [e.code for e in r.errors]})
    periods = sorted({r["return_period"] for r in rows if r["return_period"]})
    return rows, rejected, periods, parsed.source_format


def parse_any(content: bytes, file_name: str, mime: str = "") -> Parsed2B:
    name = (file_name or "").lower()
    fh = gstr2b.sha256_hex(content)
    if name.endswith(".json") or "json" in mime or content[:200].lstrip().startswith((b"{", b"\xef\xbb\xbf{")):
        rows, rejected, periods = parse_portal_json(content)
        return Parsed2B(rows, rejected, "portal_json", fh, periods)
    if not (name.endswith(".csv") or name.endswith(".xlsx")):
        if "spreadsheet" in mime or "excel" in mime:
            file_name = (file_name or "gstr2b") + ".xlsx"
        elif "csv" in mime or "text/plain" in mime:
            file_name = (file_name or "gstr2b") + ".csv"
        else:
            sniff = content[:200].lstrip()
            if sniff.startswith(b"{"):
                rows, rejected, periods = parse_portal_json(content)
                return Parsed2B(rows, rejected, "portal_json", fh, periods)
            if sniff.startswith(b"PK"):
                file_name = "gstr2b.xlsx"
            else:
                file_name = "gstr2b.csv"
    rows, rejected, periods, fmt = parse_normalized(content, file_name)
    return Parsed2B(rows, rejected, fmt, fh, periods)
