# MILAAN finale checklist

Status as verified on the presenter's laptop on 2026-10-22. Anything not ticked here is not verified.

## 1. Where things stand

| Item | Status |
|---|---|
| Test suite | ✅ all passing: `python -m unittest discover -p "test_*.py"` |
| Offline demo | ✅ `python -m agent.simulate --script demo --offline` ends with ₹16,362 recovered, no network and no API key used |
| Dashboard for the offline run | ✅ loads (command in section 3, plan B) |
| `python -m agent.check` | ✅ Gemini text and vision (`gemini-3.5-flash`), ✅ Twilio credentials |
| Live WhatsApp, Twilio | ❌ this Twilio trial ("Try out WhatsApp") never showed the incoming-message webhook setting, so Twilio cannot reach the server |
| Live WhatsApp, Meta Cloud API | ⚠️ built and covered by offline tests (`test_meta.py`); **not yet run against Meta** |

## 2. Handoff: connect WhatsApp through Meta

Do this on the laptop that will present. Full steps are in [SETUP_AGENT.md](SETUP_AGENT.md), section 3b.

1. `git clone <repo>`, then `py -m venv .venv`, `.venv\Scripts\activate`, `pip install -r requirements-agent.txt`.
2. `copy .env.agent.example .env`, then fill in `LLM_API_KEY`, `TRADER_PHONES`, `WHATSAPP_PROVIDER=meta` and the five `META_` lines. Keep `LLM_MODEL=gemini-3.5-flash` unless `agent.check` says your key cannot use it; it then lists the models it can. Never commit `.env`; it is gitignored.
3. Download `cloudflared.exe` from the [cloudflared releases](https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe) into the project folder.
4. Run `python -m agent.demo.seed --trader <phone A> --supplier <phone B>` with the server stopped.
5. Start the tunnel (`.\cloudflared.exe tunnel --url http://localhost:5050`), put its https URL in `PUBLIC_URL`, then start the server (`python -m agent.server`).
6. Meta app: add phone A and phone B under API Setup → **To**. In WhatsApp → Configuration → Webhook, enter Callback URL `<PUBLIC_URL>/whatsapp` and your `META_VERIFY_TOKEN`, click **Verify and save**, and subscribe to **messages**.

A live rehearsal passes only if every line below happens:

| Do | Must see |
|---|---|
| `python -m agent.check` | every line ✅, section 3 shows the Meta test number |
| Phone A sends `namaste` | the MILAAN help text in Hindi |
| Phone A sends `L1_marudhar_1214.jpg` | ✅ bill recorded |
| Phone A sends `L2_rathore_3341.jpg`, then types `08AAHFR6643B1Z9` | a crop of the GSTIN, then ✅ fixed |
| Phone A sends `GSTR2B_OCT2026.json` as a Document | the match report and "ready to message" |
| Phone A types `2b <id>`, then `dikhao 1`, then `haan` | phone B receives the Hindi + English message |
| Phone B replies `bhai agle hafte tak dono bill daal dunga` | phone A gets the relayed reply |
| Phone A sends `GSTR2B_NOV2026.json` | 🎉 RECOVERED |

Never use the `whatsapp-web.js` route from the other repo: it is unofficial and can get a number banned.

## 3. Demo day

**Plan A, live WhatsApp.** Only if the full rehearsal above passed at least a day before.
* 30 minutes before: paste a fresh Meta access token (it expires after about 24 hours), start the tunnel, update `PUBLIC_URL` and the Meta Callback URL if the tunnel URL changed, restart the server, and run `agent.check`.
* Phone A and phone B each send `hi` to the test number. Meta allows free-text replies only within 24 hours of the person's last message.
* Re-seed with the server stopped, then start it again.

**Plan B, offline.** Always ready, needs nothing from the network.
* `python -m agent.simulate --script demo --offline`
* Dashboard: `$env:AGENT_DB_PATH = "agent_data\simulator.db"; python -m agent.web`, then open http://localhost:8090/dashboard
* Say it plainly: in offline mode the bills are read by a fixture reader, not by the vision model.

**Gemini quota.** The free tier allows 20 requests per model per day. The number of calls in one full live run has not been measured yet: count them in the rehearsal, or enable billing.

## 4. Numbers allowed on the PPT

Only these, copied from the files named. Anything else is `[not measured]`.

| Claim | Value | Source |
|---|---|---|
| Correct action, synthetic diagnosis benchmark (seed 7) | MILAAN 0.99, exact-string matcher 0.76 | `agent_data/eval/diagnosis_results.json` |
| Chase drafts to innocent suppliers (seed 7) | MILAAN 10, exact-string matcher 143 | same |
| Messages on a misread GSTIN (seed 7) | MILAAN 0, exact-string matcher 25 | same |
| Same table for seeds 11 and 23 | see EVALUATION.md section 1 | `diagnosis_seed11.json`, `diagnosis_seed23.json` |
| Bill extraction, 3 bills with a wrong tax line | 0 unsafe passes, all 3 held for the trader | `agent_data/eval/extraction_results.json` |
| Single-shot LLM baseline, 1 run | 8/9 correct, chased JPS/0977 before its GSTIN was confirmed | `agent_data/eval/b1_results.json` |
| Demo month (synthetic) | ₹16,362 recovered | offline demo output; `test_full_demo_cycle` |

Say with every number: synthetic data, written by the same team, not a real-world accuracy figure. Do not present "MILAAN 9/9" as an eval result: no eval file contains it.

## 5. Before pushing to GitHub

* `.env`, the databases (`*.db`) and `agent_data/` runtime files are gitignored. `agent_data/eval/` is included on purpose.
* `cloudflared.exe` is gitignored; each machine downloads its own.
