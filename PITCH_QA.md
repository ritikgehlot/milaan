# MILAAN: pitch timing and Q&A drill

## Timing (assumes a 7-minute slot; scale it if yours differs)
| Time | Slides | Who |
|---|---|---|
| 0:00–1:15 | Cover, User, Problem, Insight | Ritik |
| 1:15–2:15 | Why an agent, Architecture | Ritik |
| 2:15–5:00 | **Live demo** (phone mirrored) + trace | Ritik drives the trader phone, Daksh is the supplier |
| 5:00–6:15 | Trust, Failures, Eval 1, Eval 2 | Daksh |
| 6:15–7:00 | Real vs simulated, Close | Daksh |

Rules:
* If the demo stalls for more than 20 s, switch to `python -m agent.simulate --script demo` and keep talking.
* Never read a slide aloud.
* Say every number exactly as it appears in the results files.

## Q&A: answer in 2–3 sentences, then stop

### "Is this really an agent?"
**1. Isn't this just a reconciliation script with a WhatsApp front end?**
A script compares two lists. MILAAN keeps per-invoice state for weeks. It also picks among six different actions from the evidence, including doing nothing. It talks to two parties, interprets the supplier's reply, and schedules itself against the 2B date and the claim deadline. On our seeded set, a plain matcher (B2) chases 2× as many bills, at 0.40–0.45 precision against our 0.87–0.93.

**2. Where exactly is the LLM, and why isn't it deciding everything?**
It does four jobs: reading bill photos (twice), voice-to-text, turning Hinglish into tool calls, and interpreting supplier replies. Who gets contacted about money is decided by tested rules, because an LLM mistake there means accusing an innocent business. That split is deliberate.

**3. Why not let the LLM send messages?**
The worst failure available here is a wrong message to a third party. So the model has no send tool at all. Sending happens only after the trader's explicit "haan", and the policy is re-checked at send time.

**4. What did B1 (single-shot LLM) show?**
[Fill in from `agent_data/eval/b1_results.json`.] It tests whether our decomposition earns its complexity. MILAAN gets 9/9 on the same month.

### Real-world fit
**5. Why would a trader trust a bot messaging his suppliers?**
It never messages without his yes, and he can preview each message first. The message is neutral and bilingual, and says plainly it is a record-matching request. Every fact it tells him is tagged with its source.

**6. Won't suppliers get annoyed?**
That is exactly why we suppress chases for quarterly filers, format variants and rounding. B2 would have chased 143–154 innocent suppliers per 800 invoices; MILAAN 10–22. There is also a 3-reminder cap, after which it hands over to the trader.

**7. How does the trader get the GSTR-2B? He never opens the portal.**
His CA downloads it, or he does it once with help, and forwards the file into the same WhatsApp chat. The taxpayer is entitled to his own statement, so there is no scraping and no credential handling.

**8. Isn't the CA already doing this?**
At most once a year, in bulk, when the window to fix things is mostly gone. The per-supplier follow-up is too small to bill for. MILAAN does the in-between work and hands the CA a claim-ready statement.

**9. What about traders who don't read?**
Voice notes in work today. Spoken Hindi replies (text-to-speech) are next on our roadmap. Until then, text replies are short and emoji-anchored.

### Technical depth
**10. How do you stop a misread GSTIN from causing a wrong message?**
There are three independent signals: the check digit, a second independent read that must agree, and model confidence. If any fails, the trader gets a cropped image of that field and types it. Even after that, a GSTIN never seen in any 2B triggers a confirmation before anyone is contacted. In the seeded eval, MILAAN sent 0 messages on misread GSTINs; B2 sent 21–25.

**11. How do you detect QRMP (quarterly) suppliers?**
From that GSTIN's 2B history: appearing only in quarter-end months, over at least 2 periods. The trader can also just say "Thar Steel quarterly bharta hai". Known weakness: with one period of history we can't tell. That is our measured error case.

**12. How is the invoice-number formatting variance handled without merging different bills?**
It requires the same letter prefix (if both have one), the same final serial number, and the same GST amount within ₹1. Two different bills from one supplier with the same serial and same amount are the residual risk.

**13. What state do you keep, and where?**
A SQLite case table with one row per invoice and its state: needs-input, pending-2B, matched, QRMP-wait, to-chase, chased, promised, recovered, escalated, channel-blocked or skipped. It also stores pending confirmations, outbound messages with idempotency keys, 2B imports, supplier history and a full trace. It survives restarts.

**14. What happens if two messages arrive at once?**
Each phone has its own lock, and the database is serialized in-process. We tested concurrent webhook posts. Twilio gets an immediate empty response and the work runs in the background, so no webhook timeouts.

**15. How do you handle the 2B formats?**
There are two parsers. One reads the portal's JSON layout (b2b and amended b2ba sections, plus itcavl flags). The other is our tested normalized CSV/XLSX importer with row-level rejection. Honest caveat: the JSON parser is built to the published layout and has not yet been run on a live export.

**16. §17(5) blocked credit — how accurate is that?**
It's a conservative keyword screen on the goods description: food, vehicles, gifts, club memberships and similar. It never touches stock-in-trade like cement or pipes. The trader can override with "eligible N", and anything ambiguous goes to the CA. It is a guardrail against chasing credit that can't be claimed anyway, not tax advice.

### Evaluation rigour
**17. Your diagnosis eval data was made by you. Why should we believe it?**
You shouldn't treat it as real-world accuracy, and we say so on the slide. It shows the policy is implemented correctly on known perturbations. We report a held-out hard case where we fail, and we ran 3 seeds. The real test is the pilot.

**18. Extraction numbers are on synthetic images. Are real photos harder?**
Yes. Real bills have thermal fade, handwriting and folds. That's why the headline metric is unsafe passes, which the gates control, rather than raw accuracy. The first pilot measurement is that same metric on real photos.

**19. What is your cost per completed task?**
Model cost per bill is [fill in from the extraction eval] for two reads. A trader-month is roughly that × 20–40 bills plus a few chat turns, plus WhatsApp message charges outside the sandbox. Token usage for every call is in the trace.

**20. What is "task completion" for you?**
Every invoice reaches a terminal state: matched, recovered, ineligible, escalated with options, or skipped by the trader. In the demo month 8 of 9 are terminal. The 9th is the quarterly filer, correctly waiting for its quarter.

### Safety, law, privacy
**21. Is it legal to message suppliers on someone's behalf?**
It is the trader's own business correspondence, sent only with his per-cycle approval, from a channel he set up. MILAAN gives no tax advice and never files anything. Filing is the legally human step and stays with him and his CA.

**22. What personal data do you store?**
Only reconciliation fields and supplier contact numbers the trader gave us. No bank details. Phone numbers are masked in the trace, and demo data is synthetic. Unknown senders are ignored, and webhook signatures are verified.

**23. What if the supplier lies ("already filed")?**
We don't take their word. The case stays open and is verified against the next 2B.

### Feasibility and future
**24. Why Twilio and not the Meta Cloud API?**
Our core repo already has a Meta Cloud API sender. For the finale, the Twilio sandbox gave us reliable two-way WhatsApp, including media. The channel is one interface, so switching back is a config change plus the template-approval process.

**25. How does this scale and make money?**
Per-trader cost is tokens plus messages. CAs who serve 50–200 traders are the natural channel. Pricing as a share of the credit recovered aligns incentives. We'd validate willingness to pay in the pilot before claiming a number.

**26. What would you build next with 2 more weeks?**
In order:
1. A real pilot with one trader.
2. Spoken Hindi replies.
3. A portal-export test set from consenting CAs.
4. A CA view across traders.
