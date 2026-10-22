"""Reset the agent database and load the demo month.

  python -m agent.demo.seed --trader +9198XXXXXXXX --supplier +9199XXXXXXXX [--supplier2 +91...]

The trader phone is the one that will chat with MILAAN. The supplier
phone(s) are CONSENTING test recipients (e.g. your teammate) standing in for
Mahadev Hardware / Rathore Electricals — disclosed in the pitch.
Seeded bills go through the same pipeline (fixture reader instead of the
vision model, because they are pre-loaded); the live bills L1/L2 are sent
from the phone during the demo.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .. import store
from ..channel import ConsoleChannel
from ..config import ROOT, get_settings, normalize_phone
from . import scenario
from .fixtures import FixtureExtractor

DEMO_DIR = ROOT / "agent_data" / "demo"


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trader", required=True)
    ap.add_argument("--supplier", required=True, help="consenting test phone for Mahadev Hardware")
    ap.add_argument("--supplier2", default=None, help="consenting test phone for Rathore Electricals")
    ap.add_argument("--keep", action="store_true", help="do not delete the existing agent DB")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    s = get_settings()
    if not a.keep:
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(s.db_path) + suffix)
            if p.exists():
                p.unlink()
        store.reset_initialized()
    info = scenario.build(DEMO_DIR)
    trader = normalize_phone(a.trader)
    store.ensure_trader(trader, "hi")
    with store.conn() as c:
        c.execute("UPDATE agent_traders SET name=? WHERE phone=?", ("रमेश हार्डवेयर / Ramesh Hardware", trader))
    for gstin, periods in info["history"].items():
        for p in periods:
            store.add_history(trader, gstin, p)
    phones = {"mahadev": normalize_phone(a.supplier), "rathore": normalize_phone(a.supplier2 or a.supplier)}
    for key, (name, gstin, _addr) in scenario.S.items():
        store.upsert_supplier(trader, gstin, name, phone=phones.get(key), consent=1 if key in phones else 0)

    from ..core import Agent
    os.environ.setdefault("DEMO_COMMANDS", "1")
    agent = Agent(channel=ConsoleChannel(echo=False), settings=s, fixture_extractor=FixtureExtractor(DEMO_DIR))
    for key in scenario.SEEDED:
        jpg = Path(info["bills"][key])
        agent.handle_bill(trader, jpg.read_bytes(), "image/jpeg", "seed")
    with store.conn() as c:
        c.execute("UPDATE agent_invoices SET provenance='seed:'||provenance")
        # pre-loaded bills were never sent over WhatsApp: drop their console replies
        c.execute("DELETE FROM agent_messages")
        c.execute("DELETE FROM agent_trace WHERE turn_id='seed' AND kind='MESSAGE'")
    invs = store.get_invoices(trader)
    if a.quiet:
        return
    print(f"Seeded {len(invs)} bills for {trader}:")
    for i in invs:
        print(f"  {i['id']:>2} {i['supplier_name'][:28]:<28} {i['invoice_number']:<16} {i['state']:<11} "
              f"GST ₹{(i['total_gst_paise'] or 0)/100:,.2f}")
    print(f"\nLive demo files in {DEMO_DIR}:")
    print("  bills/L1_marudhar_1214.jpg   (clean bill -> recorded)")
    print("  bills/L2_rathore_3341.jpg    (smudged GSTIN -> targeted re-ask)")
    print("  bills/X_not_a_bill.jpg       (rejected)")
    print("  GSTR2B_OCT2026.json          (send as a document -> reconciliation)")
    print("  GSTR2B_NOV2026.json          (next month -> RECOVERED)")
    print(f"\nRathore GSTIN to type when asked: {scenario.S['rathore'][1]}")


if __name__ == "__main__":
    main()
