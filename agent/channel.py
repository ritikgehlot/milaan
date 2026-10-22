"""WhatsApp channels: Twilio (sandbox or production sender) or Meta's WhatsApp
Cloud API, plus a console channel used by the offline simulator and tests.
Same interface for all three: send(to, body, media_url) and download(ref).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import threading
import time
from dataclasses import dataclass, field

import requests

from .config import Settings, get_settings

WHATSAPP_MAX = 1550  # Twilio WhatsApp body limit is 1600 chars
META_TEXT_MAX = 4000  # Cloud API text body limit is 4096 chars
META_CAPTION_MAX = 1024
GRAPH = "https://graph.facebook.com"


@dataclass
class SendResult:
    ok: bool
    provider_id: str | None = None
    error: str | None = None
    retryable: bool = False


def split_body(body: str, limit: int = WHATSAPP_MAX) -> list[str]:
    if len(body) <= limit:
        return [body]
    parts, current = [], ""
    for line in body.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            parts.append(current)
            current = ""
        while len(line) > limit:
            parts.append(line[:limit])
            line = line[limit:]
        current = f"{current}\n{line}" if current else line
    if current:
        parts.append(current)
    return parts


_send_lock = threading.Lock()
_last_send = [0.0]


class TwilioChannel:
    name = "twilio"

    def __init__(self, settings: Settings | None = None, transport=None, getter=None,
                 min_interval_s: float | None = None, account: tuple[str, str, str] | None = None):
        self.s = settings or get_settings()
        if account:  # (sid, token, from) of a second account, e.g. the supplier-side trial
            import copy
            self.s = copy.copy(self.s)
            self.s.twilio_account_sid, self.s.twilio_auth_token, self.s.twilio_whatsapp_from = account
        self._post = transport or requests.post
        self._get = getter or requests.get
        # The Twilio sandbox sends at most one message every 3 seconds.
        self.min_interval_s = float(os.environ.get("TWILIO_MIN_INTERVAL_S", "3.1")) \
            if min_interval_s is None else min_interval_s

    def _throttle(self) -> None:
        wait = self.min_interval_s - (time.monotonic() - _last_send[0])
        if wait > 0:
            time.sleep(wait)
        _last_send[0] = time.monotonic()

    def send(self, to_phone: str, body: str, media_url: str | None = None) -> SendResult:
        if not self.s.twilio_enabled:
            return SendResult(False, error="twilio not configured", retryable=False)
        with _send_lock:
            return self._send(to_phone, body, media_url)

    def _send(self, to_phone: str, body: str, media_url: str | None = None) -> SendResult:
        url = f"https://api.twilio.com/2010-04-01/Accounts/{self.s.twilio_account_sid}/Messages.json"
        last = None
        for i, chunk in enumerate(split_body(body)):
            data = {"From": self.s.twilio_whatsapp_from, "To": f"whatsapp:{to_phone}", "Body": chunk}
            if media_url and i == 0:
                data["MediaUrl"] = media_url
            self._throttle()
            try:
                resp = self._post(url, data=data, auth=(self.s.twilio_account_sid, self.s.twilio_auth_token),
                                  timeout=20)
            except requests.RequestException as exc:
                return SendResult(False, error=f"network: {type(exc).__name__}", retryable=True)
            if resp.status_code in (429, 500, 502, 503, 504):
                return SendResult(False, error=f"http {resp.status_code}", retryable=True)
            if resp.status_code >= 400:
                try:
                    err = resp.json()
                    msg = f"{err.get('code')}: {err.get('message')}"
                except ValueError:
                    msg = f"http {resp.status_code}"
                # 63016/63015: outside session window or recipient not joined to sandbox
                return SendResult(False, error=msg[:280], retryable=False)
            last = resp.json().get("sid")
        return SendResult(True, provider_id=last)

    def download(self, media_url: str) -> bytes:
        resp = self._get(media_url, auth=(self.s.twilio_account_sid, self.s.twilio_auth_token),
                         timeout=30, allow_redirects=True)
        resp.raise_for_status()
        return resp.content


def _digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


class MetaChannel:
    """WhatsApp Cloud API (Meta). Works with Meta's free test number, which can
    message up to five verified recipient numbers."""
    name = "meta"

    def __init__(self, settings: Settings | None = None, transport=None, getter=None):
        self.s = settings or get_settings()
        self._post = transport or requests.post
        self._get = getter or requests.get

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.s.meta_access_token}"}

    def send(self, to_phone: str, body: str, media_url: str | None = None) -> SendResult:
        if not self.s.meta_enabled:
            return SendResult(False, error="meta whatsapp not configured", retryable=False)
        with _send_lock:
            return self._send(to_phone, body, media_url)

    def _send(self, to_phone: str, body: str, media_url: str | None = None) -> SendResult:
        url = f"{GRAPH}/{self.s.meta_api_version}/{self.s.meta_phone_number_id}/messages"
        parts: list[dict] = []
        if media_url:  # Meta fetches the image from our public /media URL
            image = {"link": media_url}
            if body and len(body) <= META_CAPTION_MAX:
                image["caption"], body = body, ""
            parts.append({"type": "image", "image": image})
        if body:
            parts += [{"type": "text", "text": {"body": chunk, "preview_url": False}}
                      for chunk in split_body(body, META_TEXT_MAX)]
        last = None
        for part in parts:
            payload = {"messaging_product": "whatsapp", "recipient_type": "individual",
                       "to": _digits(to_phone), **part}
            try:
                resp = self._post(url, json=payload, headers=self._headers(), timeout=20)
            except requests.RequestException as exc:
                return SendResult(False, error=f"network: {type(exc).__name__}", retryable=True)
            if resp.status_code in (429, 500, 502, 503, 504):
                return SendResult(False, error=f"http {resp.status_code}", retryable=True)
            if resp.status_code >= 400:
                try:
                    err = resp.json().get("error") or {}
                    msg = f"{err.get('code')}: {err.get('message')}"
                except ValueError:
                    msg = f"http {resp.status_code}"
                # 131047: outside the 24-hour window, 131030: not an allowed test recipient,
                # 190: access token expired
                return SendResult(False, error=msg[:280], retryable=False)
            try:
                last = (resp.json().get("messages") or [{}])[0].get("id")
            except ValueError:
                last = None
        return SendResult(True, provider_id=last)

    def download(self, media_id: str) -> bytes:
        """Inbound media arrives as an id: look up its short-lived URL, then fetch it.
        Both requests need the access token."""
        if not re.fullmatch(r"[A-Za-z0-9_-]+", media_id or ""):
            raise ValueError("bad media id")
        info = self._get(f"{GRAPH}/{self.s.meta_api_version}/{media_id}", headers=self._headers(), timeout=20)
        info.raise_for_status()
        url = info.json().get("url") or ""
        if not url.startswith("https://"):
            raise ValueError("media url missing")
        resp = self._get(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.content


class ConsoleChannel:
    """Prints/records messages instead of sending. Used by simulate.py and tests."""
    name = "console"

    def __init__(self, fail_for: set[str] | None = None, echo: bool = True):
        self.sent: list[dict] = []
        self.fail_for = fail_for or set()
        self.echo = echo
        self.media: dict[str, bytes] = {}

    def send(self, to_phone: str, body: str, media_url: str | None = None) -> SendResult:
        if to_phone in self.fail_for:
            self.sent.append({"to": to_phone, "body": body, "media_url": media_url, "ok": False})
            return SendResult(False, error="simulated: number not on WhatsApp", retryable=True)
        self.sent.append({"to": to_phone, "body": body, "media_url": media_url, "ok": True})
        if self.echo:
            print(f"\n──── WhatsApp → {to_phone} ────\n{body}" + (f"\n[image: {media_url}]" if media_url else ""))
        return SendResult(True, provider_id=f"console-{len(self.sent)}")

    def download(self, media_url: str) -> bytes:
        if media_url in self.media:
            return self.media[media_url]
        with open(media_url, "rb") as fh:
            return fh.read()


def send_with_retry(channel, to_phone: str, body: str, media_url: str | None = None,
                    attempts: int = 3, base_delay: float = 1.0) -> tuple[SendResult, int]:
    result = SendResult(False, error="not attempted")
    for attempt in range(1, attempts + 1):
        result = channel.send(to_phone, body, media_url)
        if result.ok or not result.retryable:
            return result, attempt
        time.sleep(base_delay * (2 ** (attempt - 1)))
    return result, attempts


def valid_twilio_signature(auth_token: str, url: str, params: dict, signature: str) -> bool:
    """Twilio request validation: HMAC-SHA1 over URL + sorted POST params."""
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode("utf-8"), hashlib.sha1).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature or "")


def valid_meta_signature(app_secret: str, raw_body: bytes, header: str) -> bool:
    """Meta webhook validation: X-Hub-Signature-256 = 'sha256=' + HMAC-SHA256(app secret, raw body)."""
    if not app_secret or not (header or "").startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[len("sha256="):].strip().lower())


@dataclass
class InboundMessage:
    message_id: str
    sender: str  # +E.164
    body: str = ""
    media: list[tuple[str, str]] = field(default_factory=list)  # (Meta media id, mime type)
    file_names: list[str] = field(default_factory=list)


META_MEDIA_TYPES = ("image", "document", "audio")  # stickers and videos are not bills or 2B files


def parse_meta_webhook(payload: dict, phone_number_id: str = "") -> tuple[list[InboundMessage], list[dict]]:
    """Meta webhook JSON -> (inbound messages, failed delivery statuses).
    Events addressed to a different phone number id are ignored."""
    inbound, failures = [], []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                continue
            value = change.get("value") or {}
            if phone_number_id and str((value.get("metadata") or {}).get("phone_number_id")) != phone_number_id:
                continue
            for m in value.get("messages") or []:
                sender, kind = _digits(m.get("from")), m.get("type")
                if not sender:
                    continue
                msg = InboundMessage(message_id=str(m.get("id") or ""), sender="+" + sender)
                if kind == "text":
                    msg.body = (m.get("text") or {}).get("body", "")
                elif kind in META_MEDIA_TYPES:
                    part = m.get(kind) or {}
                    if not part.get("id"):
                        continue
                    msg.media.append((str(part["id"]), part.get("mime_type", "")))
                    if part.get("filename"):
                        msg.file_names.append(part["filename"])
                    msg.body = part.get("caption", "") or ""
                elif kind == "interactive":
                    reply = m.get("interactive") or {}
                    msg.body = (reply.get("button_reply") or reply.get("list_reply") or {}).get("title", "")
                elif kind == "button":
                    msg.body = (m.get("button") or {}).get("text", "")
                else:  # reactions, locations, contacts, stickers, unsupported: nothing to act on
                    continue
                inbound.append(msg)
            for st in value.get("statuses") or []:
                if st.get("status") == "failed":
                    err = (st.get("errors") or [{}])[0]
                    failures.append({"to_tail": _digits(st.get("recipient_id"))[-4:], "code": err.get("code"),
                                     "title": str(err.get("title") or err.get("message") or "")[:200]})
    return inbound, failures
