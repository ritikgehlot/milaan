"""Offline WhatsApp simulator: the same Agent, with a console channel.

  python -m agent.simulate                 interactive
  python -m agent.simulate --script demo   run the full judge demo end to end
  add --offline to either: no network and no API keys (fixture reader, no LLM)

Interactive commands:
  <text>                 trader sends a text
  /photo <path>          trader sends a photo
  /file <path>           trader sends a document (GSTR-2B JSON/CSV/XLSX)
  /supplier <text>       the (test) supplier phone replies
  /quit

Uses the vision LLM if LLM_API_KEY is set (unless --mock); otherwise the
disclosed fixture reader for the demo images.
"""

from __future__ import annotations

import argparse
import mimetypes
import os
import sys
from pathlib import Path

from . import store
from .channel import ConsoleChannel
from .config import get_settings
from .core import Agent
from .demo import scenario
from .demo.fixtures import FixtureExtractor
from .demo.seed import DEMO_DIR, main as seed_main

TRADER = "+919000000001"
SUPPLIER = "+919000000002"


def make_agent(mock: bool) -> tuple[Agent, ConsoleChannel]:
    s = get_settings()
    s.trader_phones = (TRADER,)
    s.demo_commands = True
    ch = ConsoleChannel(echo=True)
    fx = FixtureExtractor(DEMO_DIR) if (mock or not s.llm_enabled) else None
    return Agent(channel=ch, settings=s, fixture_extractor=fx), ch


def send(agent: Agent, line: str) -> None:
    line = line.strip()
    if not line:
        return
    if line.startswith("/photo ") or line.startswith("/file ") or line.startswith("/voice "):
        cmd, path = line.split(" ", 1)
        path = path.strip()
        if not Path(path).exists():
            path = str(DEMO_DIR / path)
        mime = mimetypes.guess_type(path)[0] or ("application/json" if path.endswith(".json") else "application/octet-stream")
        print(f"\n>>> TRADER sends {cmd[1:]}: {Path(path).name}")
        agent.handle(TRADER, "", [(path, mime)], [Path(path).name])
    elif line.startswith("/supplier "):
        text = line[len("/supplier "):]
        print(f"\n>>> SUPPLIER: {text}")
        agent.handle(SUPPLIER, text)
    else:
        print(f"\n>>> TRADER: {line}")
        agent.handle(TRADER, line)


DEMO_SCRIPT = [
    "namaste",
    "/photo bills/L1_marudhar_1214.jpg",
    "/photo bills/L2_rathore_3341.jpg",
    f"{scenario.S['rathore'][1]}",
    "/photo bills/X_not_a_bill.jpg",
    "/file GSTR2B_OCT2026.json",
    "2b {jps_id}",
    "dikhao 1",
    "haan",
    "/supplier bhai agle hafte tak dono bill daal dunga",
    "kitna paisa atka hai?",
    "#advance 8",
    "/file GSTR2B_NOV2026.json",
    "status",
]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", choices=["demo"], default=None)
    ap.add_argument("--mock", action="store_true", help="force the fixture reader even if an LLM key is set")
    ap.add_argument("--no-seed", action="store_true")
    ap.add_argument("--offline", action="store_true",
                    help="no network and no API keys: fixture reader for bills, no LLM planner or voice")
    a = ap.parse_args(argv)
    os.environ.setdefault("AGENT_DB_PATH", str(Path(get_settings().db_path).with_name("simulator.db")))
    if a.offline:  # .env is loaded by now, so these empty values win for the rest of the run
        os.environ["LLM_API_KEY"] = ""
        os.environ["GEMINI_API_KEY"] = ""
    if not a.no_seed:
        seed_main(["--trader", TRADER, "--supplier", SUPPLIER])
    agent, _ = make_agent(a.mock)
    if a.script == "demo":
        for line in DEMO_SCRIPT:
            if "{jps_id}" in line:
                jps = [i for i in store.get_invoices(TRADER) if i["invoice_number"] == "JPS/0977"]
                line = line.format(jps_id=jps[0]["id"] if jps else 0)
            send(agent, line)
        return
    print("MILAAN simulator. Type a message, or /photo, /file, /supplier, /quit")
    for line in sys.stdin:
        if line.strip() == "/quit":
            break
        send(agent, line)


if __name__ == "__main__":
    main()
