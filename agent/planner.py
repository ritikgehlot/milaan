"""LLM planner for free-form trader messages (Hindi / Hinglish / English, text or voice).

The planner can look things up and change bookkeeping (supplier phone,
filing pattern, skip a bill, prepare drafts). It has NO tool that sends a
message to a third party: sending happens only through the deterministic
'haan' confirmation path in core.py.
"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta

from . import diagnose, messages as M, store
from .llm import LLMUnavailable

SYSTEM = """You are MILAAN, a WhatsApp assistant for a small GST-registered trader in Rajasthan, India.
Your job: help him recover input tax credit (ITC) that is lost when suppliers don't report his purchase
bills in their returns (so they don't appear in his GSTR-2B).

How to behave:
- Reply in the trader's language: Hindi (Devanagari) if he writes Hindi or Hinglish, English if he writes English.
  Keep it short — 1 to 6 lines, WhatsApp style. Rupee amounts like ₹12,400.
- Use tools to get facts. Never invent numbers, bills, suppliers or dates.
- Tag facts by source: [📄बिल से] from his bills, [🧾2B से] from his GSTR-2B, [📤मैंने किया] for actions you
  actually completed in a tool call this turn, [⚖️GST नियम—CA से पुष्टि करें] for general GST rules.
- You CANNOT send messages to suppliers. If he wants to send, call prepare_followups; it shows him the drafts
  and he must reply 'haan' to send. Never say a message was sent unless a tool result says so.
- You never file returns, never log into the GST portal, and give no tax advice beyond general rules;
  ambiguous eligibility questions go to his CA.
- If something needs his input (a missing supplier number, an unclear bill), ask one clear question.
"""

TOOLS = [
    {"type": "function", "function": {
        "name": "get_status", "description": "Counts and rupee totals of his bills by state.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "list_bills", "description": "List bills, optionally filtered by state or supplier name.",
        "parameters": {"type": "object", "properties": {
            "state": {"type": "string", "description": "MATCHED, TO_CHASE, CHASED, PROMISED, QRMP_WAIT, "
                                                       "NEEDS_INPUT, PENDING_2B, RECOVERED, ESCALATED, INELIGIBLE"},
            "supplier": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "explain_bill", "description": "Why a specific bill is in its current state, with evidence.",
        "parameters": {"type": "object", "properties": {"invoice_number": {"type": "string"}},
                       "required": ["invoice_number"]}}},
    {"type": "function", "function": {
        "name": "set_supplier_phone", "description": "Save a supplier's WhatsApp number the trader gave.",
        "parameters": {"type": "object", "properties": {
            "supplier": {"type": "string", "description": "supplier name or last 4 chars of GSTIN"},
            "phone": {"type": "string"}}, "required": ["supplier", "phone"]}}},
    {"type": "function", "function": {
        "name": "set_supplier_filing", "description": "Record that a supplier files quarterly (QRMP) or monthly.",
        "parameters": {"type": "object", "properties": {
            "supplier": {"type": "string"}, "pattern": {"type": "string", "enum": ["qrmp", "monthly"]}},
            "required": ["supplier", "pattern"]}}},
    {"type": "function", "function": {
        "name": "skip_bill", "description": "Trader does not want this bill chased.",
        "parameters": {"type": "object", "properties": {"invoice_number": {"type": "string"}},
                       "required": ["invoice_number"]}}},
    {"type": "function", "function": {
        "name": "recheck", "description": "Re-run reconciliation and diagnosis against all GSTR-2Bs received.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "prepare_followups",
        "description": "Prepare supplier follow-up drafts and ask the trader for approval. Does NOT send.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "call_script", "description": "Phone-call script for a supplier with the exact bill list.",
        "parameters": {"type": "object", "properties": {"supplier": {"type": "string"}},
                       "required": ["supplier"]}}},
]


def _find_invoice(trader: str, number: str) -> dict | None:
    target = diagnose.loose_number(number)
    hits = [i for i in store.get_invoices(trader) if diagnose.loose_number(i.get("invoice_number")) == target]
    if not hits:
        hits = [i for i in store.get_invoices(trader)
                if target and diagnose.loose_number(i.get("invoice_number")).endswith(target)]
    return hits[-1] if len(hits) >= 1 else None


def _brief(inv: dict) -> dict:
    return {"invoice_number": inv["invoice_number"], "supplier": inv.get("supplier_name"),
            "date": inv.get("invoice_date"), "gst": M.rupees(inv.get("total_gst_paise")),
            "state": inv["state"], "why": inv.get("state_reason") or inv.get("diagnosis")}


def execute_tool(agent, trader: str, name: str, args: dict, turn: str) -> dict:
    if name == "get_status":
        invs = store.get_invoices(trader)
        out: dict = {}
        for i in invs:
            b = out.setdefault(i["state"], {"bills": 0, "gst": 0})
            b["bills"] += 1
            b["gst"] += i.get("total_gst_paise") or 0
        return {k: {"bills": v["bills"], "gst": M.rupees(v["gst"])} for k, v in out.items()} | {
            "total_bills": len(invs)}
    if name == "list_bills":
        invs = store.get_invoices(trader, states=[args["state"].upper()] if args.get("state") else None)
        if args.get("supplier"):
            s = args["supplier"].lower()
            invs = [i for i in invs if s in (i.get("supplier_name") or "").lower()]
        return {"bills": [_brief(i) for i in invs[:25]], "count": len(invs)}
    if name == "explain_bill":
        inv = _find_invoice(trader, args.get("invoice_number", ""))
        if not inv:
            return {"error": "no such bill"}
        return _brief(inv) | {"chase_count": inv["chase_count"], "last_chased_at": inv.get("last_chased_at"),
                              "next_check_at": inv.get("next_check_at"), "evidence": agent._last_evidence(inv["id"]),
                              "itc_deadline": diagnose.itc_deadline(inv["tax_period"]) if inv.get("tax_period") else None}
    if name == "set_supplier_phone":
        return agent.tool_set_supplier_phone(trader, args.get("supplier", ""), args.get("phone", ""), turn)
    if name == "set_supplier_filing":
        sup = agent._match_supplier(trader, args.get("supplier", ""))
        if not sup:
            return {"ok": False, "error": "supplier not found"}
        store.upsert_supplier(trader, sup["gstin"], filing_hint=args["pattern"])
        diag = diagnose.run(trader, turn_id=turn)
        return {"ok": True, "supplier": sup.get("name"), "recheck": diag["decisions"]}
    if name == "skip_bill":
        inv = _find_invoice(trader, args.get("invoice_number", ""))
        if not inv:
            return {"error": "no such bill"}
        store.update_invoice(inv["id"], state=store.SKIPPED, state_reason="trader chose not to chase")
        return {"ok": True, "skipped": inv["invoice_number"]}
    if name == "recheck":
        return diagnose.run(trader, turn_id=turn)
    if name == "prepare_followups":
        res = agent.plan_followups(trader, turn)
        return {"drafts_shown_to_trader": bool(res), "awaiting_trader_approval": bool(res),
                "groups": len(res["groups"]) if res else 0}
    if name == "call_script":
        sup = agent._match_supplier(trader, args.get("supplier", ""))
        if not sup:
            return {"error": "supplier not found"}
        invs = [i for i in store.get_invoices(trader) if i["supplier_gstin"] == sup["gstin"]
                and i["state"] in store.AT_RISK]
        return {"script": M.call_script(sup.get("name") or sup["gstin"], sup.get("phone"), invs, agent.lang(trader))}
    return {"error": f"unknown tool {name}"}


def run_planner(agent, trader: str, text: str, turn: str, max_steps: int = 5) -> str:
    lang = agent.lang(trader)
    history = store.recent_messages(trader, limit=10)[:-1]
    msgs = [{"role": "system", "content": SYSTEM + f"\nToday is {store.now().date().isoformat()}. "
                                                  f"Trader language preference: {lang}."}]
    for h in history:
        if h.get("body"):
            msgs.append({"role": "user" if h["direction"] == "in" else "assistant", "content": h["body"][:800]})
    msgs.append({"role": "user", "content": text})
    for step in range(max_steps):
        reply = agent.llm.chat(msgs, tools=TOOLS, max_tokens=700)
        store.trace(trader, turn, "LLM", f"planner_step_{step}",
                    {"tool_calls": [tc["function"]["name"] for tc in reply.tool_calls], "usage": reply.usage})
        if not reply.tool_calls:
            return reply.content.strip()
        assistant = dict(reply.raw_message) if reply.raw_message else {}
        assistant.setdefault("role", "assistant")
        assistant["tool_calls"] = reply.tool_calls
        if not assistant.get("content"):
            assistant["content"] = None
        msgs.append(assistant)
        for tc in reply.tool_calls:
            fn = tc["function"]["name"]
            try:
                args = json.loads(tc["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            result = execute_tool(agent, trader, fn, args, turn)
            store.trace(trader, turn, "TOOL_CALL", f"planner:{fn}", {"args": args, "result": result})
            msgs.append({"role": "tool", "tool_call_id": tc.get("id", fn),
                         "content": json.dumps(result, ensure_ascii=False, default=str)[:3000]})
    return ""


# ---- deterministic fallback when the LLM is down ---------------------------------

def fallback_reply(agent, trader: str, text: str, turn: str, lang: str) -> str:
    low = text.lower()
    store.trace(trader, turn, "DECISION", "fallback_router", {"text": text[:120]})
    phone = re.search(r"(\+?\d[\d\s\-]{9,15})", text)
    if phone and any(w in low for w in ("number", "no.", "नंबर", "phone", "mobile", "ka", "का")):
        ref = re.sub(r"(\+?\d[\d\s\-]{9,15})|number|no\.|नंबर|phone|mobile|\bka\b|का|\bhai\b|है", " ", text,
                     flags=re.I).strip()
        res = agent.tool_set_supplier_phone(trader, ref, phone.group(1), turn, reply=False)
        if res and res.get("ok"):
            agent.plan_followups(trader, turn)
            return (f"📇 {res['supplier']} का नंबर सेव किया {M.tag(lang, 'you')}" if lang == "hi"
                    else f"📇 Saved {res['supplier']}'s number {M.tag(lang, 'you')}")
    if any(w in low for w in ("quarter", "qrmp", "तिमाही", "timahi", "3 mahine")):
        for s in store.get_suppliers(trader):
            if s.get("name") and s["name"].split()[0].lower() in low:
                store.upsert_supplier(trader, s["gstin"], filing_hint="qrmp")
                diagnose.run(trader, turn_id=turn)
                return (f"✅ {s['name']} को तिमाही (QRMP) वाला मान लिया; उनके बिल का पीछा नहीं करूँगा {M.tag(lang, 'you')}"
                        if lang == "hi" else f"✅ Marked {s['name']} as quarterly (QRMP); won't chase {M.tag(lang, 'you')}")
    if any(w in low for w in ("kitna", "कितना", "paisa", "पैसा", "status", "hisab", "हिसाब", "atka", "अटका", "how much")):
        return M.status_report(store.get_invoices(trader), lang)
    return M.help_text(lang)


# ---- supplier reply interpretation ---------------------------------------------------

SUPPLIER_PROMPT = """A supplier replied on WhatsApp to a request to report some purchase invoices in their GST return.
Classify the reply. Today is {today}. Return ONLY JSON:
{{"intent": "PROMISED"|"ALREADY_FILED"|"DISPUTE"|"WRONG_PERSON"|"OTHER",
  "promised_date": "YYYY-MM-DD"|null}}
PROMISED = will file/correct (resolve relative dates like 'kal', 'next week', 'Monday' to a date).
ALREADY_FILED = says it is already done. DISPUTE = says the bill is wrong/not theirs/won't do it.
WRONG_PERSON = wrong number / not the right contact.
Reply: \"\"\"{text}\"\"\""""


def interpret_supplier_reply(llm, text: str, today: str) -> tuple[str, str | None]:
    if llm.enabled:
        try:
            data, _ = llm.json_chat([{"role": "user", "content": SUPPLIER_PROMPT.format(today=today, text=text[:600])}],
                                    max_tokens=120)
            intent = str(data.get("intent", "OTHER")).upper()
            if intent not in {"PROMISED", "ALREADY_FILED", "DISPUTE", "WRONG_PERSON", "OTHER"}:
                intent = "OTHER"
            pd = data.get("promised_date")
            try:
                pd = date.fromisoformat(pd).isoformat() if pd else None
            except (TypeError, ValueError):
                pd = None
            return intent, pd
        except (LLMUnavailable, KeyError, ValueError):
            pass
    return keyword_supplier_intent(text, today)


def keyword_supplier_intent(text: str, today: str) -> tuple[str, str | None]:
    low = text.lower()
    base = date.fromisoformat(today)
    if re.search(r"wrong number|galat number|गलत नंबर|kaun|कौन", low):
        return "WRONG_PERSON", None
    if re.search(r"kar diya|kar di|ho gaya|filed|already|कर दिया|हो गया|done", low):
        return "ALREADY_FILED", None
    if re.search(r"nahi karunga|not ours|hamara nahi|हमारा नहीं|galat bill|गलत बिल|dispute", low):
        return "DISPUTE", None
    if re.search(r"kal|tomorrow|कल", low):
        return "PROMISED", (base + timedelta(days=1)).isoformat()
    if re.search(r"week|hafte|हफ्ते|हफ़्ते|saptah", low):
        return "PROMISED", (base + timedelta(days=7)).isoformat()
    if re.search(r"kar dunga|kar denge|karenge|will file|will do|कर दूंगा|कर दूँगा|कर देंगे|dekhta|देखता", low):
        return "PROMISED", (base + timedelta(days=5)).isoformat()
    return "OTHER", None
