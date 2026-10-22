"""Tests for the Meta WhatsApp Cloud API channel and webhook. Offline: fake
transports only, no network, no real tokens."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TMP = Path(tempfile.mkdtemp(prefix="milaan-meta-test-"))
os.environ["LLM_API_KEY"] = ""
os.environ["GEMINI_API_KEY"] = ""
os.environ["TWILIO_ACCOUNT_SID"] = ""
os.environ["META_ACCESS_TOKEN"] = ""

from agent import server, store  # noqa: E402
from agent.channel import (MetaChannel, TwilioChannel, parse_meta_webhook,  # noqa: E402
                           valid_meta_signature)
from agent.config import get_settings  # noqa: E402
from agent.core import Agent  # noqa: E402
from agent.demo.fixtures import FixtureExtractor  # noqa: E402
from agent.demo.seed import DEMO_DIR, main as seed_main  # noqa: E402

TRADER = "+919000000001"
PHONE_ID = "1234567890"
SECRET = "test-app-secret"
_old_env: dict[str, str | None] = {}


def setUpModule():
    for key, value in (("AGENT_DB_PATH", str(TMP / "agent.db")), ("AGENT_MEDIA_DIR", str(TMP / "media"))):
        _old_env[key] = os.environ.get(key)
        os.environ[key] = value
    store.reset_initialized()


def tearDownModule():
    for key, value in _old_env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    store.reset_initialized()
    shutil.rmtree(TMP, ignore_errors=True)


def meta_settings():
    s = get_settings()
    s.whatsapp_provider = "meta"
    s.meta_access_token = "test-token"
    s.meta_phone_number_id = PHONE_ID
    s.meta_api_version = "v1.0"
    s.meta_app_secret = SECRET
    s.meta_verify_token = "verify-me"
    s.trader_phones = (TRADER,)
    return s


class FakeResp:
    def __init__(self, status=200, data=None, content=b""):
        self.status_code, self._data, self.content = status, data, content

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakePost:
    def __init__(self, *responses):
        self.calls, self.responses = [], list(responses)

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        if self.responses:
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return FakeResp(200, {"messages": [{"id": f"wamid.{len(self.calls)}"}]})


def message_payload(*messages, statuses=(), phone_id=PHONE_ID):
    return {"object": "whatsapp_business_account", "entry": [{"id": "WABA", "changes": [{
        "field": "messages",
        "value": {"messaging_product": "whatsapp",
                  "metadata": {"display_phone_number": "15550000000", "phone_number_id": phone_id},
                  "messages": list(messages), "statuses": list(statuses)}}]}]}


def text_msg(body, mid="wamid.in1", sender="919000000001"):
    return {"from": sender, "id": mid, "timestamp": "1", "type": "text", "text": {"body": body}}


def sign(raw: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


class MetaChannelTests(unittest.TestCase):
    def test_send_text(self):
        post = FakePost()
        r = MetaChannel(meta_settings(), transport=post).send(TRADER, "namaste")
        self.assertTrue(r.ok)
        self.assertEqual(r.provider_id, "wamid.1")
        call = post.calls[0]
        self.assertEqual(call["url"], f"https://graph.facebook.com/v1.0/{PHONE_ID}/messages")
        self.assertEqual(call["headers"], {"Authorization": "Bearer test-token"})
        self.assertEqual(call["json"]["to"], "919000000001")
        self.assertEqual(call["json"]["type"], "text")
        self.assertEqual(call["json"]["text"]["body"], "namaste")

    def test_image_with_caption_or_followup_text(self):
        post = FakePost()
        MetaChannel(meta_settings(), transport=post).send(TRADER, "GSTIN?", "https://x.example/media/crop-1.jpg")
        self.assertEqual(len(post.calls), 1)
        self.assertEqual(post.calls[0]["json"]["image"],
                         {"link": "https://x.example/media/crop-1.jpg", "caption": "GSTIN?"})
        post = FakePost()
        MetaChannel(meta_settings(), transport=post).send(TRADER, "y" * 2000, "https://x.example/media/crop-1.jpg")
        self.assertEqual([c["json"]["type"] for c in post.calls], ["image", "text"])
        self.assertNotIn("caption", post.calls[0]["json"]["image"])

    def test_long_text_is_split(self):
        post = FakePost()
        MetaChannel(meta_settings(), transport=post).send(TRADER, "x\n" * 5000)
        self.assertGreater(len(post.calls), 1)
        self.assertTrue(all(len(c["json"]["text"]["body"]) <= 4096 for c in post.calls))

    def test_errors(self):
        import requests
        window = FakeResp(400, {"error": {"code": 131047, "message": "Re-engagement message"}})
        r = MetaChannel(meta_settings(), transport=FakePost(window)).send(TRADER, "hi")
        self.assertFalse(r.ok)
        self.assertFalse(r.retryable)
        self.assertIn("131047", r.error)
        self.assertTrue(MetaChannel(meta_settings(), transport=FakePost(FakeResp(503))).send(TRADER, "hi").retryable)
        down = FakePost(requests.ConnectionError("down"))
        self.assertTrue(MetaChannel(meta_settings(), transport=down).send(TRADER, "hi").retryable)

    def test_not_configured_sends_nothing(self):
        s = meta_settings()
        s.meta_access_token = ""
        post = FakePost()
        self.assertFalse(MetaChannel(s, transport=post).send(TRADER, "hi").ok)
        self.assertEqual(post.calls, [])

    def test_download_looks_up_then_fetches_with_token(self):
        calls = []

        def getter(url, headers=None, timeout=None):
            calls.append((url, headers))
            if len(calls) == 1:
                return FakeResp(200, {"url": "https://lookaside.example/abc", "mime_type": "image/jpeg"})
            return FakeResp(200, content=b"jpeg-bytes")

        data = MetaChannel(meta_settings(), getter=getter).download("777")
        self.assertEqual(data, b"jpeg-bytes")
        self.assertEqual(calls[0][0], "https://graph.facebook.com/v1.0/777")
        self.assertEqual(calls[1][0], "https://lookaside.example/abc")
        self.assertTrue(all(h == {"Authorization": "Bearer test-token"} for _, h in calls))
        with self.assertRaises(ValueError):
            MetaChannel(meta_settings(), getter=getter).download("../me")


class MetaWebhookParsingTests(unittest.TestCase):
    def test_signature(self):
        raw = b'{"entry": []}'
        self.assertTrue(valid_meta_signature(SECRET, raw, sign(raw)))
        self.assertFalse(valid_meta_signature(SECRET, raw + b" ", sign(raw)))
        self.assertFalse(valid_meta_signature(SECRET, raw, sign(raw, "other")))
        self.assertFalse(valid_meta_signature(SECRET, raw, sign(raw)[len("sha256="):]))
        self.assertFalse(valid_meta_signature("", raw, sign(raw, "")))

    def test_message_kinds(self):
        payload = message_payload(
            text_msg("namaste", "m1"),
            {"from": "919000000001", "id": "m2", "type": "image",
             "image": {"id": "IMG1", "mime_type": "image/jpeg", "caption": "bill"}},
            {"from": "919000000001", "id": "m3", "type": "document",
             "document": {"id": "DOC1", "mime_type": "application/json", "filename": "GSTR2B_OCT2026.json"}},
            {"from": "919000000001", "id": "m4", "type": "audio",
             "audio": {"id": "AUD1", "mime_type": "audio/ogg; codecs=opus", "voice": True}},
            {"from": "919000000001", "id": "m5", "type": "interactive",
             "interactive": {"type": "button_reply", "button_reply": {"id": "b", "title": "haan"}}},
            {"from": "919000000001", "id": "m6", "type": "sticker", "sticker": {"id": "S", "mime_type": "image/webp"}},
            statuses=[{"id": "out1", "status": "failed", "recipient_id": "919000000002",
                       "errors": [{"code": 131047, "title": "Re-engagement message"}]},
                      {"id": "out2", "status": "delivered", "recipient_id": "919000000001"}])
        inbound, failures = parse_meta_webhook(payload, PHONE_ID)
        self.assertEqual([m.message_id for m in inbound], ["m1", "m2", "m3", "m4", "m5"])
        self.assertTrue(all(m.sender == TRADER for m in inbound))
        self.assertEqual(inbound[0].body, "namaste")
        self.assertEqual((inbound[1].media, inbound[1].body), ([("IMG1", "image/jpeg")], "bill"))
        self.assertEqual((inbound[2].media, inbound[2].file_names),
                         ([("DOC1", "application/json")], ["GSTR2B_OCT2026.json"]))
        self.assertEqual(inbound[3].media, [("AUD1", "audio/ogg; codecs=opus")])
        self.assertEqual(inbound[4].body, "haan")
        self.assertEqual(failures, [{"to_tail": "0002", "code": 131047, "title": "Re-engagement message"}])

    def test_other_phone_number_is_ignored(self):
        inbound, _ = parse_meta_webhook(message_payload(text_msg("hi"), phone_id="999"), PHONE_ID)
        self.assertEqual(inbound, [])


class FakePool:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append(args)


class MetaServerTests(unittest.TestCase):
    def setUp(self):
        self.pool = FakePool()
        meta = {"whatsapp_provider": "meta", "meta_app_secret": SECRET, "meta_verify_token": "verify-me",
                "meta_phone_number_id": PHONE_ID}
        for p in (mock.patch.object(server, "pool", self.pool), mock.patch.multiple(server.settings, **meta)):
            p.start()
            self.addCleanup(p.stop)
        server._seen_meta_ids.clear()
        self.client = server.app.test_client()

    def post(self, payload, signature=None):
        raw = json.dumps(payload).encode()
        return self.client.post("/whatsapp", data=raw, content_type="application/json",
                                headers={"X-Hub-Signature-256": signature or sign(raw)})

    def test_handshake(self):
        ok = self.client.get("/whatsapp?hub.mode=subscribe&hub.verify_token=verify-me&hub.challenge=4242")
        self.assertEqual((ok.status_code, ok.get_data(as_text=True)), (200, "4242"))
        self.assertEqual(self.client.get("/whatsapp?hub.mode=subscribe&hub.verify_token=nope&hub.challenge=1")
                         .status_code, 403)
        with mock.patch.object(server.settings, "whatsapp_provider", "twilio"):
            self.assertEqual(self.client.get("/whatsapp?hub.mode=subscribe&hub.verify_token=verify-me"
                                             "&hub.challenge=1").status_code, 403)

    def test_bad_signature_is_rejected_and_traced(self):
        r = self.post(message_payload(text_msg("haan")), signature="sha256=" + "0" * 64)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.pool.calls, [])
        with store.conn() as c:
            row = c.execute("SELECT name, payload FROM agent_trace ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row[0], "rejected_bad_signature")
        self.assertIn("meta", row[1])

    def test_signed_message_is_handed_to_the_agent_once(self):
        payload = message_payload(text_msg("namaste", "dup1"))
        self.assertEqual(self.post(payload).status_code, 200)
        self.assertEqual(self.post(payload).status_code, 200)  # Meta re-delivery
        self.assertEqual(self.pool.calls, [(TRADER, "namaste", [], [])])

    def test_image_is_handed_over_as_media_id(self):
        img = {"from": "919000000001", "id": "m9", "type": "image", "image": {"id": "IMG9", "mime_type": "image/jpeg"}}
        self.assertEqual(self.post(message_payload(img)).status_code, 200)
        self.assertEqual(self.pool.calls, [(TRADER, "", [("IMG9", "image/jpeg")], [])])

    def test_twilio_path_still_requires_its_signature(self):
        with mock.patch.multiple(server.settings, whatsapp_provider="twilio", twilio_validate_signature=True):
            r = self.client.post("/whatsapp", data={"From": "whatsapp:+919000000001", "Body": "haan"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.pool.calls, [])


class MetaAgentTests(unittest.TestCase):
    def fresh(self, getter=None):
        for suffix in ("", "-wal", "-shm"):
            p = Path(os.environ["AGENT_DB_PATH"] + suffix)
            if p.exists():
                p.unlink()
        store.reset_initialized()
        seed_main(["--trader", TRADER, "--supplier", "+919000000002", "--quiet"])
        post = FakePost()
        s = meta_settings()
        ch = MetaChannel(s, transport=post, getter=getter)
        return Agent(channel=ch, settings=s, fixture_extractor=FixtureExtractor(DEMO_DIR)), post

    def test_default_channel_follows_provider(self):
        s = meta_settings()
        self.assertIsInstance(Agent(settings=s).channel, MetaChannel)
        s.whatsapp_provider = "twilio"
        self.assertIsInstance(Agent(settings=s).channel, TwilioChannel)

    def test_namaste_reply_goes_out_through_meta(self):
        agent, post = self.fresh()
        agent.handle(TRADER, "namaste")
        self.assertTrue(post.calls)
        self.assertTrue(all(c["json"]["to"] == "919000000001" for c in post.calls))
        self.assertIn("MILAAN", post.calls[0]["json"]["text"]["body"])

    def test_bill_photo_is_downloaded_by_media_id(self):
        jpg = (DEMO_DIR / "bills" / "L1_marudhar_1214.jpg").read_bytes()
        seen = []

        def getter(url, headers=None, timeout=None):
            seen.append(url)
            if url.endswith("/IMGL1"):
                return FakeResp(200, {"url": "https://lookaside.example/l1"})
            return FakeResp(200, content=jpg)

        agent, post = self.fresh(getter)
        before = len(store.get_invoices(TRADER))
        agent.handle(TRADER, "", [("IMGL1", "image/jpeg")])
        self.assertEqual(seen, ["https://graph.facebook.com/v1.0/IMGL1", "https://lookaside.example/l1"])
        self.assertEqual(len(store.get_invoices(TRADER)), before + 1)
        self.assertTrue(post.calls)


if __name__ == "__main__":
    unittest.main()
