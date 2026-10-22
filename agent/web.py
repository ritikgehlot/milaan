"""Live track-record dashboard: GET /dashboard (page) and GET /api/dashboard (JSON).

Read-only view of what the agent did, built only from the agent database.
The trader's interface stays WhatsApp; this is the ops/CA/judge view.

Served by `python -m agent.server` (http://localhost:5050/dashboard), or on
its own for offline demos: `python -m agent.web` (http://localhost:8090/dashboard).

Access: open on localhost. Through the public tunnel it requires
?t=<DASHBOARD_TOKEN> (set in .env); without a token, remote access is refused.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from flask import Blueprint, Flask, Response, abort, jsonify, request

from . import diagnose, store
from .config import get_settings

bp = Blueprint("dashboard", __name__)
PAGE = Path(__file__).parent / "web" / "dashboard.html"

STATE_LABEL = {
    "PENDING_2B": "Waiting for 2B", "NEEDS_INPUT": "Needs your input", "INVALID": "Invalid",
    "MATCHED": "Matched", "INELIGIBLE": "Not claimable", "QRMP_WAIT": "Quarterly filer · wait",
    "TO_CHASE": "Awaiting approval", "CHASED": "Supplier messaged", "PROMISED": "Supplier promised",
    "RECOVERED": "Recovered", "ESCALATED": "Escalated to trader", "CHANNEL_BLOCKED": "Send failed · call",
    "SKIPPED": "Skipped by trader",
}
CAUSE_LABEL = {
    "MATCHED": "In 2B", "FORMAT_VARIANT": "Number formatted differently", "ROUNDING": "Rounding (≤ ₹1)",
    "AMOUNT_MISMATCH": "Amount mismatch", "ITC_NOT_AVAILABLE": "2B: ITC not available",
    "GSTIN_CONFLICT": "GSTIN conflict", "UNKNOWN_GSTIN": "Unknown GSTIN", "INVALID_GSTIN": "GSTIN fails check digit",
    "QRMP_TIMING": "Quarterly (QRMP) timing", "NON_FILING": "Missing from 2B", "GSTIN_AMEND": "Reported under other GSTIN",
    "BILL_GSTIN_MISPRINT": "Bill misprinted GSTIN",
}


def mask(phone: str | None) -> str:
    if not phone:
        return ""
    return phone[:3] + "•" * max(0, len(phone) - 7) + phone[-4:]


def _authorized() -> bool:
    remote = bool(request.headers.get("Cf-Connecting-Ip") or request.headers.get("X-Forwarded-For")
                  or request.remote_addr not in ("127.0.0.1", "::1", "localhost"))
    if not remote:
        return True
    token = os.environ.get("DASHBOARD_TOKEN", "")
    return bool(token) and request.args.get("t") == token


def _twob_row_for(inv: dict, rows: list[dict]) -> dict | None:
    g = (inv.get("supplier_gstin") or "").upper()
    same = [r for r in rows if (r["supplier_gstin"] or "").upper() == g
            and diagnose.numbers_equivalent(r["invoice_number"], inv.get("invoice_number"))]
    if same:
        return same[-1]
    other = [r for r in rows if diagnose.numbers_equivalent(r["invoice_number"], inv.get("invoice_number"))
             and abs((r["total_gst_paise"] or 0) - (inv.get("total_gst_paise") or 0)) <= 100]
    return other[-1] if other else None


def build_payload(trader: str | None = None) -> dict:
    with store.conn() as c:
        traders = [dict(r) for r in c.execute("SELECT phone, name, lang, created_at FROM agent_traders ORDER BY created_at")]
    if not traders:
        return {"empty": True}
    t = next((x for x in traders if x["phone"] == trader), traders[0])
    tr = t["phone"]
    invs = store.get_invoices(tr)
    rows = store.get_all_2b_rows(tr)
    imports = store.get_2b_imports(tr)
    suppliers = {s["gstin"]: s for s in store.get_suppliers(tr)}
    outbound = store.get_outbound(tr)
    chase_cause: dict[int, str] = {}
    with store.conn() as c:
        for r in c.execute("SELECT name, payload FROM agent_trace WHERE trader_phone=? AND kind='DECISION' "
                           "AND name LIKE 'diagnose:%' ORDER BY id", (tr,)):
            try:
                p = json.loads(r["payload"])
            except ValueError:
                continue
            if p.get("to") == "TO_CHASE":
                chase_cause[p.get("invoice_id")] = r["name"].split(":", 1)[1]
    for i in invs:
        if i["diagnosis"] == "GSTIN_AMEND":
            chase_cause.setdefault(i["id"], "GSTIN_AMEND")
    invoices = []
    for i in invs:
        row = _twob_row_for(i, rows) if imports else None
        sup = suppliers.get(i.get("supplier_gstin") or "", {})
        invoices.append({
            "id": i["id"], "number": i["invoice_number"], "date": i["invoice_date"], "period": i["tax_period"],
            "supplier": i["supplier_name"] or sup.get("name") or "", "gstin": i["supplier_gstin"],
            "taxable": (i["taxable_paise"] or 0) / 100, "gst": (i["total_gst_paise"] or 0) / 100,
            "total": (i["invoice_total_paise"] or 0) / 100, "state": i["state"],
            "state_label": STATE_LABEL.get(i["state"], i["state"]), "cause": i["diagnosis"],
            "cause_label": CAUSE_LABEL.get(i["diagnosis"] or "", i["diagnosis"] or ""),
            "reason": i["state_reason"], "eligibility": i["eligibility"], "source": i["provenance"],
            "pending_field": i["pending_field"], "chases": i["chase_count"], "last_chased": i["last_chased_at"],
            "next_check": i["next_check_at"], "recovered_at": i["recovered_at"], "created": i["created_at"],
            "twob_gst": (row["total_gst_paise"] / 100) if row and row.get("total_gst_paise") is not None else None,
            "twob_number": row["invoice_number"] if row else None,
            "twob_gstin": row["supplier_gstin"] if row else None,
            "twob_period": row.get("return_period") if row else None,
            "chase_cause": chase_cause.get(i["id"]),
            "chase_cause_label": CAUSE_LABEL.get(chase_cause.get(i["id"]) or "", ""),
        })
    follow: dict[str, dict] = {}
    chase_states = {"TO_CHASE", "CHASED", "PROMISED", "RECOVERED", "ESCALATED", "CHANNEL_BLOCKED", "SKIPPED"}
    for inv in invoices:
        if inv["state"] not in chase_states and not inv["chases"]:
            continue
        g = inv["gstin"] or "?"
        sup = suppliers.get(g, {})
        f = follow.setdefault(g, {"supplier": inv["supplier"], "gstin": g, "phone": mask(sup.get("phone")),
                                  "bills": 0, "open_bills": 0, "gst": 0.0, "issues": set(), "states": set(),
                                  "last": None, "next": None, "chases": 0})
        f["bills"] += 1
        f["gst"] += inv["gst"]
        f["issues"].add(inv["chase_cause_label"] or inv["cause_label"] or "")
        f["states"].add(inv["state"])
        f["chases"] = max(f["chases"], inv["chases"] or 0)
        if inv["state"] not in ("RECOVERED", "SKIPPED"):
            f["open_bills"] += 1
        if inv["last_chased"] and (not f["last"] or inv["last_chased"] > f["last"]):
            f["last"] = inv["last_chased"]
        if inv["next_check"] and inv["state"] in ("CHASED", "PROMISED") and (not f["next"] or inv["next_check"] < f["next"]):
            f["next"] = inv["next_check"]
    order = ["ESCALATED", "CHANNEL_BLOCKED", "TO_CHASE", "PROMISED", "CHASED", "SKIPPED", "RECOVERED"]
    followups = []
    for f in follow.values():
        status = next((s for s in order if s in f["states"]), "CHASED")
        followups.append({**f, "issues": sorted(x for x in f["issues"] if x), "states": sorted(f["states"]),
                          "status": status, "status_label": STATE_LABEL.get(status, status)})
    followups.sort(key=lambda x: -x["gst"])
    trace = store.get_trace(tr, limit=250)
    with store.conn() as c:
        msgs = [dict(r) for r in c.execute(
            "SELECT * FROM agent_messages ORDER BY id DESC LIMIT 200")]
    sup_phones = {s.get("phone") for s in suppliers.values() if s.get("phone")}
    convo = [{"who": ("trader" if m["phone"] == tr else "supplier"), "dir": m["direction"], "body": m["body"],
              "media": m["media_type"], "at": m["created_at"], "phone": mask(m["phone"])}
             for m in reversed(msgs) if m["phone"] == tr or m["phone"] in sup_phones]
    return {
        "empty": False, "now": store.now_text(),
        "trader": {"phone": mask(tr), "name": t.get("name") or "Trader", "lang": t.get("lang")},
        "traders": [{"phone": x["phone"], "label": (x.get("name") or "") + " " + mask(x["phone"])} for x in traders],
        "periods": sorted({i["period"] for i in invoices if i["period"]}),
        "invoices": invoices,
        "imports": [{"id": i["id"], "file": i["file_name"], "format": i["source_format"], "periods": i["periods"],
                     "accepted": i["accepted"], "rejected": i["rejected"], "at": i["created_at"]} for i in imports],
        "twob_rows": [{"gstin": r["supplier_gstin"], "supplier": r.get("supplier_name"), "number": r["invoice_number"],
                       "date": r.get("invoice_date"), "return_period": r.get("return_period"),
                       "taxable": (r["taxable_paise"] or 0) / 100 if r.get("taxable_paise") is not None else None,
                       "gst": (r["total_gst_paise"] or 0) / 100, "itc_available": bool(r.get("itc_available", 1)),
                       "import_id": r["import_id"]} for r in rows],
        "followups": followups,
        "outbound": [{"id": o["id"], "to": mask(o["to_phone"]), "gstin": o["supplier_gstin"], "status": o["status"],
                      "attempts": o["attempts"], "error": o["error"], "at": o["created_at"],
                      "invoices": json.loads(o["invoice_ids"] or "[]")} for o in outbound],
        "trace": [{"id": r["id"], "kind": r["kind"], "name": r["name"], "at": r["created_at"], "turn": r["turn_id"],
                   "payload": r["payload"]} for r in trace],
        "conversation": convo[-150:],
    }


@bp.get("/dashboard")
def page():
    if not _authorized():
        abort(403)
    return Response(PAGE.read_text(encoding="utf-8"), mimetype="text/html")


@bp.get("/api/dashboard")
def api():
    if not _authorized():
        abort(403)
    return jsonify(build_payload(request.args.get("trader")))


def main() -> None:
    get_settings()
    app = Flask(__name__)
    app.register_blueprint(bp)
    port = int(os.environ.get("DASHBOARD_PORT", 8090))
    print(f"MILAAN dashboard on http://localhost:{port}/dashboard  (db: {store.db_path()})")
    app.run(host="127.0.0.1", port=port, threaded=True)


if __name__ == "__main__":
    main()
