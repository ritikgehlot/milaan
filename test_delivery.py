"""Task 7 tests: WhatsApp delivery safety, persistence and configuration.

Run: .venv/bin/python -m unittest test_delivery -v

All sends are simulated with fake transports/providers. No test touches the
network, no real token is used, and no real message is sent.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import db  # noqa: E402
import delivery  # noqa: E402
import gstr2b  # noqa: E402

_CHARSET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_CSV_HEADER = (
    "supplier_gstin,invoice_number,invoice_date,tax_period,"
    "taxable_value,cgst,sgst,igst,total_gst"
)
PERIOD = "2026-03"
TOKEN = "test-token-must-never-leak"


def reference_checksum(first_14: str) -> str:
    total = 0
    for position, character in enumerate(first_14):
        value = _CHARSET.index(character.upper())
        product = value * (1 if position % 2 == 0 else 2)
        total += product // 36 + product % 36
    return _CHARSET[(36 - total % 36) % 36]


def make_gstin(first_14: str) -> str:
    return first_14 + reference_checksum(first_14)


GSTIN_A = make_gstin("27AAACS1234A1Z")
GSTIN_B = make_gstin("29AAACB5678B1Z")
GSTIN_C = make_gstin("24AAACD9012C1Z")


def rupees(paise: int) -> str:
    return f"{paise // 100}.{paise % 100:02d}"


def csv_bytes(rows) -> bytes:
    lines = [_CSV_HEADER]
    for values in rows:
        lines.append(",".join(str(value) for value in values))
    return ("\n".join(lines) + "\n").encode("utf-8")


def gstr2b_row(gstin, invoice_number, total_gst_paise=225_000):
    cgst = total_gst_paise // 2
    return [
        gstin,
        invoice_number,
        "2026-03-05",
        PERIOD,
        "12500.00",
        rupees(cgst),
        rupees(total_gst_paise - cgst),
        "0.00",
        rupees(total_gst_paise),
    ]


def transport_response(status=200, payload=None, headers=None):
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    return delivery.TransportResponse(
        status_code=status,
        headers=headers or {},
        body=body,
    )


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, payload, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "payload": dict(payload),
                "timeout": timeout,
            }
        )
        next_item = self.responses.pop(0)
        if isinstance(next_item, Exception):
            raise next_item
        return next_item


class ScriptedProvider:
    """In-process fake provider; no HTTP and no real message."""

    name = "mock"
    is_ready = True

    def __init__(self, responses, *, max_attempts=3):
        self.responses = list(responses)
        self.max_attempts = max_attempts
        self.calls = []

    def send(self, recipient_phone, message_body, *, idempotency_key=None):
        self.calls.append(
            {
                "recipient_phone": recipient_phone,
                "message_body": message_body,
                "idempotency_key": idempotency_key,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class RealNameProvider(ScriptedProvider):
    name = delivery.PROVIDER_WHATSAPP


def config(**overrides):
    values = {
        "enabled": True,
        "access_token": TOKEN,
        "phone_number_id": "123456789012345",
        "api_version": "v99.1",
    }
    values.update(overrides)
    return delivery.WhatsAppConfig(**values)


class ConfigTests(unittest.TestCase):
    def test_default_is_disabled_and_fails_closed(self):
        cfg = delivery.load_whatsapp_config({})
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.ready)
        self.assertIn("WHATSAPP_ENABLED is not true", cfg.problems())

    def test_enabled_requires_all_fields(self):
        cfg = delivery.load_whatsapp_config({"WHATSAPP_ENABLED": "true"})
        self.assertFalse(cfg.ready)
        joined = " ".join(cfg.problems())
        self.assertIn("WHATSAPP_ACCESS_TOKEN", joined)
        self.assertIn("WHATSAPP_PHONE_NUMBER_ID", joined)
        self.assertIn("WHATSAPP_API_VERSION", joined)

    def test_malformed_values_are_rejected(self):
        cfg = delivery.load_whatsapp_config(
            {
                "WHATSAPP_ENABLED": "true",
                "WHATSAPP_ACCESS_TOKEN": "x",
                "WHATSAPP_PHONE_NUMBER_ID": "phone-123",
                "WHATSAPP_API_VERSION": "latest",
                "WHATSAPP_MAX_ATTEMPTS": "lots",
            }
        )
        self.assertFalse(cfg.ready)
        joined = " ".join(cfg.problems())
        self.assertIn("numeric", joined)
        self.assertIn("v<major>.<minor>", joined)
        self.assertIn("WHATSAPP_MAX_ATTEMPTS", joined)

    def test_invalid_enabled_flag_does_not_enable(self):
        cfg = delivery.load_whatsapp_config({"WHATSAPP_ENABLED": "sure"})
        self.assertFalse(cfg.enabled)
        self.assertFalse(cfg.ready)

    def test_valid_config_builds_endpoint_without_exposing_token(self):
        cfg = config()
        self.assertTrue(cfg.ready)
        provider = delivery.WhatsAppCloudApiProvider(cfg)
        self.assertEqual(
            provider.endpoint,
            "https://graph.facebook.com/v99.1/123456789012345/messages",
        )
        summary = cfg.safe_summary()
        self.assertTrue(summary["ready"])
        self.assertEqual(summary["phone_number_id_masked"], "…2345")
        self.assertNotIn(TOKEN, json.dumps(summary))

    def test_valid_environment_keys_produce_ready_config(self):
        cfg = delivery.load_whatsapp_config({
            "WHATSAPP_ENABLED": "yes",
            "WHATSAPP_ACCESS_TOKEN": TOKEN,
            "WHATSAPP_PHONE_NUMBER_ID": "123456789012345",
            "WHATSAPP_API_VERSION": "v99.1",
            "WHATSAPP_MAX_ATTEMPTS": "4",
        })
        self.assertTrue(cfg.ready)
        self.assertEqual(cfg.max_attempts, 4)
        self.assertNotIn(TOKEN, json.dumps(cfg.safe_summary()))

    def test_max_attempts_bounds(self):
        cfg = config(max_attempts=0)
        self.assertFalse(cfg.ready)
        self.assertIn("WHATSAPP_MAX_ATTEMPTS", " ".join(cfg.problems()))


class ProviderTests(unittest.TestCase):
    def send(self, transport, *, phone="9876543210", body="Please verify."):
        provider = delivery.WhatsAppCloudApiProvider(
            config(), transport=transport
        )
        return provider.send(phone, body)

    def test_success_requires_valid_message_id(self):
        transport = FakeTransport([
            transport_response(
                payload={"messages": [{"id": "wamid.TEST123"}]}
            )
        ])
        response = self.send(transport)
        self.assertEqual(response.outcome, delivery.OUTCOME_ACCEPTED)
        self.assertEqual(response.provider_message_id, "wamid.TEST123")
        call = transport.calls[0]
        self.assertEqual(call["url"],
                         "https://graph.facebook.com/v99.1/"
                         "123456789012345/messages")
        self.assertEqual(
            call["headers"]["Authorization"], f"Bearer {TOKEN}"
        )
        self.assertEqual(call["payload"]["messaging_product"], "whatsapp")
        self.assertEqual(call["payload"]["to"], "9876543210")
        self.assertEqual(
            call["payload"]["text"]["body"], "Please verify."
        )
        self.assertFalse(call["payload"]["text"]["preview_url"])

    def test_2xx_without_message_id_is_unknown(self):
        for payload in (
            {},
            {"messages": []},
            {"messages": [{}]},
            {"messages": [{"id": "  "}]},
        ):
            with self.subTest(payload=payload):
                response = self.send(
                    FakeTransport([transport_response(payload=payload)])
                )
                self.assertEqual(
                    response.outcome, delivery.OUTCOME_UNKNOWN
                )
                self.assertEqual(
                    response.error_code, "MISSING_PROVIDER_MESSAGE_ID"
                )

    def test_malformed_success_json_is_unknown(self):
        response = self.send(FakeTransport([
            delivery.TransportResponse(200, {}, b"not-json")
        ]))
        self.assertEqual(response.outcome, delivery.OUTCOME_UNKNOWN)

    def test_rate_limit_and_server_errors_are_retryable(self):
        cases = [
            (429, {"retry-after": "45"}),
            (500, {}),
            (503, {}),
        ]
        for status, headers in cases:
            with self.subTest(status=status):
                transport = FakeTransport([transport_response(
                    status=status,
                    payload={"error": {"code": 7, "message": "try later"}},
                    headers=headers,
                )])
                response = self.send(transport)
                self.assertEqual(
                    response.outcome, delivery.OUTCOME_RETRYABLE
                )
                if status == 429:
                    self.assertEqual(response.retry_after_seconds, 45)

    def test_auth_error_is_not_retryable_and_redacts_token(self):
        transport = FakeTransport([transport_response(
            status=401,
            payload={
                "error": {
                    "code": 190,
                    "message": f"bad token {TOKEN} 9876543210",
                }
            },
        )])
        response = self.send(transport)
        self.assertEqual(response.outcome, delivery.OUTCOME_AUTH_ERROR)
        self.assertNotIn(TOKEN, response.error_message)
        self.assertNotIn("9876543210", response.error_message)

    def test_invalid_request_is_not_retryable(self):
        response = self.send(FakeTransport([transport_response(
            status=400,
            payload={"error": {"code": 131026, "message": "bad recipient"}},
        )]))
        self.assertEqual(
            response.outcome, delivery.OUTCOME_INVALID_REQUEST
        )
        self.assertEqual(response.error_code, "131026")

    def test_timeout_and_connection_error_are_unknown(self):
        import requests

        for exception in (requests.Timeout("slow"), requests.ConnectionError()):
            with self.subTest(exception=type(exception).__name__):
                response = self.send(FakeTransport([exception]))
                self.assertEqual(response.outcome, delivery.OUTCOME_UNKNOWN)

    def test_invalid_recipient_sends_nothing(self):
        transport = FakeTransport([])
        response = self.send(transport, phone="abc")
        self.assertEqual(
            response.outcome, delivery.OUTCOME_INVALID_REQUEST
        )
        self.assertEqual(transport.calls, [])

    def test_disabled_config_sends_nothing(self):
        transport = FakeTransport([])
        provider = delivery.WhatsAppCloudApiProvider(
            delivery.WhatsAppConfig(), transport=transport
        )
        response = provider.send("9876543210", "hello")
        self.assertEqual(response.outcome, delivery.OUTCOME_CONFIG_ERROR)
        self.assertEqual(transport.calls, [])

    def test_mock_is_deterministic_and_labeled_simulated(self):
        provider = delivery.MockDeliveryProvider()
        first = provider.send("9876543210", "hello", idempotency_key="k")
        second = provider.send("9876543210", "hello", idempotency_key="k")
        self.assertTrue(first.simulated)
        self.assertEqual(first.outcome, delivery.OUTCOME_ACCEPTED)
        self.assertEqual(first.provider_message_id,
                         second.provider_message_id)
        self.assertTrue(first.provider_message_id.startswith("mock-"))

    def test_sanitize_error_limits_and_redacts(self):
        text = delivery.sanitize_error(
            f"Bearer abc {TOKEN} 9876543210 " + ("x" * 500),
            secrets=(TOKEN,),
        )
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("9876543210", text)
        self.assertLessEqual(len(text), 300)


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="milaan-test-")
        self._old_env = os.environ.get("DATABASE_PATH")
        os.environ["DATABASE_PATH"] = str(Path(self._tmp) / "test_milaan.db")
        db.init_db()

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop("DATABASE_PATH", None)
        else:
            os.environ["DATABASE_PATH"] = self._old_env
        shutil.rmtree(self._tmp, ignore_errors=True)

    def add_supplier(self, name, gstin, phone="9999999999"):
        with db.db() as conn:
            return conn.execute(
                """INSERT INTO suppliers (supplier_name, gstin, contact_phone)
                   VALUES (?, ?, ?)""",
                (name, gstin, phone),
            ).lastrowid

    def add_invoice(self, supplier_id, number, total_gst=225_000,
                    status="valid"):
        taxable = 1_250_000
        with db.db() as conn:
            return conn.execute(
                """INSERT INTO invoices
                       (supplier_id, invoice_number, invoice_date, tax_period,
                        taxable_value, gst_rate, cgst, sgst, igst, total_gst,
                        invoice_total, validation_status)
                   VALUES (?, ?, '2026-03-05', ?, ?, 18, ?, 0, 0, ?, ?, ?)""",
                (supplier_id, number, PERIOD, taxable, total_gst,
                 total_gst, taxable + total_gst, status),
            ).lastrowid

    def import_rows(self, rows):
        parsed = gstr2b.parse_gstr2b(csv_bytes(rows), "delivery.csv")
        return db.save_gstr2b_import(parsed)["import_id"]

    def approved_draft(self, phone="9999999999"):
        supplier_a = self.add_supplier("Alpha Traders", GSTIN_A, phone)
        invoice_id = self.add_invoice(supplier_a, "INV-A-1")
        supplier_b = self.add_supplier("Beta Traders", GSTIN_B, "8888888888")
        self.add_invoice(supplier_b, "INV-B-1", total_gst=90_000)
        batch_id = self.import_rows([
            gstr2b_row(GSTIN_B, "INV-B-1", total_gst_paise=90_000)
        ])
        db.reconcile_gstr2b_import(batch_id)
        draft_id = db.generate_followup_drafts(
            batch_id
        )["draft_ids"][0]
        db.approve_followup_draft(draft_id, confirmed=True)
        return {
            "supplier_id": supplier_a,
            "invoice_id": invoice_id,
            "batch_id": batch_id,
            "draft_id": draft_id,
        }

    def delivery_count(self):
        with db.db() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM message_deliveries"
            ).fetchone()[0]


class ServiceTests(DatabaseTestCase):
    def test_approval_freezes_snapshot_and_delivery_uses_it(self):
        case = self.approved_draft()
        draft = db.get_followup_draft(case["draft_id"])
        self.assertEqual(draft["approved_text"], draft["draft_text"])
        self.assertEqual(
            draft["approved_recipient_phone"], "9999999999"
        )
        self.assertTrue(draft["approved_at"])

        provider = ScriptedProvider([
            delivery.ProviderResponse(
                outcome=delivery.OUTCOME_ACCEPTED,
                provider_message_id="wamid.SNAPSHOT",
            )
        ])
        record = delivery.deliver_approved_draft(
            case["draft_id"], provider
        )
        self.assertEqual(record["status"], "SENT")
        self.assertEqual(record["provider_message_id"], "wamid.SNAPSHOT")
        self.assertEqual(record["attempt_count"], 1)
        self.assertEqual(provider.calls[0]["recipient_phone"],
                         "9999999999")
        self.assertEqual(provider.calls[0]["message_body"],
                         draft["draft_text"])

    def test_unapproved_draft_is_blocked_without_record(self):
        supplier = self.add_supplier("Alpha Traders", GSTIN_A)
        invoice = self.add_invoice(supplier, "INV-A-1")
        supplier_b = self.add_supplier("Beta Traders", GSTIN_B)
        self.add_invoice(supplier_b, "INV-B-1", total_gst=90_000)
        batch_id = self.import_rows([
            gstr2b_row(GSTIN_B, "INV-B-1", total_gst_paise=90_000)
        ])
        db.reconcile_gstr2b_import(batch_id)
        draft_id = db.generate_followup_drafts(batch_id)["draft_ids"][0]
        provider = ScriptedProvider([])
        with self.assertRaises(delivery.DeliveryBlockedError) as caught:
            delivery.deliver_approved_draft(draft_id, provider)
        self.assertIn("DRAFT_NOT_APPROVED", str(caught.exception))
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.delivery_count(), 0)
        self.assertEqual(
            db.get_followup_draft(draft_id)["status"], "DRAFT"
        )
        self.assertTrue(invoice)

    def test_changed_phone_blocks_delivery(self):
        case = self.approved_draft()
        with db.db() as conn:
            conn.execute(
                "UPDATE suppliers SET contact_phone = '7777777777' "
                "WHERE id = ?",
                (case["supplier_id"],),
            )
        provider = ScriptedProvider([])
        with self.assertRaises(delivery.DeliveryBlockedError) as caught:
            delivery.deliver_approved_draft(case["draft_id"], provider)
        self.assertIn("RECIPIENT_PHONE_CHANGED", str(caught.exception))
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.delivery_count(), 0)

    def test_changed_approved_text_blocks_delivery(self):
        case = self.approved_draft()
        with db.db() as conn:
            conn.execute(
                """UPDATE followup_drafts
                   SET approved_text = 'tampered'
                   WHERE id = ?""",
                (case["draft_id"],),
            )
        provider = ScriptedProvider([])
        with self.assertRaises(delivery.DeliveryBlockedError) as caught:
            delivery.deliver_approved_draft(case["draft_id"], provider)
        self.assertIn("APPROVED_TEXT_CHANGED", str(caught.exception))
        self.assertEqual(provider.calls, [])
        self.assertEqual(self.delivery_count(), 0)

    def test_changed_supplier_gstin_blocks_delivery(self):
        case = self.approved_draft()
        with db.db() as conn:
            conn.execute(
                "UPDATE suppliers SET gstin = ? WHERE id = ?",
                (GSTIN_C, case["supplier_id"]),
            )
        with self.assertRaises(delivery.DeliveryBlockedError) as caught:
            delivery.deliver_approved_draft(
                case["draft_id"], ScriptedProvider([])
            )
        self.assertIn("SUPPLIER_GSTIN_CHANGED", str(caught.exception))

    def test_invalidated_invoice_blocks_delivery(self):
        case = self.approved_draft()
        db.update_invoice_validation(case["invoice_id"], "invalid")
        with self.assertRaises(delivery.DeliveryBlockedError) as caught:
            delivery.deliver_approved_draft(
                case["draft_id"], ScriptedProvider([])
            )
        self.assertIn("INVOICE_NOT_VALID", str(caught.exception))
        self.assertEqual(self.delivery_count(), 0)

    def test_missing_legacy_snapshot_blocks_delivery(self):
        case = self.approved_draft()
        with db.db() as conn:
            conn.execute(
                """UPDATE followup_drafts
                   SET approved_text = NULL,
                       approved_recipient_phone = NULL,
                       approved_at = NULL
                   WHERE id = ?""",
                (case["draft_id"],),
            )
        with self.assertRaises(delivery.DeliveryBlockedError) as caught:
            delivery.deliver_approved_draft(
                case["draft_id"], ScriptedProvider([])
            )
        self.assertIn("APPROVAL_SNAPSHOT_MISSING", str(caught.exception))

    def test_new_reconciliation_run_makes_draft_stale(self):
        case = self.approved_draft()
        db.reconcile_gstr2b_import(case["batch_id"])
        with self.assertRaises(delivery.DeliveryBlockedError) as caught:
            delivery.deliver_approved_draft(
                case["draft_id"], ScriptedProvider([])
            )
        self.assertIn("STALE_RECONCILIATION_RUN", str(caught.exception))

    def test_duplicate_clicks_do_not_duplicate_or_resend(self):
        case = self.approved_draft()
        provider = ScriptedProvider([
            delivery.ProviderResponse(
                outcome=delivery.OUTCOME_ACCEPTED,
                provider_message_id="wamid.ONCE",
            )
        ])
        first = delivery.deliver_approved_draft(
            case["draft_id"], provider
        )
        second = delivery.deliver_approved_draft(
            case["draft_id"], provider
        )
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(self.delivery_count(), 1)
        self.assertEqual(len(provider.calls), 1)

    def test_create_is_idempotent_and_freezes_snapshot(self):
        case = self.approved_draft()
        draft = db.get_followup_draft(case["draft_id"])
        key = "fixed-test-key"
        first = db.create_message_delivery(
            case["draft_id"], provider="mock",
            idempotency_key=key, max_attempts=3,
        )
        second = db.create_message_delivery(
            case["draft_id"], provider="mock",
            idempotency_key=key, max_attempts=3,
        )
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(
            first["delivery"]["id"], second["delivery"]["id"]
        )
        self.assertEqual(
            first["delivery"]["message_body_snapshot"],
            draft["approved_text"],
        )
        self.assertEqual(
            first["delivery"]["recipient_phone_snapshot"],
            draft["approved_recipient_phone"],
        )
        self.assertEqual(self.delivery_count(), 1)

    def test_atomic_claim_and_completion_transitions(self):
        case = self.approved_draft()
        delivery_id = db.create_message_delivery(
            case["draft_id"], provider="mock",
            idempotency_key="claim-key",
        )["delivery"]["id"]
        self.assertTrue(db.claim_message_delivery(delivery_id))
        self.assertFalse(db.claim_message_delivery(delivery_id))
        record = db.get_message_delivery(delivery_id)
        self.assertEqual(record["status"], "SENDING")
        self.assertEqual(record["attempt_count"], 1)
        with self.assertRaises(ValueError):
            db.finish_message_delivery_attempt(
                delivery_id, status="DELIVERED"
            )
        with self.assertRaises(ValueError):
            db.finish_message_delivery_attempt(
                delivery_id, status="READ"
            )
        with self.assertRaises(ValueError):
            db.finish_message_delivery_attempt(
                delivery_id, status="SENT", provider_message_id="  "
            )
        db.finish_message_delivery_attempt(
            delivery_id, status="SENT",
            provider_message_id="wamid.CLAIM",
        )
        with self.assertRaises(ValueError):
            db.finish_message_delivery_attempt(
                delivery_id, status="FAILED"
            )
        self.assertEqual(
            db.get_message_delivery(delivery_id)["status"], "SENT"
        )

    def test_rate_limit_then_manual_retry(self):
        case = self.approved_draft()
        provider = ScriptedProvider(
            [
                delivery.ProviderResponse(
                    outcome=delivery.OUTCOME_RETRYABLE,
                    error_code="RATE_LIMITED",
                    error_message="slow down",
                    retry_after_seconds=60,
                ),
                delivery.ProviderResponse(
                    outcome=delivery.OUTCOME_ACCEPTED,
                    provider_message_id="wamid.RETRY",
                ),
            ],
            max_attempts=3,
        )
        first = delivery.deliver_approved_draft(
            case["draft_id"], provider
        )
        self.assertEqual(first["status"], "RETRY_PENDING")
        self.assertEqual(first["attempt_count"], 1)
        self.assertTrue(first["next_attempt_at"])

        with self.assertRaises(delivery.DeliveryBlockedError):
            delivery.retry_message_delivery(first["id"], provider)
        self.assertEqual(len(provider.calls), 1)

        later = datetime.now(timezone.utc) + timedelta(seconds=61)
        second = delivery.retry_message_delivery(
            first["id"], provider, now=later
        )
        self.assertEqual(second["status"], "SENT")
        self.assertEqual(second["attempt_count"], 2)
        self.assertEqual(len(provider.calls), 2)

    def test_retry_exhaustion_fails_without_extra_attempt(self):
        case = self.approved_draft()
        provider = ScriptedProvider(
            [delivery.ProviderResponse(
                outcome=delivery.OUTCOME_RETRYABLE,
                error_code="SERVER_ERROR_500",
            )],
            max_attempts=1,
        )
        record = delivery.deliver_approved_draft(
            case["draft_id"], provider
        )
        self.assertEqual(record["status"], "FAILED")
        self.assertEqual(record["attempt_count"], 1)
        self.assertEqual(record["last_error_code"], "RETRIES_EXHAUSTED")
        with self.assertRaises(delivery.DeliveryBlockedError):
            delivery.retry_message_delivery(record["id"], provider)
        self.assertEqual(len(provider.calls), 1)

    def test_retry_rejects_a_different_provider(self):
        case = self.approved_draft()
        provider = ScriptedProvider([delivery.ProviderResponse(
            outcome=delivery.OUTCOME_RETRYABLE,
            error_code="RATE_LIMITED",
            retry_after_seconds=0,
        )])
        record = delivery.deliver_approved_draft(
            case["draft_id"], provider
        )
        other = RealNameProvider([])
        with self.assertRaises(delivery.DeliveryBlockedError) as caught:
            delivery.retry_message_delivery(record["id"], other)
        self.assertIn("provider", str(caught.exception))
        self.assertEqual(other.calls, [])

    def test_terminal_failure_keeps_one_record_per_draft(self):
        case = self.approved_draft()
        first = db.create_message_delivery(
            case["draft_id"], provider="mock", idempotency_key="terminal-key"
        )["delivery"]
        db.claim_message_delivery(first["id"])
        db.finish_message_delivery_attempt(
            first["id"], status="FAILED", error_code="TERMINAL"
        )
        with self.assertRaises(ValueError) as caught:
            db.create_message_delivery(
                case["draft_id"], provider="mock",
                idempotency_key="different-key",
            )
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(self.delivery_count(), 1)

    def test_auth_failure_is_terminal(self):
        case = self.approved_draft()
        provider = ScriptedProvider([delivery.ProviderResponse(
            outcome=delivery.OUTCOME_AUTH_ERROR,
            http_status=401,
            error_code="AUTHENTICATION_FAILED",
            error_message="bad credentials",
        )])
        record = delivery.deliver_approved_draft(
            case["draft_id"], provider
        )
        self.assertEqual(record["status"], "FAILED")
        with self.assertRaises(delivery.DeliveryBlockedError):
            delivery.retry_message_delivery(record["id"], provider)

    def test_timeout_is_unknown_and_never_auto_resent(self):
        case = self.approved_draft()
        provider = ScriptedProvider([delivery.ProviderResponse(
            outcome=delivery.OUTCOME_UNKNOWN,
            error_code="TIMEOUT",
            error_message="ambiguous",
        )])
        record = delivery.deliver_approved_draft(
            case["draft_id"], provider
        )
        self.assertEqual(record["status"], "UNKNOWN")
        self.assertEqual(record["attempt_count"], 1)
        with self.assertRaises(delivery.DeliveryBlockedError):
            delivery.retry_message_delivery(record["id"], provider)
        self.assertEqual(len(provider.calls), 1)

    def test_unknown_confirmed_sent_records_sent_never_delivered(self):
        case = self.approved_draft()
        provider = ScriptedProvider([delivery.ProviderResponse(
            outcome=delivery.OUTCOME_UNKNOWN,
            error_code="TIMEOUT",
        )])
        record = delivery.deliver_approved_draft(
            case["draft_id"], provider
        )
        delivery.resolve_unknown(
            record["id"], outcome="SENT_CONFIRMED",
            resolved_by="operator", note="Meta dashboard shows accepted",
            provider_message_id="wamid.MANUAL",
        )
        updated = db.get_message_delivery(record["id"])
        self.assertEqual(updated["status"], "SENT")
        self.assertEqual(updated["provider_message_id"], "wamid.MANUAL")
        self.assertEqual(updated["resolution_outcome"], "SENT_CONFIRMED")
        self.assertIsNone(updated["last_error_code"])
        self.assertIsNone(updated["delivered_at"])

    def test_unknown_not_sent_allows_bounded_retry(self):
        case = self.approved_draft()
        provider = ScriptedProvider([delivery.ProviderResponse(
            outcome=delivery.OUTCOME_UNKNOWN,
            error_code="TIMEOUT",
        )])
        record = delivery.deliver_approved_draft(
            case["draft_id"], provider
        )
        delivery.resolve_unknown(
            record["id"], outcome="NOT_SENT",
            resolved_by="operator", note="Provider confirms no send",
        )
        updated = db.get_message_delivery(record["id"])
        self.assertEqual(updated["status"], "RETRY_PENDING")
        self.assertEqual(updated["attempt_count"], 1)
        self.assertEqual(updated["resolution_outcome"], "NOT_SENT")

    def test_unknown_not_sent_exhausted_becomes_failed(self):
        case = self.approved_draft()
        provider = ScriptedProvider(
            [delivery.ProviderResponse(
                outcome=delivery.OUTCOME_UNKNOWN,
                error_code="TIMEOUT",
            )],
            max_attempts=1,
        )
        record = delivery.deliver_approved_draft(
            case["draft_id"], provider
        )
        delivery.resolve_unknown(
            record["id"], outcome="NOT_SENT",
            resolved_by="operator", note="Provider confirms no send",
        )
        updated = db.get_message_delivery(record["id"])
        self.assertEqual(updated["status"], "FAILED")
        self.assertEqual(updated["last_error_code"], "UNKNOWN_NOT_SENT")
        self.assertEqual(updated["resolution_outcome"], "NOT_SENT")

    def test_resolution_requires_evidence_and_valid_state(self):
        case = self.approved_draft()
        record = delivery.deliver_approved_draft(
            case["draft_id"], ScriptedProvider([delivery.ProviderResponse(
                outcome=delivery.OUTCOME_UNKNOWN,
                error_code="TIMEOUT",
            )])
        )
        with self.assertRaises(ValueError):
            delivery.resolve_unknown(
                record["id"], outcome="SENT_CONFIRMED",
                resolved_by="operator", note="no id supplied",
            )
        with self.assertRaises(ValueError):
            delivery.resolve_unknown(
                record["id"], outcome="NOT_SENT",
                resolved_by="", note="missing operator",
            )
        with self.assertRaises(ValueError):
            delivery.resolve_unknown(
                record["id"], outcome="DELIVERED",
                resolved_by="operator", note="not allowed",
            )

    def test_unconfigured_real_provider_fails_closed(self):
        case = self.approved_draft()
        provider = delivery.WhatsAppCloudApiProvider(
            delivery.WhatsAppConfig(), transport=FakeTransport([])
        )
        with self.assertRaises(delivery.DeliveryConfigurationError):
            delivery.deliver_approved_draft(case["draft_id"], provider)
        self.assertEqual(self.delivery_count(), 0)
        self.assertEqual(
            db.get_followup_draft(case["draft_id"])["status"], "APPROVED"
        )

    def test_existing_unresolved_delivery_blocks_a_new_one(self):
        case = self.approved_draft()
        db.create_message_delivery(
            case["draft_id"], provider="mock",
            idempotency_key="first-key",
        )
        with self.assertRaises(ValueError) as caught:
            db.create_message_delivery(
                case["draft_id"], provider="mock",
                idempotency_key="second-key",
            )
        self.assertIn("already exists", str(caught.exception))
        self.assertEqual(self.delivery_count(), 1)

    def test_provider_exception_is_unknown_and_token_not_stored(self):
        case = self.approved_draft()
        provider = ScriptedProvider([
            RuntimeError(f"transport exploded with {TOKEN}")
        ])
        record = delivery.deliver_approved_draft(
            case["draft_id"], provider
        )
        self.assertEqual(record["status"], "UNKNOWN")
        serialized = json.dumps(record)
        self.assertNotIn(TOKEN, serialized)
        self.assertNotIn("exploded", serialized)

    def test_mock_delivery_is_simulated_and_sent(self):
        case = self.approved_draft()
        with mock.patch("requests.post") as post:
            record = delivery.deliver_approved_draft(
                case["draft_id"], delivery.MockDeliveryProvider()
            )
        post.assert_not_called()
        self.assertEqual(record["status"], "SENT")
        self.assertEqual(record["provider"], "mock")
        self.assertTrue(
            record["provider_message_id"].startswith("mock-")
        )

    def test_accepted_without_provider_id_becomes_unknown(self):
        case = self.approved_draft()
        provider = ScriptedProvider([delivery.ProviderResponse(
            outcome=delivery.OUTCOME_ACCEPTED,
            provider_message_id=None,
        )])
        record = delivery.deliver_approved_draft(
            case["draft_id"], provider
        )
        self.assertEqual(record["status"], "UNKNOWN")
        self.assertEqual(
            record["last_error_code"], "MISSING_PROVIDER_MESSAGE_ID"
        )
        self.assertEqual(
            db.get_followup_draft(case["draft_id"])["status"], "APPROVED"
        )

    def test_retry_revalidates_evidence_before_dispatch(self):
        case = self.approved_draft()
        provider = ScriptedProvider([
            delivery.ProviderResponse(
                outcome=delivery.OUTCOME_RETRYABLE,
                error_code="RATE_LIMITED",
                retry_after_seconds=0,
            ),
            delivery.ProviderResponse(
                outcome=delivery.OUTCOME_ACCEPTED,
                provider_message_id="wamid.MUST_NOT_SEND",
            ),
        ])
        record = delivery.deliver_approved_draft(
            case["draft_id"], provider
        )
        self.assertEqual(record["status"], "RETRY_PENDING")
        with db.db() as conn:
            conn.execute(
                "UPDATE suppliers SET contact_phone = '7777777777' "
                "WHERE id = ?",
                (case["supplier_id"],),
            )
        with self.assertRaises(delivery.DeliveryBlockedError) as caught:
            delivery.retry_message_delivery(record["id"], provider)
        self.assertIn("RECIPIENT_PHONE_CHANGED", str(caught.exception))
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(
            db.get_message_delivery(record["id"])["status"],
            "RETRY_PENDING",
        )

    def test_webhook_support_is_explicitly_deferred(self):
        support = delivery.webhook_support()
        self.assertFalse(support["supported"])
        self.assertIn("DELIVERED/READ", support["reason"])

    def test_idempotency_key_changes_with_snapshot(self):
        first = delivery.compute_idempotency_key(
            draft_id=1, approved_at="2026-01-01 00:00:00",
            approved_text="hello", approved_recipient_phone="9876543210",
            provider="mock",
        )
        second = delivery.compute_idempotency_key(
            draft_id=1, approved_at="2026-01-01 00:00:00",
            approved_text="hello!", approved_recipient_phone="9876543210",
            provider="mock",
        )
        self.assertNotEqual(first, second)
        self.assertEqual(len(first), 64)


class SchemaTests(DatabaseTestCase):
    def test_task7_tables_and_approval_snapshot_columns_exist(self):
        with db.db() as conn:
            draft_columns = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(followup_drafts)"
                ).fetchall()
            }
            tables = {
                row["name"] for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertTrue(
            {"approved_text", "approved_recipient_phone", "approved_at"}
            <= draft_columns
        )
        self.assertIn("message_deliveries", tables)

    def test_foreign_key_check_is_clean(self):
        with db.db() as conn:
            problems = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertEqual(problems, [])

    def test_additive_migration_restores_approval_snapshot_columns(self):
        with db.db() as conn:
            for column in (
                "approved_text",
                "approved_recipient_phone",
                "approved_at",
            ):
                conn.execute(
                    f"ALTER TABLE followup_drafts DROP COLUMN {column}"
                )
            missing = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(followup_drafts)"
                ).fetchall()
            }
        self.assertNotIn("approved_text", missing)
        db.init_db()
        with db.db() as conn:
            restored = {
                row["name"] for row in conn.execute(
                    "PRAGMA table_info(followup_drafts)"
                ).fetchall()
            }
            fk = conn.execute("PRAGMA foreign_key_check").fetchall()
        self.assertTrue(
            {"approved_text", "approved_recipient_phone", "approved_at"}
            <= restored
        )
        self.assertEqual(fk, [])

    def test_delivery_records_are_removed_with_draft(self):
        case = self.approved_draft()
        db.create_message_delivery(
            case["draft_id"], provider="mock", idempotency_key="cascade-key"
        )
        with db.db() as conn:
            conn.execute(
                "DELETE FROM followup_drafts WHERE id = ?",
                (case["draft_id"],),
            )
            remaining = conn.execute(
                "SELECT COUNT(*) FROM message_deliveries"
            ).fetchone()[0]
        self.assertEqual(remaining, 0)


class DeliveryUITests(DatabaseTestCase):
    def app(self):
        from streamlit.testing.v1 import AppTest

        at = AppTest.from_file(
            str(Path(__file__).parent / "app.py"), default_timeout=30
        )
        at.run()
        at.switch_page("pages/6_followups.py").run()
        return at

    def test_ui_does_not_send_on_render_or_approval(self):
        case = self.approved_draft()
        at = self.app()
        self.assertEqual(at.exception, [])
        self.assertEqual(self.delivery_count(), 0)
        self.assertEqual(
            db.get_followup_draft(case["draft_id"])["status"], "APPROVED"
        )
        captions = " ".join(item.value for item in at.caption)
        self.assertIn("approval alone sends nothing", captions)

    def test_ui_requires_confirmation_then_records_mock_delivery(self):
        case = self.approved_draft()
        draft_id = case["draft_id"]
        with mock.patch("requests.post") as post:
            at = self.app()
            self.assertEqual(at.exception, [])
            at.checkbox(key=f"delivery_confirm_{draft_id}").check().run()
            at.button(key=f"delivery_send_{draft_id}").click().run()
        self.assertEqual(at.exception, [])
        post.assert_not_called()

        records = db.get_message_deliveries(draft_id=draft_id)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["provider"], "mock")
        self.assertEqual(records[0]["status"], "SENT")
        self.assertTrue(
            any(
                "No external message was sent" in item.value
                for item in at.success
            ),
            [item.value for item in at.success],
        )

    def test_ui_real_mode_is_disabled_without_configuration(self):
        self.approved_draft()
        with mock.patch.dict(os.environ, {"WHATSAPP_ENABLED": "false"}):
            at = self.app()
            at.radio(key="delivery_mode").set_value(
                "WhatsApp Cloud API (real)"
            ).run()
        self.assertEqual(at.exception, [])
        warnings = " ".join(item.value for item in at.warning)
        self.assertIn("Real delivery is disabled", warnings)
        self.assertEqual(self.delivery_count(), 0)

    def test_ui_live_delivery_stays_disabled_even_when_configured(self):
        self.approved_draft()
        configured = {
            "WHATSAPP_ENABLED": "true",
            "WHATSAPP_ACCESS_TOKEN": "test-token-only",
            "WHATSAPP_PHONE_NUMBER_ID": "1234567890",
            "WHATSAPP_API_VERSION": "v99.0",
        }
        with mock.patch.dict(os.environ, configured):
            at = self.app()
            at.radio(key="delivery_mode").set_value(
                "WhatsApp Cloud API (real)"
            ).run()

        self.assertEqual(at.exception, [])
        warnings = " ".join(item.value for item in at.warning)
        self.assertIn("disabled in this build", warnings)
        self.assertFalse(
            any("Send via WhatsApp Cloud API" in item.label for item in at.button)
        )
        self.assertEqual(self.delivery_count(), 0)


if __name__ == "__main__":
    unittest.main()
