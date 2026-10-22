"""Baseline B1: one vision-LLM call handed every bill photo + the GSTR-2B + supplier
filing history, asked to list what to do for each bill. Tests whether the agentic
decomposition earns its complexity. Needs LLM_API_KEY.

  python -m agent.eval.b1_single_shot [--runs 3]

Scored on the demo month (9 bills, every cause represented) against the correct
action for each bill. Small n — reported as such.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agent.config import ROOT, get_settings
from agent.demo import scenario
from agent.demo.seed import DEMO_DIR
from agent.llm import LLMClient, image_part

TRUTH = {  # invoice number -> correct action
    "MCA/1187": "NO_ACTION", "MCA/1214": "NO_ACTION", "SBP/26-27/0412": "NO_ACTION", "TST/088": "NO_ACTION",
    "MHM/2026/551": "CHASE", "MHM/2026/583": "CHASE", "RE-3341": "CHASE", "JPS/0977": "ASK_TRADER",
    "SRB-2291": "NO_ACTION",
}
INNOCENT = {"MCA/1187", "MCA/1214", "SBP/26-27/0412", "TST/088", "SRB-2291"}

PROMPT = """You help an Indian GST-registered trader recover input tax credit. Attached are photos of ALL his
purchase bills for October 2026, then his GSTR-2B JSON for October 2026, then each supplier's past 2B appearance
history (which months their invoices appeared in).
For EACH bill decide one action:
  CHASE       = message the supplier asking them to report/correct the invoice in their return
  NO_ACTION   = nothing to do (already in 2B, legitimately not due yet, or credit not claimable anyway)
  ASK_TRADER  = ask the trader to confirm something before contacting anyone
Return ONLY JSON: {"bills": [{"invoice_number": "...", "action": "CHASE|NO_ACTION|ASK_TRADER", "reason": "..."}]}
"""


def run_once(llm: LLMClient) -> dict:
    content = [{"type": "text", "text": PROMPT}]
    for key in scenario.SEEDED + scenario.LIVE:
        content.append({"type": "text", "text": f"Bill photo {key}:"})
        content.append(image_part((DEMO_DIR / "bills" / f"{key}.jpg").read_bytes(), "image/jpeg"))
    content.append({"type": "text", "text": "GSTR-2B JSON:\n" + (DEMO_DIR / "GSTR2B_OCT2026.json").read_text()})
    content.append({"type": "text", "text": "Supplier 2B history:\n" + (DEMO_DIR / "history.json").read_text()})
    s = get_settings()
    data, usage = llm.json_chat([{"role": "user", "content": content}], model=s.vision_model, max_tokens=2500)
    got = {}
    for b in data.get("bills", []):
        num = str(b.get("invoice_number", "")).strip().upper()
        for t in TRUTH:
            if num == t.upper():
                got[t] = str(b.get("action", "")).upper()
    correct = sum(1 for t, a in TRUTH.items() if got.get(t) == a)
    chases = [t for t, a in got.items() if a == "CHASE"]
    return {"correct": correct, "of": len(TRUTH), "missing_bills": [t for t in TRUTH if t not in got],
            "messages_to_innocent": [t for t in chases if t in INNOCENT],
            "premature_on_gstin_conflict": got.get("JPS/0977") == "CHASE",
            "chase_recall": sum(1 for t in chases if TRUTH[t] == "CHASE") / 3, "actions": got, "usage": usage}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3)
    a = ap.parse_args(argv)
    llm = LLMClient()
    if not llm.enabled:
        raise SystemExit("Set LLM_API_KEY in .env first.")
    if not (DEMO_DIR / "GSTR2B_OCT2026.json").exists():
        scenario.build(DEMO_DIR)
    results = []
    for i in range(a.runs):
        r = run_once(llm)
        results.append(r)
        print(f"run {i + 1}: {r['correct']}/{r['of']} correct, innocent msgs={r['messages_to_innocent']}, "
              f"JPS chased prematurely={r['premature_on_gstin_conflict']}, missing={r['missing_bills']}")
    out = ROOT / "agent_data" / "eval" / "b1_results.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2))
    print("MILAAN on the same 9 bills: 9/9 (see test_agent.AgentFlowTests.test_full_demo_cycle). saved", out)


if __name__ == "__main__":
    main()
