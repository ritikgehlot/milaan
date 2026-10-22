"""WhatsApp webhook server (Twilio or Meta WhatsApp Cloud API).

Run:   python -m agent.server            (listens on :5050)
Then:  start a tunnel to :5050 -> set PUBLIC_URL, and point the provider at
       {PUBLIC_URL}/whatsapp:
         Twilio: sandbox "When a message comes in" URL (HTTP POST)
         Meta:   app dashboard Callback URL + META_VERIFY_TOKEN, field 'messages'
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, Response, abort, request, send_from_directory

from .channel import parse_meta_webhook, valid_meta_signature, valid_twilio_signature
from .config import get_settings
from .core import Agent
from . import store
from .web import bp as dashboard_bp

log = logging.getLogger("milaan")
app = Flask(__name__)
app.register_blueprint(dashboard_bp)
settings = get_settings()
agent = Agent(settings=settings)
pool = ThreadPoolExecutor(max_workers=4)
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
_seen_meta_ids: deque = deque(maxlen=500)  # Meta re-delivers a webhook it thinks failed
_seen_lock = threading.Lock()


@app.get("/health")
def health():
    return {"ok": True, "llm": settings.llm_enabled, "provider": settings.whatsapp_provider,
            "twilio": settings.twilio_enabled, "meta": settings.meta_enabled,
            "public_url": settings.public_url or None, "traders": len(settings.trader_phones)}


@app.get("/whatsapp")
def whatsapp_verify():
    """Meta's webhook subscription handshake: echo hub.challenge if the verify token matches."""
    if (settings.whatsapp_provider == "meta" and settings.meta_verify_token
            and request.args.get("hub.mode") == "subscribe"
            and hmac.compare_digest(request.args.get("hub.verify_token", ""), settings.meta_verify_token)):
        return Response(request.args.get("hub.challenge", ""), status=200, mimetype="text/plain")
    abort(403)


@app.post("/whatsapp")
def whatsapp():
    if settings.whatsapp_provider == "meta":
        return _meta_webhook()
    form = request.form.to_dict()
    if settings.twilio_validate_signature:
        url = (settings.public_url + "/whatsapp") if settings.public_url else request.url
        sig = request.headers.get("X-Twilio-Signature", "")
        tokens = [t for t in (settings.twilio_auth_token, settings.supplier_twilio_auth_token) if t]
        if not any(valid_twilio_signature(t, url, form, sig) for t in tokens):
            store.trace(None, None, "POLICY", "rejected_bad_signature", {"from_tail": form.get("From", "")[-4:]})
            abort(403)
    sender = form.get("From", "")
    body = form.get("Body", "") or ""
    n = int(form.get("NumMedia", "0") or 0)
    media = [(form.get(f"MediaUrl{i}"), form.get(f"MediaContentType{i}", "")) for i in range(n)]
    # Respond to Twilio immediately; do the (slow) agent work in the background.
    pool.submit(_safe_handle, sender, body, media)
    # Empty 200: replies go out through the REST API, so no TwiML is needed
    # (Twilio trial flows do not accept TwiML responses).
    return Response("", status=200, mimetype="text/plain")


def _meta_webhook():
    raw = request.get_data()
    if not valid_meta_signature(settings.meta_app_secret, raw, request.headers.get("X-Hub-Signature-256", "")):
        store.trace(None, None, "POLICY", "rejected_bad_signature", {"provider": "meta"})
        abort(403)
    try:
        payload = json.loads(raw)
    except ValueError:
        abort(400)
    inbound, failures = parse_meta_webhook(payload, settings.meta_phone_number_id)
    for failure in failures:  # e.g. 131047 outside the 24-hour window
        store.trace(None, None, "MESSAGE", "meta_delivery_failed", failure)
    for msg in inbound:
        with _seen_lock:
            if msg.message_id and msg.message_id in _seen_meta_ids:
                continue
            _seen_meta_ids.append(msg.message_id)
        pool.submit(_safe_handle, msg.sender, msg.body, msg.media, msg.file_names)
    # Meta only needs a quick 200; replies go out through the Graph API.
    return Response("", status=200, mimetype="text/plain")


def _safe_handle(sender, body, media, file_names=None):
    try:
        agent.handle(sender, body, media, file_names)
    except Exception:  # pragma: no cover - last-resort logging
        log.exception("agent.handle failed")


@app.get("/media/<path:name>")
def media(name: str):
    if not name.startswith("crop-") or "/" in name or ".." in name:
        abort(404)
    return send_from_directory(settings.media_dir, name)


def _ticker():
    while True:
        time.sleep(60)
        try:
            agent.tick()
        except Exception:
            log.exception("tick failed")


def main():
    logging.basicConfig(level=logging.INFO)
    threading.Thread(target=_ticker, daemon=True).start()
    print("Dashboard: http://localhost:5050/dashboard")
    sender_ok = settings.meta_enabled if settings.whatsapp_provider == "meta" else settings.twilio_enabled
    print(f"MILAAN agent on :5050 | LLM={'on' if settings.llm_enabled else 'OFF'} | "
          f"WhatsApp={settings.whatsapp_provider} {'on' if sender_ok else 'OFF'} | traders={list(settings.trader_phones)}")
    app.run(host="0.0.0.0", port=int(__import__('os').environ.get("PORT", 5050)), threaded=True)


if __name__ == "__main__":
    main()
