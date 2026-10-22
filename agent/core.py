"""MILAAN agent: one inbound WhatsApp message -> a traced, multi-step turn.

Layering (the safety story in one sentence): the LLM reads and talks; the
rules decide; the trader approves; only then does anything leave.

* Perception (LLM): bill photos, voice notes, free-text Hindi/Hinglish,
  supplier replies.
* Decision (deterministic): validation, reconciliation, diagnosis,
  eligibility, and every send gate.
* Consent (human): every third-party message needs an explicit 'haan'
  from the trader for that cycle. The LLM has no send tool at all.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import validation

from . import diagnose, extract, messages as M, store
from .channel import ConsoleChannel, MetaChannel, TwilioChannel, send_with_retry
from .config import Settings, get_settings, normalize_phone
from .llm import LLMClient, LLMUnavailable
from . import twob

YES = re.compile(r"^\s*(haan|haa|han|ha|hanji|haanji|ji|yes|y|ok send|send|bhejo|bhej do|kar do|"
                 r"हाँ|हां|हा|जी|भेजो|भेज दो|ठीक है भेजो)\s*[.!🙏👍]*\s*$", re.I)
NO = re.compile(r"^\s*(nahi|nahin|na|no|cancel|mat bhejo|ruko|rehne do|नहीं|ना|मत भेजो|रुको)\s*[.!]*\s*$", re.I)
OKAY = re.compile(r"^\s*(ok|okay|sahi|sahi hai|theek|thik|correct|right|yes|haan|ha|ठीक|सही|सही है|हाँ)\s*[.!]*\s*$", re.I)
DEVANAGARI = re.compile(r"[ऀ-ॿ]")

_phone_locks: dict[str, threading.Lock] = defaultdict(threading.Lock)


class Agent:
    def __init__(self, channel=None, llm: LLMClient | None = None, settings: Settings | None = None,
                 fixture_extractor=None):
        self.s = settings or get_settings()
        if channel is None:  # send_mode 'twilio' = really send, through WHATSAPP_PROVIDER
            if self.s.send_mode != "twilio":
                channel = ConsoleChannel()
            elif self.s.whatsapp_provider == "meta":
                channel = MetaChannel(self.s)
            else:
                channel = TwilioChannel(self.s)
        self.channel = channel
        self.supplier_channel = channel
        if isinstance(channel, TwilioChannel) and self.s.supplier_twilio_enabled:
            self.supplier_channel = TwilioChannel(self.s, account=(
                self.s.supplier_twilio_account_sid, self.s.supplier_twilio_auth_token,
                self.s.supplier_twilio_whatsapp_from))
        self.llm = llm or LLMClient(self.s)
        self.fixture_extractor = fixture_extractor  # offline mock for demos/tests (disclosed)

    # ------------------------------------------------------------------ helpers
    def role_of(self, phone: str) -> str:
        if phone in self.s.trader_phones:
            return "trader"
        if store.find_supplier_by_phone(phone):
            return "supplier"
        return "trader" if not self.s.trader_phones else "unknown"

    def say(self, trader: str, to: str, body: str, turn: str, media_url: str | None = None,
            role: str = "trader") -> bool:
        ch = self.supplier_channel if role == "supplier" else self.channel
        result, attempts = send_with_retry(ch, to, body, media_url, attempts=3, base_delay=0.5)
        store.log_message(to, role, "out", body, "image" if media_url else None)
        store.trace(trader, turn, "MESSAGE", f"send:{role}",
                    {"to": to[-4:].rjust(len(to), "•"), "ok": result.ok, "attempts": attempts,
                     "error": result.error, "chars": len(body)})
        return result.ok

    def lang(self, trader: str) -> str:
        t = store.get_trader(trader)
        return (t or {}).get("lang") or "hi"

    def media_public_url(self, path: Path | None) -> str | None:
        if not path:
            return None
        if self.s.public_url:
            return f"{self.s.public_url}/media/{path.name}"
        return str(path) if isinstance(self.channel, ConsoleChannel) else None

    # ------------------------------------------------------------------ entry
    def handle(self, from_phone: str, body: str = "", media: list[tuple[str, str]] | None = None,
               file_names: list[str] | None = None) -> None:
        phone = normalize_phone(from_phone)
        with _phone_locks[phone]:
            turn = uuid.uuid4().hex[:10]
            role = self.role_of(phone)
            store.log_message(phone, role, "in", body, ",".join(m[1] for m in media or []) or None)
            store.trace(phone if role != "supplier" else None, turn, "INBOUND", role,
                        {"text": body[:500], "media": [m[1] for m in media or []]})
            try:
                if role == "supplier":
                    self.handle_supplier(phone, body, turn)
                elif role == "trader":
                    self.handle_trader(phone, body, media or [], file_names or [], turn)
                else:
                    store.trace(None, turn, "POLICY", "ignored_unknown_sender", {"phone_tail": phone[-4:]})
            except Exception as exc:  # never leave the user without an answer
                store.trace(phone, turn, "ERROR", type(exc).__name__, {"error": str(exc)[:500]})
                lang = self.lang(phone) if role == "trader" else "hi"
                self.say(phone, phone, "⚠️ कुछ गड़बड़ हुई, आपका डेटा सुरक्षित है। कृपया एक बार फिर भेजें।"
                         if lang == "hi" else "⚠️ Something went wrong; your data is safe. Please resend.", turn)

    # ------------------------------------------------------------------ trader
    def handle_trader(self, trader: str, body: str, media: list[tuple[str, str]], names: list[str],
                      turn: str) -> None:
        store.ensure_trader(trader)
        if body and DEVANAGARI.search(body):
            store.set_trader_lang(trader, "hi")
        lang = self.lang(trader)

        for i, (url, mime) in enumerate(media):
            mime = (mime or "").lower()
            data = self.channel.download(url)
            store.trace(trader, turn, "TOOL_CALL", "download_media", {"mime": mime, "bytes": len(data)})
            fname = names[i] if i < len(names) else url.rsplit("/", 1)[-1]
            if mime.startswith("image/"):
                self.handle_bill(trader, data, mime, turn)
            elif mime.startswith("audio/"):
                text = self.transcribe(trader, data, mime, turn)
                if text:
                    self.say(trader, trader, f"🎙️ {'सुना' if lang == 'hi' else 'Heard'}: \"{text}\"", turn)
                    body = f"{body} {text}".strip()
            elif mime == "application/pdf":
                self.say(trader, trader, "📎 PDF मिला। GSTR-2B के लिए कृपया पोर्टल से JSON या Excel फ़ाइल भेजें; "
                         "बिल हो तो फ़ोटो भेजें।" if lang == "hi" else
                         "📎 Got a PDF. For GSTR-2B please send the portal JSON or Excel; for a bill, send a photo.", turn)
            else:
                self.handle_2b(trader, data, fname, mime, turn)
        if body:
            self.handle_text(trader, body, turn)

    def transcribe(self, trader: str, data: bytes, mime: str, turn: str) -> str | None:
        try:
            text = self.llm.transcribe(data, mime)
            store.trace(trader, turn, "TOOL_CALL", "speech_to_text", {"chars": len(text)})
            return text
        except Exception as exc:
            store.trace(trader, turn, "ERROR", "speech_to_text", {"error": str(exc)[:200]})
            self.say(trader, trader, "🎙️ वॉइस नोट समझ नहीं पाया, कृपया लिखकर भेजें।" if self.lang(trader) == "hi"
                     else "🎙️ Couldn't understand the voice note, please type it.", turn)
            return None

    # ------------------------------------------------------------------ bills
    def handle_bill(self, trader: str, image: bytes, mime: str, turn: str, retry_of: int | None = None) -> None:
        lang = self.lang(trader)
        sha = extract.sha256(image)
        dup = store.find_invoice_by_media(trader, sha)
        if dup:
            self.say(trader, trader, M.duplicate_photo(dup, lang), turn)
            return
        self.s.media_dir.mkdir(parents=True, exist_ok=True)
        img_path = self.s.media_dir / f"bill-{sha[:16]}.{mime.split('/')[-1].split(';')[0] or 'jpg'}"
        img_path.write_bytes(image)
        try:
            if self.fixture_extractor is not None:
                ex = self.fixture_extractor(image)
                store.trace(trader, turn, "TOOL_CALL", "extract_bill[fixture-mock]", {"status": ex.status})
            elif self.llm.enabled:
                ex = extract.extract_with_llm(image, mime, self.llm, trader_gstin=None,
                                              double_read=self.s.extract_double_read)
                store.trace(trader, turn, "TOOL_CALL", "extract_bill[vision-llm]",
                            {"status": ex.status, "uncertain": ex.uncertain, "usage": ex.usage,
                             "problems": [p["code"] for p in ex.problems]})
            else:
                raise LLMUnavailable("no extractor configured")
        except LLMUnavailable as exc:
            store.create_pending(trader, f"RETRY_EXTRACT:{sha[:16]}",
                                 {"path": str(img_path), "mime": mime, "attempts": 1 if retry_of is None else retry_of + 1})
            store.trace(trader, turn, "ERROR", "extract_bill", {"error": str(exc)[:200], "queued_retry": True})
            self.say(trader, trader, "⏳ बिल की फ़ोटो सुरक्षित रख ली है, पर अभी पढ़ने वाली सेवा जवाब नहीं दे रही। "
                     "थोड़ी देर में अपने-आप दोबारा कोशिश करूँगा — आपको फिर से भेजने की ज़रूरत नहीं।" if lang == "hi" else
                     "⏳ Saved your bill photo, but the reader service is not responding. I'll retry automatically — "
                     "no need to resend.", turn)
            return

        if ex.status == "REJECTED":
            self.say(trader, trader, M.not_a_bill(lang), turn)
            return
        f = ex.fields
        if f.get("supplier_gstin") and f.get("invoice_number") and f.get("tax_period"):
            d = store.find_duplicate_invoice(trader, f["supplier_gstin"], f["invoice_number"], f["tax_period"])
            if d and ex.status == "VALID":
                self.say(trader, trader, M.duplicate_invoice(d, lang), turn)
                return
        eligibility, clause = extract.classify_eligibility(f.get("description"))
        state = store.NEEDS_INPUT if ex.status == "NEEDS_INPUT" else store.PENDING_2B
        if eligibility == "BLOCKED" and state != store.NEEDS_INPUT:
            state = store.INELIGIBLE
        inv_id = store.insert_invoice(
            trader, media_sha=sha, image_path=str(img_path), provenance="photo", state=state,
            pending_field=ex.ask_field, eligibility=eligibility,
            state_reason=clause if eligibility == "BLOCKED" else "",
            extraction_json=json.dumps({"raw": ex.raw, "uncertain": ex.uncertain, "problems": ex.problems},
                                       ensure_ascii=False, default=str),
            **{k: f.get(k) for k in ("supplier_name", "supplier_gstin", "invoice_number", "invoice_date",
                                     "tax_period", "taxable_paise", "cgst_paise", "sgst_paise", "igst_paise",
                                     "total_gst_paise", "invoice_total_paise", "gst_rate", "description")})
        if f.get("supplier_gstin") and validation.validate_gstin(f["supplier_gstin"]).is_format_valid \
                and ex.status == "VALID":
            store.upsert_supplier(trader, f["supplier_gstin"], f.get("supplier_name"))
        inv = store.get_invoice(inv_id)
        store.trace(trader, turn, "DECISION", "bill_recorded",
                    {"invoice_id": inv_id, "state": state, "eligibility": eligibility, "clause": clause})
        if state == store.NEEDS_INPUT:
            self.ask_next_field(trader, inv, ex, image, turn)
        elif state == store.INELIGIBLE:
            self.say(trader, trader, M.bill_blocked(inv, clause, lang), turn)
        else:
            pending = len(store.get_invoices(trader, states=[store.PENDING_2B]))
            self.say(trader, trader, M.bill_ok(inv, lang, pending), turn)

    def ask_next_field(self, trader: str, inv: dict, ex: extract.Extraction | None, image: bytes | None,
                       turn: str) -> None:
        lang = self.lang(trader)
        meta = json.loads(inv.get("extraction_json") or "{}")
        fld = inv.get("pending_field")
        problems = meta.get("problems", [])
        current = {"supplier_gstin": inv.get("supplier_gstin"), "invoice_number": inv.get("invoice_number"),
                   "invoice_date": inv.get("invoice_date"),
                   "total_gst": M.rupees(inv.get("total_gst_paise")) if inv.get("total_gst_paise") is not None else None
                   }.get(fld)
        crop_url = None
        if image is None and inv.get("image_path") and Path(inv["image_path"]).exists():
            image = Path(inv["image_path"]).read_bytes()
        box = ((meta.get("raw") or {}).get("boxes") or {}).get(fld)
        if image and box:
            crop = extract.crop_field(image, box, self.s.media_dir)
            crop_url = self.media_public_url(crop)
        header = f"🧾 {inv.get('supplier_name') or ''} · {inv.get('invoice_number') or ''}".strip(" ·")
        self.say(trader, trader, f"{header}\n" + M.ask_field(fld, current, M.reason_text(problems, fld, lang), lang,
                                                             bool(crop_url)), turn, media_url=crop_url)
        store.trace(trader, turn, "DECISION", "targeted_reask", {"invoice_id": inv["id"], "field": fld,
                                                               "crop_sent": bool(crop_url)})

    def answer_field(self, trader: str, inv: dict, text: str, turn: str) -> bool:
        """Trader replies to a targeted re-ask. Returns True if consumed."""
        lang = self.lang(trader)
        fld = inv["pending_field"]
        meta = json.loads(inv.get("extraction_json") or "{}")
        uncertain = [u for u in meta.get("uncertain", []) if u != fld]
        update: dict = {}
        accepted_as_is = bool(OKAY.match(text))
        if fld == "supplier_gstin":
            g = extract.find_gstin_in_text(text)
            if g:
                if not validation.validate_gstin(g).is_format_valid:
                    self.say(trader, trader, M.still_invalid(fld, lang), turn)
                    return True
                update["supplier_gstin"] = g
            elif accepted_as_is:
                if not validation.validate_gstin(inv.get("supplier_gstin")).is_format_valid:
                    # printed GSTIN itself is wrong: never message on it; look for the bill by number in 2B later
                    update["provenance"] = "trader_confirmed_misprint"
            else:
                return False
        elif fld == "total_gst":
            if accepted_as_is:
                pass
            else:
                if not re.fullmatch(r"\s*(₹|rs\.?|inr)?\s*[\d,]+(\.\d{1,2})?\s*(/-)?\s*", text, re.I):
                    return False
                paise = extract.money_to_paise(re.sub(r"[^\d.]", "", text))
                if paise is None:
                    return False
                update["total_gst_paise"] = paise
                if (inv.get("igst_paise") or 0) > 0:
                    update.update(igst_paise=paise, cgst_paise=0, sgst_paise=0)
                else:
                    update.update(cgst_paise=paise // 2, sgst_paise=paise - paise // 2, igst_paise=0)
        elif fld == "invoice_number":
            if not accepted_as_is:
                cand = text.strip()
                if len(cand.split()) > 2 or not re.search(r"\d", cand) or len(cand) > 40:
                    return False
                update["invoice_number"] = cand
        elif fld == "invoice_date":
            if not accepted_as_is:
                d = extract.parse_date(text.strip())
                if not d:
                    return False
                update.update(invoice_date=d, tax_period=d[:7])
        else:
            return False
        meta["uncertain"] = uncertain
        meta.setdefault("trader_answers", {})[fld] = text[:60]
        update["extraction_json"] = json.dumps(meta, ensure_ascii=False, default=str)
        if uncertain:
            update["pending_field"] = uncertain[0]
        else:
            update["pending_field"] = None
            update["state"] = store.INELIGIBLE if inv["eligibility"] == "BLOCKED" else store.PENDING_2B
        if "provenance" not in update:
            update["provenance"] = "photo+trader"
        store.update_invoice(inv["id"], **update)
        inv = store.get_invoice(inv["id"])
        store.trace(trader, turn, "TOOL_CALL", "apply_trader_correction",
                    {"invoice_id": inv["id"], "field": fld, "accepted_as_is": accepted_as_is})
        if inv.get("supplier_gstin") and validation.validate_gstin(inv["supplier_gstin"]).is_format_valid:
            store.upsert_supplier(trader, inv["supplier_gstin"], inv.get("supplier_name"))
        if uncertain:
            self.ask_next_field(trader, inv, None, None, turn)
        else:
            self.say(trader, trader, M.field_fixed(inv, lang), turn)
            if store.get_2b_imports(trader):
                self.reconcile_and_plan(trader, turn)
        return True

    # ------------------------------------------------------------------ GSTR-2B
    def handle_2b(self, trader: str, data: bytes, name: str, mime: str, turn: str) -> None:
        lang = self.lang(trader)
        try:
            parsed = twob.parse_any(data, name, mime)
        except Exception as exc:
            store.trace(trader, turn, "ERROR", "parse_gstr2b", {"error": str(exc)[:300]})
            self.say(trader, trader, "⚠️ यह फ़ाइल GSTR-2B जैसी नहीं पढ़ी जा सकी। पोर्टल से डाउनलोड की हुई JSON/Excel भेजें।"
                     if lang == "hi" else "⚠️ Could not read this as a GSTR-2B. Send the portal JSON/Excel.", turn)
            return
        store.trace(trader, turn, "TOOL_CALL", "parse_gstr2b",
                    {"format": parsed.source_format, "accepted": len(parsed.rows),
                     "rejected": len(parsed.rejected), "periods": parsed.return_periods})
        if not parsed.rows:
            self.say(trader, trader, "⚠️ इस 2B में कोई पढ़ने लायक लाइन नहीं मिली।" if lang == "hi"
                     else "⚠️ No readable rows in this 2B.", turn)
            return
        store.save_2b_import(trader, name, parsed.file_hash, parsed.source_format, parsed.rows, len(parsed.rejected))
        header = M.twob_received(parsed.source_format, parsed.return_periods, len(parsed.rows),
                                 len(parsed.rejected), lang)
        self.reconcile_and_plan(trader, turn, header=header)

    def reconcile_and_plan(self, trader: str, turn: str, header: str | None = None) -> None:
        lang = self.lang(trader)
        result = diagnose.run(trader, turn_id=turn)
        store.trace(trader, turn, "TOOL_CALL", "reconcile_and_diagnose", result)
        if result["decisions"]:
            report = M.reconcile_report(store.get_invoices(trader), result["decisions"], lang)
            self.say(trader, trader, f"{header}\n\n{report}" if header else report, turn)
        elif header:
            self.say(trader, trader, header, turn)
        for cause in ("GSTIN_CONFLICT", "UNKNOWN_GSTIN", "INVALID_GSTIN"):
            for iid in result["decisions"].get(cause, []):
                inv = store.get_invoice(iid)
                ev = self._last_evidence(iid)
                if cause == "GSTIN_CONFLICT":
                    self.say(trader, trader, M.gstin_conflict_question(inv, ev, lang), turn)
                elif cause == "UNKNOWN_GSTIN":
                    self.say(trader, trader, M.unknown_gstin_question(inv, lang), turn)
                else:
                    self.say(trader, trader, M.invalid_gstin_note(inv, lang), turn)
        self.plan_followups(trader, turn)

    def _last_evidence(self, invoice_id: int) -> dict:
        with store.conn() as c:
            rows = c.execute("SELECT payload FROM agent_trace WHERE kind='DECISION' AND name LIKE 'diagnose:%' "
                             "ORDER BY id DESC LIMIT 200").fetchall()
        for r in rows:
            p = json.loads(r["payload"])
            if p.get("invoice_id") == invoice_id:
                return p.get("evidence", {})
        return {}

    # ------------------------------------------------------------------ follow-ups
    def plan_followups(self, trader: str, turn: str, quiet_if_none: bool = True) -> dict | None:
        lang = self.lang(trader)
        to_chase = store.get_invoices(trader, states=[store.TO_CHASE])
        groups: dict[str, dict] = {}
        asked_phone = set()
        for inv in to_chase:
            g = inv["supplier_gstin"]
            sup = store.get_supplier(trader, g) or {}
            if not sup.get("phone"):
                if g not in asked_phone:
                    self.say(trader, trader, M.ask_supplier_phone(inv.get("supplier_name") or g, g, lang), turn)
                    asked_phone.add(g)
                continue
            grp = groups.setdefault(g, {"gstin": g, "name": sup.get("name") or inv.get("supplier_name") or g,
                                        "phone": sup["phone"], "invoices": [], "gst_paise": 0, "nudge": 1,
                                        "amendment": False})
            grp["invoices"].append(inv["id"])
            grp["gst_paise"] += inv["total_gst_paise"] or 0
            grp["nudge"] = max(grp["nudge"], (inv.get("chase_count") or 0) + 1)
            grp["amendment"] = grp["amendment"] or inv.get("diagnosis") == "AMOUNT_MISMATCH"
        if not groups:
            stale = store.get_pending(trader, "SEND_FOLLOWUPS")
            if stale:
                store.resolve_pending(stale["id"], "SUPERSEDED")
            return None
        ordered = sorted(groups.values(), key=lambda x: -x["gst_paise"])
        pid = store.create_pending(trader, "SEND_FOLLOWUPS", {"groups": ordered})
        store.trace(trader, turn, "DECISION", "drafts_ready_awaiting_consent",
                    {"pending_id": pid, "groups": [{"gstin": g["gstin"], "n": len(g["invoices"]),
                                                    "gst_paise": g["gst_paise"]} for g in ordered]})
        self.say(trader, trader, M.confirm_drafts(ordered, lang), turn)
        return {"pending_id": pid, "groups": ordered}

    def _draft_body(self, trader: str, group: dict) -> str:
        t = store.get_trader(trader) or {}
        invs = []
        for iid in group["invoices"]:
            inv = store.get_invoice(iid)
            if inv.get("diagnosis") == "AMOUNT_MISMATCH":
                ev = self._last_evidence(iid)
                inv["reported_gst_paise"] = ev.get("row_gst_paise")
            invs.append(inv)
        return M.supplier_message(t.get("name") or "आपके ग्राहक / your customer", group["name"], invs,
                                  nudge=group.get("nudge", 1))

    def policy_check(self, trader: str, group: dict) -> list[str]:
        """Hard gates re-checked at send time. Any problem blocks the send."""
        problems = []
        if not validation.validate_gstin(group["gstin"]).is_format_valid:
            problems.append("GSTIN fails check digit")
        sup = store.get_supplier(trader, group["gstin"]) or {}
        if not sup.get("phone"):
            problems.append("no supplier phone")
        for iid in group["invoices"]:
            inv = store.get_invoice(iid)
            if inv is None or inv["state"] != store.TO_CHASE:
                problems.append(f"invoice {iid} no longer TO_CHASE")
            elif inv["eligibility"] != "ELIGIBLE":
                problems.append(f"invoice {iid} not eligible")
            elif inv.get("supplier_gstin") != group["gstin"]:
                problems.append(f"invoice {iid} GSTIN changed")
        return problems

    def execute_sends(self, trader: str, pending: dict, turn: str) -> None:
        lang = self.lang(trader)
        store.resolve_pending(pending["id"], "APPROVED")
        store.trace(trader, turn, "CONSENT", "trader_approved", {"pending_id": pending["id"]})
        results = []
        for group in pending["payload"]["groups"]:
            problems = self.policy_check(trader, group)
            store.trace(trader, turn, "POLICY", "pre_send_check", {"gstin": group["gstin"], "problems": problems})
            if problems:
                results.append({"name": group["name"], "status": "BLOCKED"})
                continue
            body = self._draft_body(trader, group)
            out = store.create_outbound(trader, group["phone"], "supplier", body, supplier_gstin=group["gstin"],
                                        invoice_ids=group["invoices"],
                                        idempotency_key=f"p{pending['id']}-{group['gstin']}")
            if out["status"] == "SENT":
                results.append({"name": group["name"], "status": "SENT"})
                continue
            res, attempts = send_with_retry(self.supplier_channel, group["phone"], body, attempts=3, base_delay=0.5)
            store.update_outbound(out["id"], status="SENT" if res.ok else "FAILED", attempts=attempts,
                                  provider_id=res.provider_id, error=res.error)
            store.log_message(group["phone"], "supplier", "out", body)
            store.trace(trader, turn, "TOOL_CALL", "send_whatsapp_to_supplier",
                        {"gstin": group["gstin"], "ok": res.ok, "attempts": attempts, "error": res.error,
                         "invoices": group["invoices"]})
            nxt = (store.now() + timedelta(days=self.s.chase_interval_days)).strftime("%Y-%m-%d %H:%M:%S")
            for iid in group["invoices"]:
                inv = store.get_invoice(iid)
                if res.ok:
                    store.update_invoice(iid, state=store.CHASED, chase_count=(inv["chase_count"] or 0) + 1,
                                         last_chased_at=store.now_text(), next_check_at=nxt)
                else:
                    store.update_invoice(iid, state=store.CHANNEL_BLOCKED, state_reason=res.error or "")
            results.append({"name": group["name"], "status": "SENT" if res.ok else "FAILED"})
            if not res.ok:
                invs = [store.get_invoice(i) for i in group["invoices"]]
                self.say(trader, trader, M.call_script(group["name"], group["phone"], invs, lang), turn)
        self.say(trader, trader, M.sent_report(results, lang, store.now_text()), turn)

    # ------------------------------------------------------------------ text
    def handle_text(self, trader: str, text: str, turn: str) -> None:
        lang = self.lang(trader)
        t = text.strip()
        low = t.lower()

        if self.s.demo_commands and low.startswith("#"):
            return self.demo_command(trader, low, turn)
        if low in {"english", "eng", "angrezi"}:
            store.set_trader_lang(trader, "en")
            return self.say(trader, trader, "OK, English from now on.", turn) and None
        if low in {"hindi", "हिंदी", "हिन्दी"}:
            store.set_trader_lang(trader, "hi")
            return self.say(trader, trader, "ठीक है, अब हिंदी में।", turn) and None

        pending = store.get_pending(trader, "SEND_FOLLOWUPS")
        if pending:
            if YES.match(t):
                return self.execute_sends(trader, pending, turn)
            if NO.match(t):
                store.resolve_pending(pending["id"], "REJECTED")
                store.trace(trader, turn, "CONSENT", "trader_declined", {"pending_id": pending["id"]})
                return self.say(trader, trader, "👍 ठीक है, कुछ नहीं भेजा। जब चाहें 'bhejo' लिखें।" if lang == "hi"
                                else "👍 OK, nothing sent. Say 'send' whenever you want.", turn) and None
            m = re.match(r"^\s*(skip|hatao|हटाओ)\s+(\d+)\s*$", low)
            if m:
                return self.skip_group(trader, pending, int(m.group(2)), turn)
            m = re.match(r"^\s*(dikhao|show|दिखाओ)\s+(\d+)\s*$", low)
            if m:
                groups = pending["payload"]["groups"]
                k = int(m.group(2))
                if 1 <= k <= len(groups):
                    return self.say(trader, trader, f"👀 #{k} → {groups[k-1]['name']}:\n\n" +
                                    self._draft_body(trader, groups[k - 1]), turn) and None

        waiting = [i for i in store.get_invoices(trader, states=[store.NEEDS_INPUT])
                   if i.get("pending_field") in ("supplier_gstin", "total_gst", "invoice_number", "invoice_date")]
        if waiting and self.answer_field(trader, waiting[-1], t, turn):
            return

        if self.command(trader, t, turn):
            return
        self.converse(trader, t, turn)

    def skip_group(self, trader: str, pending: dict, k: int, turn: str) -> None:
        lang = self.lang(trader)
        groups = pending["payload"]["groups"]
        if not 1 <= k <= len(groups):
            return
        g = groups.pop(k - 1)
        for iid in g["invoices"]:
            store.update_invoice(iid, state=store.SKIPPED, state_reason="trader chose not to chase")
        store.resolve_pending(pending["id"], "EDITED")
        store.trace(trader, turn, "CONSENT", "trader_skipped_supplier", {"gstin": g["gstin"]})
        if groups:
            store.create_pending(trader, "SEND_FOLLOWUPS", {"groups": groups})
            self.say(trader, trader, M.confirm_drafts(groups, lang), turn)
        else:
            self.say(trader, trader, "👍 हटा दिया। भेजने को अब कुछ नहीं बचा।" if lang == "hi" else
                     "👍 Removed. Nothing left to send.", turn)

    def command(self, trader: str, t: str, turn: str) -> bool:
        lang = self.lang(trader)
        low = t.lower().strip()
        m = re.match(r"^(bill|2b|confirm|wrong|eligible)\s+(\d+)$", low)
        if m:
            verb, iid = m.group(1), int(m.group(2))
            inv = store.get_invoice(iid)
            if not inv or inv["trader_phone"] != trader:
                return False
            if verb == "2b":
                ev = self._last_evidence(iid)
                store.update_invoice(iid, supplier_gstin=ev.get("twob_gstin"), state=store.MATCHED,
                                     diagnosis="BILL_GSTIN_MISPRINT", provenance="trader_confirmed",
                                     state_reason="Bill misprinted GSTIN; 2B entry is under the correct GSTIN.")
                self.say(trader, trader, f"✅ ठीक है — बिल पर GSTIN गलत छपा था, आपका credit 2B में सुरक्षित है। "
                         f"खाते में GSTIN {ev.get('twob_gstin')} कर दें। {M.tag(lang, 'you')}" if lang == "hi" else
                         f"✅ OK — the bill misprinted the GSTIN; your credit is safe in 2B. Update your books to "
                         f"{ev.get('twob_gstin')}. {M.tag(lang, 'you')}", turn)
            elif verb == "bill":
                store.update_invoice(iid, state=store.TO_CHASE, diagnosis="GSTIN_AMEND", provenance="trader_confirmed",
                                     state_reason="Supplier reported under a different GSTIN; ask to amend.")
                store.upsert_supplier(trader, inv["supplier_gstin"], inv.get("supplier_name"), consent=1)
                self.plan_followups(trader, turn)
            elif verb == "confirm":
                store.upsert_supplier(trader, inv["supplier_gstin"], inv.get("supplier_name"), consent=1)
                store.update_invoice(iid, state=store.PENDING_2B, pending_field=None)
                self.reconcile_and_plan(trader, turn)
            elif verb == "wrong":
                store.update_invoice(iid, state=store.NEEDS_INPUT, pending_field="supplier_gstin")
                self.ask_next_field(trader, store.get_invoice(iid), None, None, turn)
            elif verb == "eligible":
                store.update_invoice(iid, eligibility="ELIGIBLE", state=store.PENDING_2B,
                                     state_reason="trader says stock for resale")
                self.say(trader, trader, "✅ ठीक है, इसे claim योग्य माल माना। " + M.tag(lang, "you") if lang == "hi"
                         else "✅ OK, treated as eligible stock. " + M.tag(lang, "you"), turn)
                if store.get_2b_imports(trader):
                    self.reconcile_and_plan(trader, turn)
            store.trace(trader, turn, "CONSENT", f"trader_answer:{verb}", {"invoice_id": iid})
            return True
        m = re.match(r"^(number|no\.?|नंबर)\s+(\S+)\s+(\+?[\d\s\-]{10,16})$", low)
        if m:
            return self.tool_set_supplier_phone(trader, m.group(2), m.group(3), turn, reply=True) is not None
        if low in {"status", "report", "hisab", "hisaab", "हिसाब", "स्टेटस", "kitna", "summary"}:
            self.say(trader, trader, M.status_report(store.get_invoices(trader), lang), turn)
            return True
        if low in {"help", "madad", "मदद", "hi", "hello", "namaste", "नमस्ते", "start"}:
            self.say(trader, trader, M.help_text(lang), turn)
            return True
        if low in {"bhejo", "send", "भेजो"}:
            if not self.plan_followups(trader, turn):
                self.say(trader, trader, "अभी भेजने को कुछ नहीं है।" if lang == "hi" else "Nothing to send right now.", turn)
            return True
        return False

    # ------------------------------------------------------------------ tools used by the planner
    def _match_supplier(self, trader: str, ref: str) -> dict | None:
        ref_l = ref.lower().strip()
        sups = store.get_suppliers(trader)
        for s in sups:
            if s["gstin"].lower() == ref_l or s["gstin"].lower().endswith(ref_l):
                return s
        hits = [s for s in sups if s.get("name") and ref_l in s["name"].lower()]
        if len(hits) == 1:
            return hits[0]
        first = ref_l.split()[0] if ref_l.split() else ref_l
        hits = [s for s in sups if s.get("name") and first in s["name"].lower()]
        return hits[0] if len(hits) == 1 else None

    def tool_set_supplier_phone(self, trader: str, ref: str, phone: str, turn: str, reply: bool = False):
        sup = self._match_supplier(trader, ref)
        lang = self.lang(trader)
        if not sup:
            if reply:
                self.say(trader, trader, f"❓ '{ref}' नाम/GSTIN का supplier नहीं मिला।" if lang == "hi"
                         else f"❓ No supplier matching '{ref}'.", turn)
            return {"ok": False, "error": f"no unique supplier matches '{ref}'"}
        ph = normalize_phone(phone)
        if len(ph) < 12:
            return {"ok": False, "error": "phone number looks invalid"}
        store.upsert_supplier(trader, sup["gstin"], phone=ph, consent=1)
        store.trace(trader, turn, "TOOL_CALL", "set_supplier_phone", {"gstin": sup["gstin"], "phone_tail": ph[-4:]})
        if reply:
            self.say(trader, trader, f"📇 {sup.get('name')} का नंबर सेव किया (…{ph[-4:]}) {M.tag(lang, 'you')}"
                     if lang == "hi" else f"📇 Saved {sup.get('name')}'s number (…{ph[-4:]}) {M.tag(lang, 'you')}", turn)
            self.plan_followups(trader, turn)
        return {"ok": True, "supplier": sup.get("name"), "gstin": sup["gstin"]}

    def converse(self, trader: str, text: str, turn: str) -> None:
        from .planner import run_planner, fallback_reply
        lang = self.lang(trader)
        if self.llm.enabled:
            try:
                reply = run_planner(self, trader, text, turn)
                if reply:
                    self.say(trader, trader, reply, turn)
                    return
            except LLMUnavailable as exc:
                store.trace(trader, turn, "ERROR", "planner", {"error": str(exc)[:200], "fallback": True})
        self.say(trader, trader, fallback_reply(self, trader, text, turn, lang), turn)

    # ------------------------------------------------------------------ suppliers
    def handle_supplier(self, phone: str, text: str, turn: str) -> None:
        from .planner import interpret_supplier_reply
        sups = store.find_supplier_by_phone(phone)
        relayed: dict[str, list] = {}
        for sup in sups:
            trader = sup["trader_phone"]
            open_invs = [i for i in store.get_invoices(trader, states=[store.CHASED, store.PROMISED,
                                                                        store.CHANNEL_BLOCKED])
                         if i["supplier_gstin"] == sup["gstin"]]
            if not open_invs:
                continue
            intent, promised = interpret_supplier_reply(self.llm, text, store.now().date().isoformat())
            store.trace(trader, turn, "TOOL_CALL", "interpret_supplier_reply",
                        {"gstin": sup["gstin"], "intent": intent, "promised": promised})
            for inv in open_invs:
                if intent == "PROMISED":
                    nxt = (promised or (store.now() + timedelta(days=7)).date().isoformat()) + " 23:59:00"
                    store.update_invoice(inv["id"], state=store.PROMISED, next_check_at=nxt,
                                         state_reason=f"supplier promised: {text[:120]}")
                elif intent == "ALREADY_FILED":
                    store.update_invoice(inv["id"], state=store.PROMISED, state_reason=f"supplier says filed: {text[:120]}",
                                         next_check_at=(store.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"))
                elif intent in ("DISPUTE", "WRONG_PERSON"):
                    store.update_invoice(inv["id"], state=store.CHANNEL_BLOCKED, state_reason=f"{intent}: {text[:120]}")
            relayed.setdefault(trader, []).append((sup.get("name") or sup["gstin"], intent, promised))
        for trader, items in relayed.items():
            names = ", ".join(n for n, _, _ in items)
            _, intent, promised = items[0]
            self.say(trader, trader, M.supplier_reply_relay(names, text, intent, promised, self.lang(trader)), turn)
        if relayed:
            self.say(next(iter(relayed)), phone, "🙏 धन्यवाद, आपका जवाब पहुँचा दिया गया है। / Thank you, your reply has "
                     "been passed on.", turn, role="supplier")

    # ------------------------------------------------------------------ scheduler
    def tick(self, trader: str | None = None) -> None:
        """Deadline-aware follow-up ladder. Runs every minute in the server."""
        turn = "tick-" + uuid.uuid4().hex[:6]
        traders = [trader] if trader else [r["phone"] for r in _all_traders()]
        now = store.now_text()
        for tr in traders:
            lang = self.lang(tr)
            # 1. retry bill reads that failed because the model/service was down
            for p in _pendings(tr, "RETRY_EXTRACT:"):
                path = Path(p["payload"]["path"])
                if path.exists() and p["payload"].get("attempts", 1) < 5:
                    store.resolve_pending(p["id"], "RETRIED")
                    self.handle_bill(tr, path.read_bytes(), p["payload"]["mime"], turn,
                                     retry_of=p["payload"].get("attempts", 1))
            # 2. chase ladder
            due = [i for i in store.get_invoices(tr, states=[store.CHASED, store.PROMISED])
                   if i.get("next_check_at") and i["next_check_at"] <= now]
            escalate, renudge = [], []
            for inv in due:
                (escalate if (inv["chase_count"] or 0) >= self.s.max_chases else renudge).append(inv)
            for inv in renudge:
                store.update_invoice(inv["id"], state=store.TO_CHASE)
                store.trace(tr, turn, "DECISION", "nudge_due", {"invoice_id": inv["id"],
                                                                 "chase_count": inv["chase_count"]})
            if escalate:
                groups = {}
                for inv in escalate:
                    store.update_invoice(inv["id"], state=store.ESCALATED,
                                         state_reason=f"no report after {inv['chase_count']} reminders")
                    g = groups.setdefault(inv["supplier_gstin"], {"name": inv.get("supplier_name") or inv["supplier_gstin"],
                                                                 "invoices": [], "gst_paise": 0,
                                                                 "deadline": diagnose.itc_deadline(inv["tax_period"])})
                    g["invoices"].append(inv)
                    g["gst_paise"] += inv["total_gst_paise"] or 0
                store.trace(tr, turn, "DECISION", "escalate_to_trader", {"invoice_ids": [i["id"] for i in escalate]})
                self.say(tr, tr, M.escalation(list(groups.values()), lang), turn)
                for gstin, g in groups.items():
                    sup = store.get_supplier(tr, gstin) or {}
                    self.say(tr, tr, M.call_script(g["name"], sup.get("phone"), g["invoices"], lang), turn)
            if renudge:
                self.say(tr, tr, "⏰ कुछ supplier ने अभी तक जवाब/फ़ाइल नहीं किया। दोबारा याद दिलाने का मैसेज तैयार है:" if lang == "hi"
                         else "⏰ Some suppliers haven't reported yet. Reminder drafts are ready:", turn)
                self.plan_followups(tr, turn)
            # 3. QRMP re-checks
            q = [i for i in store.get_invoices(tr, states=[store.QRMP_WAIT])
                 if i.get("next_check_at") and i["next_check_at"] <= now]
            if q:
                for inv in q:
                    store.update_invoice(inv["id"], next_check_at=(store.now() + timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S"))
                self.say(tr, tr, f"📅 तिमाही वाले supplier के {len(q)} बिल अब 2B में आ जाने चाहिए। कृपया नया GSTR-2B forward करें।"
                         if lang == "hi" else f"📅 {len(q)} quarterly-filer bill(s) should now be in your 2B. Please forward the new GSTR-2B.",
                         turn)

    def demo_command(self, trader: str, low: str, turn: str) -> None:
        m = re.match(r"^#advance\s+(\d+)", low)
        if m:
            days = int(m.group(1))
            total = store.advance_clock(days)
            store.trace(trader, turn, "DEMO", "advance_clock", {"days": days, "offset_total": total})
            self.say(trader, trader, f"⏩ [DEMO] घड़ी {days} दिन आगे (कुल {total:.0f})। / clock +{days} days", turn)
            self.tick(trader)
            return
        if low.startswith("#tick"):
            self.tick(trader)
            return
        if low.startswith("#status"):
            self.say(trader, trader, M.status_report(store.get_invoices(trader), self.lang(trader)), turn)


def _all_traders() -> list[dict]:
    with store.conn() as c:
        return [dict(r) for r in c.execute("SELECT phone FROM agent_traders")]


def _pendings(trader: str, prefix: str) -> list[dict]:
    with store.conn() as c:
        rows = c.execute("SELECT * FROM agent_pending WHERE trader_phone=? AND status='PENDING' AND kind LIKE ?",
                         (trader, prefix + "%")).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out
