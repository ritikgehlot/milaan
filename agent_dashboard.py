"""Streamlit view of the WhatsApp agent: cases, conversation, and the full
trace of every tool call, decision, consent and message (for judges and
debugging). Read-only."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from agent import store
from agent.messages import rupees

KIND_ICON = {"INBOUND": "📥", "TOOL_CALL": "🛠️", "DECISION": "🧠", "POLICY": "🛡️", "CONSENT": "✅",
             "MESSAGE": "📤", "LLM": "🤖", "ERROR": "⚠️", "DEMO": "⏩"}


def render() -> None:
    st.title("WhatsApp agent")
    st.caption("Live view of MILAAN's agent database: every bill, decision, consent and message is traced.")
    with store.conn() as c:
        traders = [r[0] for r in c.execute("SELECT phone FROM agent_traders ORDER BY created_at")]
    if not traders:
        st.info("No conversations yet. Seed the demo with `python -m agent.demo.seed ...` or message the bot.")
        return
    trader = st.selectbox("Trader", traders, format_func=lambda p: f"…{p[-4:]}")
    if st.button("Refresh", icon=":material/refresh:"):
        st.rerun()
    invs = store.get_invoices(trader)
    total = sum(i["total_gst_paise"] or 0 for i in invs)
    at_risk = sum(i["total_gst_paise"] or 0 for i in invs if i["state"] in {"TO_CHASE", "CHASED", "PROMISED",
                                                                             "CHANNEL_BLOCKED", "ESCALATED"})
    rec = sum(i["total_gst_paise"] or 0 for i in invs if i["state"] == "RECOVERED")
    done = sum(1 for i in invs if i["state"] in store.TERMINAL)
    k = st.columns(4)
    k[0].metric("Bills", len(invs), f"GST {rupees(total)}", delta_color="off")
    k[1].metric("GST at risk", rupees(at_risk))
    k[2].metric("Recovered", rupees(rec))
    k[3].metric("Terminal state", f"{done}/{len(invs)}")

    tab_cases, tab_chat, tab_trace, tab_out = st.tabs(["Cases", "Conversation", "Trace", "Outbound"])
    with tab_cases:
        df = pd.DataFrame([{
            "id": i["id"], "supplier": i["supplier_name"], "invoice": i["invoice_number"],
            "date": i["invoice_date"], "GST": (i["total_gst_paise"] or 0) / 100, "state": i["state"],
            "cause": i["diagnosis"], "why": i["state_reason"], "chases": i["chase_count"],
            "next check": i["next_check_at"], "source": i["provenance"]} for i in invs])
        st.dataframe(df, hide_index=True, use_container_width=True,
                     column_config={"GST": st.column_config.NumberColumn(format="₹%.2f")})
    with tab_chat:
        for m in store.recent_messages(trader, limit=200):
            with st.chat_message("user" if m["direction"] == "in" else "assistant"):
                st.text(m["body"] or f"[{m['media_type']}]")
    with tab_trace:
        rows = store.get_trace(trader, limit=800)
        kinds = sorted({r["kind"] for r in rows})
        pick = st.multiselect("Kinds", kinds, default=kinds)
        for r in rows:
            if r["kind"] not in pick:
                continue
            with st.expander(f"{KIND_ICON.get(r['kind'], '•')} {r['created_at']} · {r['kind']} · {r['name']}"):
                try:
                    st.json(json.loads(r["payload"]))
                except json.JSONDecodeError:
                    st.text(r["payload"])
    with tab_out:
        out = store.get_outbound(trader)
        st.dataframe(pd.DataFrame([{"id": o["id"], "to": "…" + o["to_phone"][-4:], "supplier GSTIN": o["supplier_gstin"],
                                    "status": o["status"], "attempts": o["attempts"], "error": o["error"],
                                    "at": o["created_at"]} for o in out]), hide_index=True, use_container_width=True)
