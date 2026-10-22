"""Setup check — run this first:  python -m agent.check

Verifies .env, the LLM (text + vision on a demo bill), Twilio credentials,
and the public URL. Prints exactly what is wrong.
"""

from __future__ import annotations

import re
import sys

import requests

from .config import get_settings
from .demo import scenario
from .demo.seed import DEMO_DIR
from .llm import LLMClient


def ok(msg):
    print(f"  ✅ {msg}")


def bad(msg):
    print(f"  ❌ {msg}")


def check_meta(s) -> int:
    """Meta WhatsApp Cloud API: settings present and well-formed, token accepted (read-only lookup)."""
    print("3. WhatsApp (Meta Cloud API)")
    failures = 0
    for name, value in (("META_ACCESS_TOKEN", s.meta_access_token), ("META_PHONE_NUMBER_ID", s.meta_phone_number_id),
                        ("META_API_VERSION", s.meta_api_version), ("META_APP_SECRET", s.meta_app_secret),
                        ("META_VERIFY_TOKEN", s.meta_verify_token)):
        if not value:
            bad(f"{name} missing")
            failures += 1
    if s.meta_api_version and not re.fullmatch(r"v\d+\.\d+", s.meta_api_version):
        bad("META_API_VERSION must look like v<major>.<minor>, copied from the Meta app")
        failures += 1
    if s.meta_phone_number_id and not s.meta_phone_number_id.isdigit():
        bad("META_PHONE_NUMBER_ID must be the numeric Phone number ID, not the phone number itself")
        failures += 1
    if failures:
        return failures
    try:
        r = requests.get(f"https://graph.facebook.com/{s.meta_api_version}/{s.meta_phone_number_id}",
                         params={"fields": "display_phone_number,verified_name"},
                         headers={"Authorization": f"Bearer {s.meta_access_token}"}, timeout=15)
    except Exception as exc:
        bad(f"graph.facebook.com unreachable: {type(exc).__name__}")
        return 1
    try:
        data = r.json()
    except ValueError:
        data = {}
    if r.status_code == 200:
        ok(f"credentials: HTTP 200, sender {data.get('display_phone_number')} ({data.get('verified_name')})")
        return 0
    err = data.get("error") or {}
    bad(f"credentials: HTTP {r.status_code} {err.get('code')}: {str(err.get('message'))[:160]}")
    if err.get("code") == 190:
        print("     the access token expired or is wrong: generate a new one in the Meta app (WhatsApp > API Setup)")
    return 1


def main() -> int:
    s = get_settings()
    failures = 0
    print("1. Config")
    if s.trader_phones:
        ok(f"TRADER_PHONES = {list(s.trader_phones)}")
    else:
        bad("TRADER_PHONES empty (any sender would be treated as the trader)")
        failures += 1
    print("2. LLM", s.llm_base_url, s.llm_model)
    llm = LLMClient(s)
    if not llm.enabled:
        bad("LLM_API_KEY missing")
        failures += 1
    else:
        try:
            r = llm.chat([{"role": "user", "content": "Reply with the single word: pong"}], max_tokens=10)
            ok(f"text: {r.content.strip()[:30]!r} usage={r.usage}")
        except Exception as exc:
            bad(f"text call failed: {exc}")
            failures += 1
            if llm.is_anthropic:
                print("     Claude: check the key at console.anthropic.com and LLM_MODEL (e.g. claude-sonnet-5-5)")
            try:
                if llm.is_gemini:
                    r = requests.get(s.llm_base_url.split("/v1beta")[0] + "/v1beta/models?pageSize=200",
                                     headers={"x-goog-api-key": s.llm_api_key}, timeout=15)
                    ids = [m.get("name", "").replace("models/", "") for m in r.json().get("models", [])]
                else:
                    r = requests.get(s.llm_base_url.rstrip("/") + "/models",
                                     headers={"Authorization": f"Bearer {s.llm_api_key}"}, timeout=15)
                    ids = [m.get("id", "") for m in r.json().get("data", [])]
                flash = [i for i in ids if "flash" in i or "mini" in i][:12]
                print("     models your key can use (set LLM_MODEL to one of these):", flash or ids[:12])
            except Exception:
                pass
        try:
            if not (DEMO_DIR / "bills" / "L1_marudhar_1214.jpg").exists():
                scenario.build(DEMO_DIR)
            from . import extract
            ex = extract.extract_with_llm((DEMO_DIR / "bills" / "L1_marudhar_1214.jpg").read_bytes(), "image/jpeg",
                                          llm, double_read=True)
            f = ex.fields
            good = f.get("supplier_gstin") == scenario.S["marudhar"][1] and f.get("total_gst_paise") == 698400
            (ok if good else bad)(f"vision: status={ex.status} gstin={f.get('supplier_gstin')} "
                                  f"gst={f.get('total_gst_paise')} uncertain={ex.uncertain}")
            failures += 0 if good else 1
        except Exception as exc:
            bad(f"vision call failed: {exc}")
            failures += 1
    print("   voice notes:", "Gemini key set ✅" if s.gemini_api_key else
          "no GEMINI_API_KEY — voice notes will politely ask the trader to type (Claude has no audio input)")
    if s.whatsapp_provider == "meta":
        failures += check_meta(s)
    else:
        failures += check_twilio(s)
    print("4. Public URL")
    if not s.public_url:
        bad("PUBLIC_URL empty — set it to your tunnel's https URL (needed for signature check + crop images)")
        failures += 1
    else:
        try:
            r = requests.get(s.public_url + "/health", timeout=10, headers={"ngrok-skip-browser-warning": "1"})
            (ok if r.status_code == 200 else bad)(f"{s.public_url}/health -> HTTP {r.status_code} {r.text[:120]}")
        except Exception as exc:
            bad(f"{s.public_url} not reachable (is the server + tunnel running?): {exc}")
        if s.whatsapp_provider == "meta":
            ok(f"Meta webhook Callback URL must be: {s.public_url}/whatsapp  (Verify token = your META_VERIFY_TOKEN; "
               "subscribe to the 'messages' field)")
        else:
            ok(f"Twilio sandbox webhook must be: {s.public_url}/whatsapp  (HTTP POST)")
    print("\nAll good." if not failures else f"\n{failures} problem(s) above.")
    return 1 if failures else 0


def check_twilio(s) -> int:
    failures = 0
    print("3. Twilio")
    if not s.twilio_enabled:
        bad("TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN missing")
        failures += 1
    else:
        try:
            r = requests.get(f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}.json",
                             auth=(s.twilio_account_sid, s.twilio_auth_token), timeout=15)
            (ok if r.status_code == 200 else bad)(f"credentials: HTTP {r.status_code}")
            failures += 0 if r.status_code == 200 else 1
        except Exception as exc:
            bad(f"twilio unreachable: {exc}")
            failures += 1
        ok(f"sender = {s.twilio_whatsapp_from}")
    return failures


if __name__ == "__main__":
    sys.exit(main())
