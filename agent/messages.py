"""All user-facing text. Hindi (default) and English.

Every factual line carries a provenance tag so the trader can always tell
what came from his bill, what came from his GSTR-2B, what MILAAN actually
did, and what is a general GST rule (always with a pointer to his CA).
"""

from __future__ import annotations

from datetime import datetime

TAGS = {
    "hi": {"bill": "📄बिल से", "2b": "🧾2B से", "did": "📤मैंने किया", "rule": "⚖️GST नियम—CA से पुष्टि करें",
           "you": "👤आपने बताया"},
    "en": {"bill": "📄from your bill", "2b": "🧾from your 2B", "did": "📤I did this",
           "rule": "⚖️GST rule—confirm with your CA", "you": "👤you told me"},
}


def tag(lang: str, key: str) -> str:
    return f"[{TAGS.get(lang, TAGS['hi'])[key]}]"


def rupees(paise: int | None) -> str:
    if paise is None:
        return "₹?"
    neg = paise < 0
    paise = abs(int(paise))
    r, p = divmod(paise, 100)
    s = str(r)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    out = f"₹{s}" + (f".{p:02d}" if p else "")
    return "-" + out if neg else out


def nice_date(iso: str | None) -> str:
    if not iso:
        return "?"
    try:
        return datetime.fromisoformat(iso[:10]).strftime("%d %b %Y")
    except ValueError:
        return iso


def period_label(period: str | None) -> str:
    if not period:
        return "?"
    try:
        return datetime.strptime(period, "%Y-%m").strftime("%b %Y")
    except ValueError:
        return period


FIELD_NAMES = {
    "hi": {"supplier_gstin": "विक्रेता का GSTIN", "invoice_number": "बिल नंबर",
           "invoice_date": "बिल की तारीख", "total_gst": "कुल GST राशि"},
    "en": {"supplier_gstin": "supplier GSTIN", "invoice_number": "invoice number",
           "invoice_date": "invoice date", "total_gst": "total GST amount"},
}


def bill_line(inv: dict, lang: str) -> str:
    sup = inv.get("supplier_name") or inv.get("supplier_gstin") or "?"
    if lang == "en":
        return (f"{sup} · Bill {inv.get('invoice_number')} · {nice_date(inv.get('invoice_date'))} · "
                f"GST {rupees(inv.get('total_gst_paise'))}")
    return (f"{sup} · बिल {inv.get('invoice_number')} · {nice_date(inv.get('invoice_date'))} · "
            f"GST {rupees(inv.get('total_gst_paise'))}")


# ---- bills ---------------------------------------------------------------------

def bill_ok(inv: dict, lang: str, pending_count: int) -> str:
    if lang == "en":
        return (f"✅ Bill recorded {tag(lang, 'bill')}\n{bill_line(inv, lang)}\n"
                f"Checks passed: GSTIN check digit ✓, tax arithmetic ✓, read twice ✓.\n"
                f"{pending_count} bill(s) this month are waiting for your GSTR-2B.")
    return (f"✅ बिल दर्ज हो गया {tag(lang, 'bill')}\n{bill_line(inv, lang)}\n"
            f"जाँच पास: GSTIN जाँच-अंक ✓, टैक्स का हिसाब ✓, दो बार पढ़ा ✓\n"
            f"इस महीने के {pending_count} बिल आपके GSTR-2B का इंतज़ार कर रहे हैं।")


def bill_blocked(inv: dict, clause: str, lang: str) -> str:
    if lang == "en":
        return (f"ℹ️ Bill recorded: {bill_line(inv, lang)}\nThis looks like blocked credit under §{clause} "
                f"{tag(lang, 'rule')}. I will not chase this supplier for it. If it is stock for resale, reply "
                f"'eligible {inv['id']}'.")
    return (f"ℹ️ बिल दर्ज: {bill_line(inv, lang)}\nयह §{clause} के तहत blocked credit लगता है "
            f"{tag(lang, 'rule')}। इसके लिए supplier से पीछा नहीं करूँगा। अगर यह दुकान का बिकने वाला माल है तो "
            f"'eligible {inv['id']}' लिखें।")


def not_a_bill(lang: str) -> str:
    if lang == "en":
        return "🤔 This photo does not look like a GST bill, so I have not recorded anything. Please send the bill photo."
    return "🤔 यह फ़ोटो GST बिल जैसी नहीं लग रही, इसलिए कुछ दर्ज नहीं किया। कृपया बिल की फ़ोटो भेजें।"


def duplicate_photo(inv: dict, lang: str) -> str:
    if lang == "en":
        return f"♻️ This exact photo was already recorded ({bill_line(inv, lang)}). Not counted twice."
    return f"♻️ यह फ़ोटो पहले ही दर्ज है ({bill_line(inv, lang)})। दोबारा नहीं गिना।"


def duplicate_invoice(inv: dict, lang: str) -> str:
    if lang == "en":
        return f"♻️ Same supplier + bill number already recorded ({bill_line(inv, lang)}). Not counted twice."
    return f"♻️ यही supplier और बिल नंबर पहले से दर्ज है ({bill_line(inv, lang)})। दोबारा नहीं गिना।"


def ask_field(field: str, current: str | None, reason: str, lang: str, has_crop: bool) -> str:
    name = FIELD_NAMES.get(lang, FIELD_NAMES["hi"]).get(field, field)
    look = ("(see the cropped photo)" if lang == "en" else "(कटी हुई फ़ोटो देखें)") if has_crop else ""
    if field == "supplier_gstin":
        if lang == "en":
            return (f"❓ I could not read the {name} reliably {look}. I read: {current or '—'} ({reason}).\n"
                    f"Please type the full 15-character GSTIN exactly as printed. I never guess a GSTIN, "
                    f"because a wrong one would mean messaging an innocent business.")
        return (f"❓ {name} पक्का नहीं पढ़ पाया {look}। मैंने पढ़ा: {current or '—'} ({reason})।\n"
                f"कृपया बिल पर छपा पूरा 15 अक्षर का GSTIN टाइप करें। मैं GSTIN का अंदाज़ा कभी नहीं लगाता — "
                f"गलत GSTIN मतलब किसी बेकसूर व्यापारी को मैसेज।")
    if lang == "en":
        return (f"❓ Please check the {name} on this bill {look}. I read: {current or '—'} ({reason}).\n"
                f"Reply with the correct value, or 'ok' if it is right.")
    return (f"❓ इस बिल पर {name} जाँच दीजिए {look}। मैंने पढ़ा: {current or '—'} ({reason})।\n"
            f"सही मान लिखकर भेजें, या सही है तो 'ok' लिखें।")


def reason_text(problems: list[dict], field: str, lang: str) -> str:
    codes = {p["code"] for p in problems if p["field"] == field or
             (field == "total_gst" and p["field"] in ("taxable_value", "invoice_total"))}
    hi = {"GSTIN_CHECKSUM": "जाँच-अंक मेल नहीं खाता", "GSTIN_FORMAT": "फ़ॉर्मेट गलत",
          "RATE_MISMATCH": "टैक्स दर से हिसाब मेल नहीं खाता", "INVOICE_TOTAL_MISMATCH": "कुल राशि मेल नहीं खाती",
          "TAX_TOTAL_MISMATCH": "CGST+SGST जोड़ मेल नहीं खाता", "REQUIRED_FIELD": "पढ़ा नहीं जा सका",
          "BUYER_SELLER_SWAPPED": "यह आपका अपना GSTIN है", "INVALID_DATE": "तारीख गलत"}
    en = {"GSTIN_CHECKSUM": "check digit fails", "GSTIN_FORMAT": "bad format",
          "RATE_MISMATCH": "tax does not match the rate", "INVOICE_TOTAL_MISMATCH": "total does not add up",
          "TAX_TOTAL_MISMATCH": "CGST+SGST does not add up", "REQUIRED_FIELD": "unreadable",
          "BUYER_SELLER_SWAPPED": "that is your own GSTIN", "INVALID_DATE": "invalid date"}
    table = en if lang == "en" else hi
    parts = [table[c] for c in codes if c in table]
    if not parts:
        parts = ["two independent reads disagree" if lang == "en" else "दो बार पढ़ने पर अलग-अलग आया"]
    return ", ".join(parts)


def field_fixed(inv: dict, lang: str) -> str:
    if lang == "en":
        return f"✅ Thanks, fixed {tag(lang, 'you')}. {bill_line(inv, lang)} — all checks pass now."
    return f"✅ धन्यवाद, ठीक कर दिया {tag(lang, 'you')}। {bill_line(inv, lang)} — अब सारी जाँच पास।"


def still_invalid(field: str, lang: str) -> str:
    name = FIELD_NAMES.get(lang, FIELD_NAMES["hi"]).get(field, field)
    if lang == "en":
        return f"⚠️ That {name} still fails the check. Please re-type it carefully from the bill."
    return f"⚠️ यह {name} अभी भी जाँच में फ़ेल है। कृपया बिल से ध्यान से दोबारा टाइप करें।"


# ---- 2B + diagnosis --------------------------------------------------------------

def twob_received(fmt: str, periods: list[str], accepted: int, rejected: int, lang: str) -> str:
    ps = ", ".join(period_label(p) for p in periods) or "?"
    if lang == "en":
        return (f"🧾 GSTR-2B received ({ps}, {fmt}). {accepted} rows read"
                + (f", {rejected} unreadable rows kept aside" if rejected else "") + ". Reconciling now…")
    return (f"🧾 GSTR-2B मिल गया ({ps})। {accepted} लाइनें पढ़ीं"
            + (f", {rejected} खराब लाइनें अलग रखीं" if rejected else "") + "। मिलान कर रहा हूँ…")


def reconcile_report(trader_invoices: list[dict], decisions: dict, lang: str) -> str:
    ids = {i["id"]: i for i in trader_invoices}
    def _sel(cause_list):
        out = [i for c in cause_list for i in decisions.get(c, []) if i in ids]
        if cause_list == ["MATCHED", "FORMAT_VARIANT", "ROUNDING"]:
            out = [i for i in out if ids[i]["state"] != "RECOVERED"]
        return out
    def total(cause_list):
        return sum((ids[i].get("total_gst_paise") or 0) for i in _sel(cause_list))
    def count(cause_list):
        return len(_sel(cause_list))
    matched = ["MATCHED", "FORMAT_VARIANT", "ROUNDING"]
    lines = []
    if lang == "en":
        lines.append(f"📊 Reconciliation {tag(lang, '2b')}")
        if count(matched):
            lines.append(f"✅ Matched: {count(matched)} bills, GST {rupees(total(matched))} — safe to claim")
        if decisions.get("FORMAT_VARIANT"):
            lines.append(f"   ↳ {len(decisions['FORMAT_VARIANT'])} matched despite a differently formatted bill number (no chase)")
        if decisions.get("QRMP_TIMING"):
            lines.append(f"⏳ Quarterly filer, expected later: {count(['QRMP_TIMING'])} bills, {rupees(total(['QRMP_TIMING']))} — NOT chasing, will re-check")
        if decisions.get("ITC_NOT_AVAILABLE"):
            lines.append(f"🚫 2B says ITC not available: {count(['ITC_NOT_AVAILABLE'])} bills")
        conf = count(["GSTIN_CONFLICT", "UNKNOWN_GSTIN", "INVALID_GSTIN"])
        if conf:
            lines.append(f"❓ Need your confirmation first: {conf} bills (questions follow)")
        chase = ["NON_FILING", "AMOUNT_MISMATCH"]
        if count(chase):
            lines.append(f"🔴 At risk, supplier must act: {count(chase)} bills, GST {rupees(total(chase))}")
        recovered = [i for i in decisions.get("MATCHED", []) + decisions.get("FORMAT_VARIANT", []) + decisions.get("ROUNDING", [])
                     if ids.get(i, {}).get("state") == "RECOVERED"]
        if recovered:
            lines.append(f"🎉 RECOVERED after follow-up: {len(recovered)} bills, {rupees(sum(ids[i]['total_gst_paise'] for i in recovered))}")
        return "\n".join(lines)
    lines.append(f"📊 मिलान का नतीजा {tag(lang, '2b')}")
    if count(matched):
        lines.append(f"✅ मिल गए: {count(matched)} बिल, GST {rupees(total(matched))} — claim सुरक्षित")
    if decisions.get("FORMAT_VARIANT"):
        lines.append(f"   ↳ {len(decisions['FORMAT_VARIANT'])} बिल का नंबर 2B में अलग ढंग से लिखा है, फिर भी वही बिल है (पीछा नहीं)")
    if decisions.get("QRMP_TIMING"):
        lines.append(f"⏳ तिमाही रिटर्न वाले supplier: {count(['QRMP_TIMING'])} बिल, {rupees(total(['QRMP_TIMING']))} — अभी आना ज़रूरी नहीं, पीछा नहीं करूँगा, बाद में दोबारा जाँचूँगा")
    if decisions.get("ITC_NOT_AVAILABLE"):
        lines.append(f"🚫 2B में ITC उपलब्ध नहीं: {count(['ITC_NOT_AVAILABLE'])} बिल")
    conf = count(["GSTIN_CONFLICT", "UNKNOWN_GSTIN", "INVALID_GSTIN"])
    if conf:
        lines.append(f"❓ पहले आपकी पुष्टि चाहिए: {conf} बिल (सवाल आगे भेज रहा हूँ)")
    chase = ["NON_FILING", "AMOUNT_MISMATCH"]
    if count(chase):
        lines.append(f"🔴 खतरे में, supplier को कुछ करना होगा: {count(chase)} बिल, GST {rupees(total(chase))}")
    recovered = [i for i in decisions.get("MATCHED", []) + decisions.get("FORMAT_VARIANT", []) + decisions.get("ROUNDING", [])
                 if ids.get(i, {}).get("state") == "RECOVERED"]
    if recovered:
        lines.append(f"🎉 पीछा करने के बाद वापस मिला: {len(recovered)} बिल, {rupees(sum(ids[i]['total_gst_paise'] for i in recovered))}")
    return "\n".join(lines)


def gstin_conflict_question(inv: dict, ev: dict, lang: str) -> str:
    if lang == "en":
        return (f"❓ Bill {inv['invoice_number']} ({rupees(inv['total_gst_paise'])}) from {inv.get('supplier_name') or '?'}:\n"
                f"• GSTIN on your bill {tag(lang, 'bill')}: {ev['bill_gstin']}\n"
                f"• Same bill in your 2B under {tag(lang, '2b')}: {ev['twob_gstin']}\n"
                f"Which is correct? Reply 'bill {inv['id']}' if the bill is right (I'll ask the supplier to amend their "
                f"return) or '2b {inv['id']}' if the bill was misprinted (then your credit is safe, just correct your books).")
    return (f"❓ बिल {inv['invoice_number']} ({rupees(inv['total_gst_paise'])}), {inv.get('supplier_name') or '?'}:\n"
            f"• आपके बिल पर GSTIN {tag(lang, 'bill')}: {ev['bill_gstin']}\n"
            f"• यही बिल आपके 2B में इस GSTIN से {tag(lang, '2b')}: {ev['twob_gstin']}\n"
            f"कौनसा सही है? बिल वाला सही है तो 'bill {inv['id']}' लिखें (मैं supplier से रिटर्न सुधारने को कहूँगा), "
            f"बिल पर गलत छपा है तो '2b {inv['id']}' लिखें (तब आपका credit सुरक्षित है, बस खाते में सुधार करें)।")


def unknown_gstin_question(inv: dict, lang: str) -> str:
    if lang == "en":
        return (f"❓ Bill {inv['invoice_number']} from {inv.get('supplier_name') or '?'} has GSTIN {inv['supplier_gstin']}, "
                f"which I have never seen in any of your 2Bs. Before I contact anyone: is this a supplier you "
                f"actually buy from? Reply 'confirm {inv['id']}' or 'wrong {inv['id']}'.")
    return (f"❓ बिल {inv['invoice_number']} ({inv.get('supplier_name') or '?'}) का GSTIN {inv['supplier_gstin']} "
            f"मैंने आपके किसी भी 2B में कभी नहीं देखा। किसी से संपर्क करने से पहले: क्या आप सच में इनसे माल लेते हैं? "
            f"'confirm {inv['id']}' या 'wrong {inv['id']}' लिखें।")


def invalid_gstin_note(inv: dict, lang: str) -> str:
    if lang == "en":
        return (f"⚠️ Bill {inv['invoice_number']}: GSTIN {inv['supplier_gstin']} fails the check digit and this bill is not "
                f"in your 2B. I will NOT message anyone about it. Please call the supplier and ask for a corrected bill.")
    return (f"⚠️ बिल {inv['invoice_number']}: GSTIN {inv['supplier_gstin']} जाँच-अंक में फ़ेल है और यह बिल 2B में नहीं है। "
            f"इसके बारे में मैं किसी को मैसेज नहीं करूँगा। कृपया supplier को फ़ोन करके सही बिल माँगें।")


def ask_supplier_phone(name: str, gstin: str, lang: str) -> str:
    if lang == "en":
        return (f"📇 I don't have a WhatsApp number for {name} ({gstin}). Send it like: "
                f"'number {gstin[-4:]} 98XXXXXXXX'.")
    return (f"📇 {name} ({gstin}) का WhatsApp नंबर मेरे पास नहीं है। ऐसे भेजें: 'number {gstin[-4:]} 98XXXXXXXX'")


def confirm_drafts(groups: list[dict], lang: str) -> str:
    total = sum(g["gst_paise"] for g in groups)
    if lang == "en":
        lines = [f"✋ Ready to message {len(groups)} supplier(s) about {rupees(total)} of your GST credit:"]
        for i, g in enumerate(groups, 1):
            kind = " (amount correction)" if g.get("amendment") else ""
            nudge = f" — reminder #{g['nudge']}" if g.get("nudge", 1) > 1 else ""
            lines.append(f"{i}. {g['name']} · {len(g['invoices'])} bill(s) · {rupees(g['gst_paise'])}{kind}{nudge}")
        lines.append("Nothing is sent without your OK. Reply 'haan' to send all, 'skip 2' to drop one, "
                     "'dikhao 1' to see a message, or 'nahi' to cancel.")
        return "\n".join(lines)
    lines = [f"✋ {len(groups)} supplier को आपके {rupees(total)} GST credit के लिए मैसेज भेजने को तैयार:"]
    for i, g in enumerate(groups, 1):
        kind = " (राशि सुधार)" if g.get("amendment") else ""
        nudge = f" — याद-दिहानी #{g['nudge']}" if g.get("nudge", 1) > 1 else ""
        lines.append(f"{i}. {g['name']} · {len(g['invoices'])} बिल · {rupees(g['gst_paise'])}{kind}{nudge}")
    lines.append("आपकी मंज़ूरी के बिना कुछ नहीं जाएगा। सब भेजने के लिए 'haan', किसी एक को हटाने के लिए 'skip 2', "
                 "मैसेज देखने के लिए 'dikhao 1', या रद्द करने के लिए 'nahi' लिखें।")
    return "\n".join(lines)


def supplier_message(trader_name: str, supplier_name: str, invoices: list[dict], nudge: int = 1) -> str:
    """Bilingual, neutral, factual. Facts are inserted verbatim from stored data."""
    lines_hi, lines_en = [], []
    for inv in invoices:
        extra_hi = extra_en = ""
        if inv.get("diagnosis") == "AMOUNT_MISMATCH" and inv.get("reported_gst_paise") is not None:
            extra_hi = f" (आपने {rupees(inv['reported_gst_paise'])} दिखाया है)"
            extra_en = f" (reported as {rupees(inv['reported_gst_paise'])})"
        lines_hi.append(f"• बिल {inv['invoice_number']}, {nice_date(inv['invoice_date'])}, GST {rupees(inv['total_gst_paise'])}{extra_hi}")
        lines_en.append(f"• Bill {inv['invoice_number']}, {nice_date(inv['invoice_date'])}, GST {rupees(inv['total_gst_paise'])}{extra_en}")
    total = sum(i["total_gst_paise"] for i in invoices)
    amend = any(i.get("diagnosis") == "AMOUNT_MISMATCH" for i in invoices)
    period = period_label(invoices[0].get("tax_period"))
    opener_hi = "नमस्ते" if nudge == 1 else f"नमस्ते, एक बार फिर याद दिला रहे हैं (#{nudge})"
    opener_en = "Hello" if nudge == 1 else f"Hello, a gentle reminder (#{nudge})"
    ask_hi = ("कृपया अपने GSTR-1 में इनकी राशि सुधार दें" if amend else
              "ये बिल हमारे GSTR-2B में नहीं दिख रहे। कृपया अपने GSTR-1/IFF में इन्हें दर्ज या सुधार दें")
    ask_en = ("please amend the amounts in your GSTR-1" if amend else
              "these bills do not appear in our GSTR-2B. Please report or correct them in your GSTR-1/IFF")
    return (
        f"{opener_hi} {supplier_name} जी, {trader_name} की ओर से।\n"
        f"{period} के ये बिल: \n" + "\n".join(lines_hi) + f"\nकुल GST {rupees(total)}। {ask_hi}।\n\n"
        f"{opener_en} {supplier_name}, on behalf of {trader_name}. For {period}:\n" + "\n".join(lines_en) +
        f"\nTotal GST {rupees(total)} — {ask_en}.\n\n"
        "यह सिर्फ़ रिकॉर्ड मिलान का अनुरोध है। / This is a record-matching request only.\n"
        "— MILAAN (sent with the buyer's approval)"
    )


def sent_report(results: list[dict], lang: str, when_iso: str | None = None) -> str:
    ok = [r for r in results if r["status"] == "SENT"]
    bad = [r for r in results if r["status"] != "SENT"]
    when = nice_date(when_iso or datetime.now().isoformat())
    if lang == "en":
        lines = [f"📤 Sent {len(ok)} message(s) on {when} {tag(lang, 'did')}:"]
        lines += [f"✓ {r['name']}" for r in ok]
        if bad:
            lines.append("✗ Could not send to: " + ", ".join(r["name"] for r in bad) + " — call scripts follow.")
        lines.append("I'll watch for replies and re-check against your next 2B.")
        return "\n".join(lines)
    lines = [f"📤 {when} को {len(ok)} मैसेज भेजे {tag(lang, 'did')}:"]
    lines += [f"✓ {r['name']}" for r in ok]
    if bad:
        lines.append("✗ इन्हें नहीं जा सका: " + ", ".join(r["name"] for r in bad) + " — फ़ोन के लिए स्क्रिप्ट आगे भेज रहा हूँ।")
    lines.append("जवाब पर नज़र रखूँगा और अगले 2B में दोबारा जाँचूँगा।")
    return "\n".join(lines)


def call_script(name: str, phone: str | None, invoices: list[dict], lang: str) -> str:
    total = sum(i["total_gst_paise"] for i in invoices)
    bills = "; ".join(f"{i['invoice_number']} ({nice_date(i['invoice_date'])}, {rupees(i['total_gst_paise'])})" for i in invoices)
    if lang == "en":
        return (f"📞 Call script for {name} {phone or ''}:\n\"Namaste, these bills of ours are not in our GSTR-2B: {bills}. "
                f"Total GST {rupees(total)}. Please add them in your next GSTR-1/IFF and tell me once filed.\"")
    return (f"📞 {name} {phone or ''} को फ़ोन के लिए:\n\"नमस्ते, हमारे ये बिल हमारे GSTR-2B में नहीं दिख रहे: {bills}। "
            f"कुल GST {rupees(total)}। कृपया अगले GSTR-1/IFF में डाल दीजिए और फ़ाइल होने पर बता दीजिए।\"")


def supplier_reply_relay(name: str, text: str, intent: str, promised: str | None, lang: str) -> str:
    label_hi = {"PROMISED": "करने का वादा किया", "ALREADY_FILED": "कह रहे हैं पहले ही फ़ाइल कर दिया",
                "DISPUTE": "असहमत हैं", "WRONG_PERSON": "कह रहे हैं गलत व्यक्ति", "OTHER": "जवाब दिया"}
    label_en = {"PROMISED": "promised to file", "ALREADY_FILED": "says it is already filed",
                "DISPUTE": "disputes it", "WRONG_PERSON": "says wrong person", "OTHER": "replied"}
    if lang == "en":
        when = f" by {nice_date(promised)}" if promised else ""
        nxt = {"PROMISED": "I'll re-check after that date.", "ALREADY_FILED": "I'll verify against your next 2B — not taking their word for it.",
               "DISPUTE": "Please talk to them; I've paused reminders.", "WRONG_PERSON": "Please send the right number; I've paused reminders."}.get(intent, "")
        return f"💬 {name} {label_en.get(intent, 'replied')}{when}:\n\"{text}\"\n{nxt}"
    when = f" ({nice_date(promised)} तक)" if promised else ""
    nxt = {"PROMISED": "उस तारीख के बाद दोबारा जाँचूँगा।", "ALREADY_FILED": "अगले 2B से खुद जाँचूँगा — सिर्फ़ उनकी बात पर भरोसा नहीं।",
           "DISPUTE": "कृपया उनसे बात करें; याद-दिहानी रोक दी है।", "WRONG_PERSON": "कृपया सही नंबर भेजें; याद-दिहानी रोक दी है।"}.get(intent, "")
    return f"💬 {name} ने {label_hi.get(intent, 'जवाब दिया')}{when}:\n\"{text}\"\n{nxt}"


def escalation(inv_groups: list[dict], lang: str) -> str:
    total = sum(g["gst_paise"] for g in inv_groups)
    if lang == "en":
        lines = [f"🚨 Escalation: {rupees(total)} still not reported after all reminders:"]
        lines += [f"• {g['name']}: {len(g['invoices'])} bill(s), {rupees(g['gst_paise'])}, claim deadline {nice_date(g['deadline'])} {tag(lang, 'rule')}" for g in inv_groups]
        lines.append("Your options: (1) call them (script below), (2) hold that amount from their next payment, "
                     "(3) ask your CA. I will stop messaging them.")
        return "\n".join(lines)
    lines = [f"🚨 सारी याद-दिहानी के बाद भी {rupees(total)} दर्ज नहीं हुआ:"]
    lines += [f"• {g['name']}: {len(g['invoices'])} बिल, {rupees(g['gst_paise'])}, claim की आखिरी तारीख {nice_date(g['deadline'])} {tag(lang, 'rule')}" for g in inv_groups]
    lines.append("आपके विकल्प: (1) फ़ोन करें (स्क्रिप्ट नीचे), (2) उनके अगले भुगतान से यह राशि रोकें, (3) अपने CA से बात करें। "
                 "मैं अब उन्हें मैसेज नहीं करूँगा।")
    return "\n".join(lines)


def status_report(invs: list[dict], lang: str) -> str:
    def s(states):
        sel = [i for i in invs if i["state"] in states]
        return len(sel), sum(i.get("total_gst_paise") or 0 for i in sel)
    total_n, total_g = len(invs), sum(i.get("total_gst_paise") or 0 for i in invs)
    m = s({"MATCHED"}); r = s({"RECOVERED"}); q = s({"QRMP_WAIT"}); w = s({"PENDING_2B"})
    risk = s({"TO_CHASE", "CHASED", "PROMISED", "CHANNEL_BLOCKED"}); esc = s({"ESCALATED", "SKIPPED"})
    ni = s({"NEEDS_INPUT", "INVALID"}); inel = s({"INELIGIBLE"})
    done = sum(1 for i in invs if i["state"] in {"MATCHED", "RECOVERED", "INELIGIBLE", "ESCALATED", "SKIPPED"})
    if lang == "en":
        return "\n".join([
            f"📒 Your GST credit status — {total_n} bills, GST {rupees(total_g)} {tag(lang, 'bill')}",
            f"✅ Safe (in 2B): {m[0]} · {rupees(m[1])}",
            f"🎉 Recovered by follow-up: {r[0]} · {rupees(r[1])}",
            f"🔴 Being chased: {risk[0]} · {rupees(risk[1])}",
            f"⏳ Quarterly filers, expected later: {q[0]} · {rupees(q[1])}",
            f"🕒 Waiting for 2B: {w[0]} · {rupees(w[1])}",
            f"❓ Need your input: {ni[0]} · {rupees(ni[1])}",
            f"🚫 Not claimable (§17(5)/2B): {inel[0]} · {rupees(inel[1])}",
            f"🚨 Handed back to you: {esc[0]} · {rupees(esc[1])}",
            f"Done: {done}/{total_n} bills in a final state."])
    return "\n".join([
        f"📒 आपके GST credit का हाल — {total_n} बिल, GST {rupees(total_g)} {tag(lang, 'bill')}",
        f"✅ सुरक्षित (2B में): {m[0]} · {rupees(m[1])}",
        f"🎉 पीछा करके वापस मिला: {r[0]} · {rupees(r[1])}",
        f"🔴 पीछा चल रहा है: {risk[0]} · {rupees(risk[1])}",
        f"⏳ तिमाही वाले, बाद में आएँगे: {q[0]} · {rupees(q[1])}",
        f"🕒 2B का इंतज़ार: {w[0]} · {rupees(w[1])}",
        f"❓ आपका जवाब चाहिए: {ni[0]} · {rupees(ni[1])}",
        f"🚫 claim नहीं होगा (§17(5)/2B): {inel[0]} · {rupees(inel[1])}",
        f"🚨 आपको सौंपे गए: {esc[0]} · {rupees(esc[1])}",
        f"पूरे हुए: {total_n} में से {done} बिल अंतिम स्थिति में।"])


def help_text(lang: str) -> str:
    if lang == "en":
        return ("🙏 I'm MILAAN. I recover GST input credit you are silently losing.\n"
                "1) Send me photos of your purchase bills.\n2) Forward your GSTR-2B file (JSON/Excel) when your CA gets it.\n"
                "3) I'll find missing bills, tell you why, and — only with your OK — message the supplier.\n"
                "Ask anything: 'kitna paisa atka hai?', 'status', 'Sharma ka number 98…'. Voice notes work too.\n"
                "I never file returns or log in to the GST portal — that stays with you and your CA.")
    return ("🙏 मैं MILAAN हूँ। आपका जो GST input credit चुपचाप डूब रहा है, उसे वापस लाने में मदद करता हूँ।\n"
            "1) खरीद के बिलों की फ़ोटो भेजें।\n2) CA से GSTR-2B मिले तो वो फ़ाइल (JSON/Excel) यहाँ forward करें।\n"
            "3) मैं गायब बिल ढूँढूँगा, कारण बताऊँगा, और सिर्फ़ आपकी मंज़ूरी से supplier को मैसेज करूँगा।\n"
            "कुछ भी पूछें: 'कितना पैसा अटका है?', 'status', 'शर्मा का नंबर 98…'। वॉइस नोट भी चलेगा।\n"
            "मैं रिटर्न फ़ाइल नहीं करता और GST पोर्टल में लॉगिन नहीं करता — वो काम आपका और आपके CA का है।")
