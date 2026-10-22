"""Reconciliation + diagnosis: for every invoice, decide WHY it is absent and
WHAT the right action is — including deliberately doing nothing.

Deterministic on purpose: this layer decides who may be contacted about
money, so every decision is rule-based, explainable, and traced with its
evidence. The LLM never decides whom to accuse.

Causes:
  MATCHED            present in 2B (exact)
  FORMAT_VARIANT     present, invoice number formatted differently -> no chase
  ROUNDING           present, GST differs within tolerance -> no chase
  AMOUNT_MISMATCH    present, GST differs beyond tolerance -> ask supplier to amend
  ITC_NOT_AVAILABLE  2B itself marks ITC unavailable -> INELIGIBLE, never chase
  GSTIN_CONFLICT     same bill found under a different GSTIN -> ask trader first
  UNKNOWN_GSTIN      GSTIN never seen anywhere -> confirm with trader, never message a stranger
  QRMP_TIMING        quarterly filer, invoice legitimately absent -> wait, re-check
  NON_FILING         genuinely absent -> chase supplier (after trader approval)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

import validation

from . import store

DEFAULT_TOLERANCE_PAISE = 100  # ₹1 rounding tolerance
QUARTER_END = {"03", "06", "09", "12"}
ACTIVE_STATES = {store.PENDING_2B, store.TO_CHASE, store.CHASED, store.PROMISED,
                 store.QRMP_WAIT, store.CHANNEL_BLOCKED}


def norm_number(value: str | None) -> str:
    return " ".join((value or "").split()).upper()


def loose_number(value: str | None) -> str:
    """'INV/2026-27/0042' and 'INV-2026-27-42' -> 'INV202627 42'-ish canonical form."""
    text = re.sub(r"[^A-Z0-9]+", " ", (value or "").upper()).strip()
    parts = [p.lstrip("0") or "0" if p.isdigit() else p for p in text.split()]
    return "".join(parts)


def numbers_equivalent(a: str | None, b: str | None) -> bool:
    """Same bill, different formatting: 'SBP/26-27/0412' ~ 'SBP-412' ~ 'sbp 412'.

    Requires the same leading letter prefix (if both have one) and the same
    final serial number. Callers additionally require the GST amount to agree,
    so this never merges two genuinely different bills of one supplier.
    """
    la, lb = loose_number(a), loose_number(b)
    if not la or not lb:
        return False
    if la == lb:
        return True
    pa = re.match(r"[A-Z]*", (a or "").upper().strip()).group(0)
    pb = re.match(r"[A-Z]*", (b or "").upper().strip()).group(0)
    if pa and pb and pa != pb:
        return False
    sa, sb = re.findall(r"\d+", a or ""), re.findall(r"\d+", b or "")
    return bool(sa and sb and int(sa[-1]) == int(sb[-1]))


def same_pan(g1: str | None, g2: str | None) -> bool:
    return bool(g1 and g2 and len(g1) == 15 and len(g2) == 15 and g1[2:12] == g2[2:12])


def char_distance(g1: str, g2: str) -> int:
    if len(g1) != len(g2):
        return 99
    return sum(1 for a, b in zip(g1, g2) if a != b)


def is_qrmp(history: list[str], hint: str | None) -> bool:
    if (hint or "").lower() == "qrmp":
        return True
    if (hint or "").lower() == "monthly":
        return False
    return len(history) >= 2 and all(p[-2:] in QUARTER_END for p in history)


def quarter_end_period(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    qm = ((m - 1) // 3 + 1) * 3
    return f"{y:04d}-{qm:02d}"


def twob_available_date(period: str) -> str:
    """GSTR-2B for a period is generated on the 14th of the following month."""
    y, m = int(period[:4]), int(period[5:7])
    m += 1
    if m == 13:
        y, m = y + 1, 1
    return f"{y:04d}-{m:02d}-14 09:00:00"


def itc_deadline(period: str) -> str:
    """§16(4): 30 November following the end of the financial year."""
    y, m = int(period[:4]), int(period[5:7])
    fy_end_year = y + 1 if m >= 4 else y
    return f"{fy_end_year}-11-30"


@dataclass
class Decision:
    invoice_id: int
    cause: str
    new_state: str
    evidence: dict = field(default_factory=dict)
    explanation: str = ""


def diagnose_invoice(inv: dict, rows: list[dict], *, history: list[str], filing_hint: str | None,
                     known_gstins: set[str], latest_return_period: str | None,
                     tolerance: int = DEFAULT_TOLERANCE_PAISE) -> Decision | None:
    """Pure function: one invoice + all 2B rows -> decision (or None = no change)."""
    iid = inv["id"]
    gstin = (inv.get("supplier_gstin") or "").upper()
    number = norm_number(inv.get("invoice_number"))
    period = inv.get("tax_period") or ""
    amount = inv.get("total_gst_paise") or 0
    prior = inv["state"]
    was_chased = prior in (store.CHASED, store.PROMISED, store.CHANNEL_BLOCKED) or inv.get("chase_count", 0) > 0

    if latest_return_period is None or latest_return_period < period:
        return None  # no 2B yet covers this invoice's period

    def present(cause: str, row: dict, note: str) -> Decision:
        state = store.RECOVERED if was_chased else store.MATCHED
        return Decision(iid, cause, state, {"row_id": row["id"], "row_gstin": row["supplier_gstin"],
                                             "row_number": row["invoice_number"],
                                             "row_gst_paise": row["total_gst_paise"],
                                             "return_period": row.get("return_period")}, note)

    exact = [r for r in rows if (r["supplier_gstin"] or "").upper() == gstin
             and norm_number(r["invoice_number"]) == number]
    if exact:
        row = exact[-1]
        if not row.get("itc_available", 1):
            return Decision(iid, "ITC_NOT_AVAILABLE", store.INELIGIBLE, {"row_id": row["id"]},
                            "2B marks ITC as not available for this invoice.")
        diff = amount - (row["total_gst_paise"] or 0)
        if diff == 0:
            return present("MATCHED", row, "Found in 2B with the same GST amount.")
        if abs(diff) <= tolerance:
            return present("ROUNDING", row, f"Found in 2B; GST differs by {abs(diff)} paise (rounding).")
        return Decision(iid, "AMOUNT_MISMATCH", store.TO_CHASE,
                        {"row_id": row["id"], "difference_paise": diff,
                         "row_gst_paise": row["total_gst_paise"]},
                        "Supplier reported a different GST amount; ask them to amend.")

    same_supplier = [r for r in rows if (r["supplier_gstin"] or "").upper() == gstin]
    variants = [r for r in same_supplier if numbers_equivalent(r["invoice_number"], inv.get("invoice_number"))
                and abs(amount - (r["total_gst_paise"] or 0)) <= tolerance]
    if len(variants) == 1:
        return present("FORMAT_VARIANT", variants[0],
                       f"Found as '{variants[0]['invoice_number']}' — same bill, number formatted differently.")

    elsewhere = [r for r in rows if (r["supplier_gstin"] or "").upper() != gstin
                 and numbers_equivalent(r["invoice_number"], inv.get("invoice_number"))
                 and abs(amount - (r["total_gst_paise"] or 0)) <= tolerance
                 and (same_pan(gstin, r["supplier_gstin"]) or char_distance(gstin, r["supplier_gstin"] or "") <= 2)]
    if elsewhere:
        row = elsewhere[-1]
        return Decision(iid, "GSTIN_CONFLICT", store.NEEDS_INPUT,
                        {"bill_gstin": gstin, "twob_gstin": row["supplier_gstin"], "row_id": row["id"],
                         "bill_gstin_checksum_ok": validation.validate_gstin(gstin).is_format_valid},
                        "The same bill appears in 2B under a different GSTIN; trader must confirm which is right.")

    if not validation.validate_gstin(gstin).is_format_valid:
        return Decision(iid, "INVALID_GSTIN", store.NEEDS_INPUT, {"bill_gstin": gstin},
                        "GSTIN fails the check digit and no 2B row matches; never message anyone on it.")

    if gstin not in known_gstins:
        return Decision(iid, "UNKNOWN_GSTIN", store.NEEDS_INPUT, {"bill_gstin": gstin},
                        "GSTIN never seen in any 2B or confirmed by the trader; confirm before contacting.")

    if is_qrmp(history, filing_hint) and latest_return_period < quarter_end_period(period):
        qe = quarter_end_period(period)
        return Decision(iid, "QRMP_TIMING", store.QRMP_WAIT,
                        {"history": history, "quarter_end": qe, "recheck_at": twob_available_date(qe)},
                        f"Quarterly (QRMP) filer; invoice can legitimately appear only in the {qe} 2B.")

    if prior in (store.CHASED, store.PROMISED, store.CHANNEL_BLOCKED):
        return None  # already in the chase ladder; scheduler owns it
    return Decision(iid, "NON_FILING", store.TO_CHASE,
                    {"history": history, "latest_return_period": latest_return_period,
                     "itc_deadline": itc_deadline(period)},
                    "Not in any 2B imported so far; supplier has not reported it.")


def run(trader: str, *, turn_id: str | None = None,
        tolerance: int = DEFAULT_TOLERANCE_PAISE) -> dict:
    """Reconcile all active invoices of a trader against every 2B imported so far."""
    rows = store.get_all_2b_rows(trader)
    imports = store.get_2b_imports(trader)
    periods = sorted({p for i in imports for p in (i["periods"] or "").split(",") if p})
    latest = periods[-1] if periods else None
    known = {r["supplier_gstin"] for r in rows} | {
        s["gstin"] for s in store.get_suppliers(trader) if s.get("consent") or s.get("filing_hint")}
    known |= set(_history_gstins(trader))
    suppliers = {s["gstin"]: s for s in store.get_suppliers(trader)}
    decisions: list[Decision] = []
    candidates = store.get_invoices(trader, states=ACTIVE_STATES)
    for inv in candidates:
        if inv["eligibility"] != "ELIGIBLE":
            continue
        gstin = (inv.get("supplier_gstin") or "").upper()
        d = diagnose_invoice(inv, rows, history=store.supplier_history(trader, gstin),
                             filing_hint=(suppliers.get(gstin) or {}).get("filing_hint"),
                             known_gstins=known, latest_return_period=latest, tolerance=tolerance)
        if d is None:
            continue
        decisions.append(d)
        update = {"state": d.new_state, "diagnosis": d.cause, "state_reason": d.explanation}
        if d.new_state == store.QRMP_WAIT:
            update["next_check_at"] = d.evidence["recheck_at"]
        if d.new_state == store.RECOVERED:
            update["recovered_at"] = store.now_text()
        if d.new_state == store.NEEDS_INPUT:
            update["pending_field"] = d.cause.lower()
        store.update_invoice(inv["id"], **update)
        store.trace(trader, turn_id, "DECISION", f"diagnose:{d.cause}",
                    {"invoice_id": inv["id"], "invoice_number": inv["invoice_number"],
                     "from": inv["state"], "to": d.new_state, "evidence": d.evidence,
                     "why": d.explanation})
    return summarize(trader, decisions)


def _history_gstins(trader: str) -> list[str]:
    with store.conn() as c:
        return [r[0] for r in c.execute(
            "SELECT DISTINCT supplier_gstin FROM agent_2b_history WHERE trader_phone=?", (trader,))]


def summarize(trader: str, decisions: list[Decision] | None = None) -> dict:
    invs = store.get_invoices(trader)
    by_state: dict[str, dict] = {}
    for inv in invs:
        b = by_state.setdefault(inv["state"], {"count": 0, "gst_paise": 0})
        b["count"] += 1
        b["gst_paise"] += inv.get("total_gst_paise") or 0
    decided: dict[str, list[int]] = {}
    for d in decisions or []:
        decided.setdefault(d.cause, []).append(d.invoice_id)
    return {"by_state": by_state, "decisions": decided,
            "total_invoices": len(invs),
            "terminal": sum(1 for i in invs if i["state"] in store.TERMINAL)}
