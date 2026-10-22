# MILAAN: notes for Claude Code

MILAAN is a WhatsApp agent (SARCathon 2026 finale) that recovers GST input tax credit for a small trader.
The trader uses only WhatsApp. The web dashboard is a read-only track record.

## Commands (Windows PowerShell, venv at .venv)
- Install: `py -m venv .venv; .venv\Scripts\Activate.ps1; pip install -r requirements-agent.txt`
- All tests: `python -m unittest discover -p "test_*.py"` (expect 314+, all OK)
- Offline demo, no keys needed: `python -m agent.simulate --script demo`
- Setup check: `python -m agent.check` (LLM text + vision, Twilio credentials, PUBLIC_URL)
- Seed the demo month: `python -m agent.demo.seed --trader +91XXXXXXXXXX --supplier +91YYYYYYYYYY`. Stop the server first.
- Server (port 5050): `python -m agent.server`. The live dashboard is at http://localhost:5050/dashboard.
- Tunnel: `.\cloudflared.exe tunnel --url http://localhost:5050`. Its https URL goes in PUBLIC_URL and in the Twilio webhook `<url>/whatsapp`.
- Standalone dashboard (offline): `python -m agent.web` → http://localhost:8090/dashboard
- Evals: `python -m agent.eval.diagnosis_eval`, `python -m agent.eval.extraction_eval --run`, `python -m agent.eval.b1_single_shot`

## Architecture
- `agent/core.py`: turn handler: bills, 2B, consent-gated sends, supplier replies, scheduler tick.
- `agent/extract.py`: vision extraction, double read, gates. A GSTIN is never guessed.
- `agent/diagnose.py`: deterministic cause → action.
- `agent/planner.py`: LLM tool loop. It has no send tool.
- `agent/llm.py`: Gemini native API (works with AQ. keys), Claude native + OpenAI-compatible endpoints.
- `agent/channel.py`: Twilio send (throttled to 1 message / 3 s for the sandbox) and signature check; Meta WhatsApp Cloud API send, media download, `X-Hub-Signature-256` check and webhook parsing (`WHATSAPP_PROVIDER=meta`, tests in `test_meta.py`).
- `agent/web.py` + `agent/web/dashboard.html`: the read-only dashboard.
- Core modules from Round 1 (`validation.py`, `gstr2b.py`, `reconciliation.py`) are tested. Don't change their behaviour.

## Hard rules
- Never print, log or commit `.env` values (keys, tokens).
- Never fabricate metrics. Numbers in docs or slides come only from files the eval scripts write.
- Don't weaken tests to make them pass. Fix the code.
- Any third-party message must stay behind the trader's explicit "haan". The LLM must never get a send tool.
- The Twilio trial has about 100 free messages. Don't send WhatsApp messages without asking the user first.
