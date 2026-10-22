# MILAAN: live demo setup (about 45 minutes the first time)

## What you need
* A laptop with Python 3.10+, the unzipped `MILAAN-master` folder, and internet.
* **Phone A (trader):** yours. Open WhatsApp Web on the laptop logged in as this number, so the projector shows the chat and you can attach files straight from the laptop.
* **Phone B (supplier):** your teammate's, the consenting test recipient.
* Free accounts at aistudio.google.com (Gemini key), twilio.com, and ngrok.com.

## 1. Install and rehearse offline (10 min)
```
cd MILAAN-master
py -m venv .venv                  (mac: python3 -m venv .venv)
.venv\Scripts\activate            (mac: source .venv/bin/activate)
pip install -r requirements-agent.txt
copy .env.agent.example .env      (mac: cp .env.agent.example .env)
python -m agent.simulate --script demo
```
The last command plays the whole demo in the terminal with no keys. If it runs, the code works on your laptop.

## 2. Gemini key (3 min)
At aistudio.google.com, choose Get API key, then Create. In `.env`, set `LLM_API_KEY=` to the key.

## 3. Twilio WhatsApp sandbox (10 min)
1. Sign up at twilio.com and verify your phone number.
2. Go to Console → Messaging → Try it out → Send a WhatsApp message. It shows a code like `join yellow-tiger`.
3. Send that exact text from **phone A** and from **phone B** to **+1 415 523 8886**. Each phone gets a "you are connected" reply.
4. On the Console home page, copy the **Account SID** and **Auth Token** into `.env`.
5. Set `TRADER_PHONES=+91<phone A number>` in `.env`.

Sandbox limits to plan around:
* **About 100 free messages on a trial.** One full demo uses about 20, so do a single live rehearsal and practise the rest in the simulator. If you can, add $20 credit to remove the trial limits.
* **The join lasts 3 days.** Re-send `join …` from both phones on the morning of the finale.
* **24-hour window.** The bot can only send to a phone that messaged the sandbox in the last 24 hours. Both phones should send "hi" within an hour of the demo.
* **One message every 3 seconds.** The agent paces itself, so a reply burst after the 2B file takes about 10 to 15 seconds. That is expected.

## 3b. OR: Meta WhatsApp Cloud API instead of Twilio (15 min)
Use this if your Twilio trial will not let you set an incoming-message webhook, or allows only one recipient. Meta's free test number can message up to 5 verified numbers, so phone A and phone B share it.

In the Meta app at developers.facebook.com (the app needs the WhatsApp product added):
1. **WhatsApp → API Setup.** Copy the temporary access token, the **Phone number ID** (a long number, not the phone number), and the version from the sample curl (`https://graph.facebook.com/vXX.X/...`).
2. On the same page, under **To**, add phone A and phone B as recipients and confirm each with the code WhatsApp sends.
3. **App settings → Basic → App secret.** Click Show and copy it.
4. In `.env`, set:
   ```
   WHATSAPP_PROVIDER=meta
   META_ACCESS_TOKEN=<temporary access token>
   META_PHONE_NUMBER_ID=<Phone number ID>
   META_API_VERSION=<vXX.X from the sample curl>
   META_APP_SECRET=<App secret>
   META_VERIFY_TOKEN=<any long random string you make up>
   ```
5. Start the tunnel, set `PUBLIC_URL`, and start (or restart) the server, as in steps 4 and 5 below. The server must be running for the next step.
6. **WhatsApp → Configuration → Webhook → Edit.** Callback URL: `<PUBLIC_URL>/whatsapp`. Verify token: the same `META_VERIFY_TOKEN`. Click **Verify and save**. Then, under Webhook fields, subscribe to **messages**.
7. Run `python -m agent.check`. Section 3 must show `credentials: HTTP 200` with the test number.

Meta rules to plan around:
* **24-hour window.** The bot can send free text only to a phone that messaged the test number in the last 24 hours. Phone A and phone B should each send "hi" to the test number before the demo.
* **The temporary token expires after about 24 hours.** Paste a fresh one into `.env` on demo day and restart the server, or create a permanent System User token in Meta Business settings.
* Re-seed with phone B's real number (`--supplier`) so MILAAN knows which sender is the supplier.

## 4. ngrok, your public URL (5 min)
1. Sign up at ngrok.com, download ngrok, and run `ngrok config add-authtoken <token>`.
2. In the ngrok dashboard, open **Domains** and claim your free static domain, for example `milaan-demo.ngrok-free.app`. A static domain means the URL never changes.
3. In `.env`, set `PUBLIC_URL=https://milaan-demo.ngrok-free.app` (no trailing slash).

## 5. Start everything (two terminals; activate the venv in each)
```
Terminal 1:   python -m agent.demo.seed --trader +91<phone A> --supplier +91<phone B>
              python -m agent.server
Terminal 2:   ngrok http --url=milaan-demo.ngrok-free.app 5050
              (older ngrok versions: --domain=milaan-demo.ngrok-free.app)
```
Then in Twilio, open Send a WhatsApp message → **Sandbox settings** and set:
* **When a message comes in:** `https://milaan-demo.ngrok-free.app/whatsapp`
* **Method:** POST

Save.

## 6. Verify (2 min)
* In a third terminal, run `python -m agent.check`. Every line must be ✅.
* From phone A, send `namaste`. You should get the MILAAN help text in Hindi.
* Then run the seed again so the demo starts clean (stop the server first; see "Reset" below).

## 7. Trace viewer for the projector
In a third terminal, run `streamlit run app.py`, open http://localhost:8501, and go to the **WhatsApp agent** page. Use the Refresh button after each demo step.

## 8. Demo run (about 3 minutes)
Work from WhatsApp Web, in the chat with +1 415 523 8886. Files are in `MILAAN-master\agent_data\demo\`.

| # | Do this | Expect |
|---|---|---|
| 1 | Attach (📎 → Photos) `bills\L1_marudhar_1214.jpg` | ✅ bill recorded, all checks pass |
| 2 | Attach `bills\L2_rathore_3341.jpg` | ❓ cropped image of the smudged GSTIN; asks you to type it |
| 3 | Type `08AAHFR6643B1Z9` | ✅ fixed |
| 4 | Attach (📎 → **Document**) `GSTR2B_OCT2026.json` | Report: 3 matched (1 via a format variant), 1 quarterly filer not chased, 1 GSTIN question, then "ready to message 2 suppliers about ₹16,362" |
| 5 | Type the reply the question asks for, e.g. `2b 6` | ✅ bill misprint, credit safe |
| 6 | Type `dikhao 1`, then `haan` | Phone B receives the Hindi + English messages; you get "📤 sent" |
| 7 | Phone B types `bhai agle hafte tak dono bill daal dunga` | You get the relay with a resolved date |
| 8 | Hold the mic in WhatsApp Web and say "कितना पैसा अटका है?" | 🎙️ transcript, then the status in rupees |
| 9 | Attach (Document) `GSTR2B_NOV2026.json` | 🎉 RECOVERED ₹16,362 |
| 10 | Switch to Streamlit → Trace | Every tool call, decision, consent and message |

## Reset between runs
Press Ctrl+C on the server, run the seed command again, then start the server again. Do not run the seed while the server is running.

## If something breaks on stage
| Symptom | Fix |
|---|---|
| No reply at all | Is the server terminal showing `POST /whatsapp`? If not, check ngrok is running and the Twilio webhook URL. |
| Server logs 403 | `PUBLIC_URL` must match the Twilio webhook URL exactly. Quick fix: `TWILIO_VALIDATE_SIGNATURE=0` in `.env`, then restart the server. |
| Phone B doesn't receive | Its join expired or its 24-hour window closed. Phone B sends `join …`, then `hi`. |
| Replies are slow | Normal: 3 seconds per message, plus the model. Talk over it; the trace explains what it is doing. |
| The JSON won't attach or is ignored | Send it as **Document**. If needed, rename it to `.txt`; the parser detects the content. |
| Internet or Twilio is down | Run `python -m agent.simulate --script demo`. Same agent, console channel. Say so to the judges. |
| Meta: "Verify and save" fails | The server must be running with `WHATSAPP_PROVIDER=meta`, and the Verify token in Meta must equal `META_VERIFY_TOKEN`. Restart the server after editing `.env`. |
| Meta: server logs 403 on `POST /whatsapp` | `META_APP_SECRET` is not this app's App secret. |
| Meta: replies fail with 131047 | That phone has not messaged the test number in the last 24 hours. Send "hi" from it. |
| Meta: replies fail with 131030 | That phone is not in the test number's **To** list (API Setup). |
| Meta: `agent.check` shows code 190 | The access token expired. Paste a fresh one and restart the server. |

Demo-only command (disclosed): `#advance 8` fast-forwards the agent clock 8 days to show the reminder ladder.
