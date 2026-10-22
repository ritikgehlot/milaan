"""WhatsApp Cloud API delivery integration (Task 7).

All provider networking lives here. ``db.py`` stores delivery state and
``app.py`` orchestrates UI actions, but neither performs HTTP calls.

Safety model:

- Real delivery is fail-closed: ``WHATSAPP_ENABLED`` defaults to false, all
  credentials are validated, and no request is attempted when configuration
  is absent or malformed.
- A send is only recorded ``SENT`` when Meta returns a valid provider message
  ID. HTTP 2xx without an ID is ``UNKNOWN``.
- Timeouts/ambiguous transport outcomes become ``UNKNOWN`` and are never
  retried automatically.
- Only definitive rate-limit (HTTP 429) and server (HTTP 5xx) responses are
  retryable, and only through a bounded manual retry action.
- ``DELIVERED``/``READ`` cannot be produced here. Webhooks are explicitly
  deferred until signature verification can be implemented and tested.
- Access tokens, raw responses and unnecessary personal data are never
  stored or logged. Error text is redacted and length-limited.

No message is ever sent by importing this module or loading the UI.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Protocol
from urllib.parse import quote

import requests

import db

PROVIDER_WHATSAPP = "whatsapp_cloud_api"
PROVIDER_MOCK = "mock"

OUTCOME_ACCEPTED = "ACCEPTED"
OUTCOME_RETRYABLE = "RETRYABLE"
OUTCOME_AUTH_ERROR = "AUTH_ERROR"
OUTCOME_INVALID_REQUEST = "INVALID_REQUEST"
OUTCOME_UNKNOWN = "UNKNOWN"
OUTCOME_CONFIG_ERROR = "CONFIG_ERROR"

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off", ""}
_API_VERSION_RE = re.compile(r"^v\d+\.\d+$")
_PHONE_ID_RE = re.compile(r"^\d{5,30}$")

DEFAULT_MAX_ATTEMPTS = 3
MAX_ATTEMPTS_LIMIT = 5
DEFAULT_TIMEOUT_SECONDS = 10.0


class DeliveryError(RuntimeError):
    """Base error with a safe, non-secret message."""


class DeliveryConfigurationError(DeliveryError):
    """Real delivery is disabled or misconfigured; nothing was attempted."""


class DeliveryBlockedError(DeliveryError):
    """Draft/current state does not permit a delivery attempt."""


@dataclass(frozen=True)
class WhatsAppConfig:
    enabled: bool = False
    access_token: str | None = None
    phone_number_id: str | None = None
    api_version: str | None = None
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    issues: tuple[str, ...] = field(default_factory=tuple)

    def problems(self) -> list[str]:
        problems = list(self.issues)
        if not self.enabled:
            problems.append("WHATSAPP_ENABLED is not true")
            return problems
        if not self.access_token or not self.access_token.strip():
            problems.append("WHATSAPP_ACCESS_TOKEN is missing")
        if not self.phone_number_id or not self.phone_number_id.strip():
            problems.append("WHATSAPP_PHONE_NUMBER_ID is missing")
        elif not _PHONE_ID_RE.fullmatch(self.phone_number_id.strip()):
            problems.append("WHATSAPP_PHONE_NUMBER_ID must be numeric")
        if not self.api_version or not self.api_version.strip():
            problems.append("WHATSAPP_API_VERSION is missing")
        elif not _API_VERSION_RE.fullmatch(self.api_version.strip()):
            problems.append(
                "WHATSAPP_API_VERSION must look like v<major>.<minor>"
            )
        if not 1 <= self.max_attempts <= MAX_ATTEMPTS_LIMIT:
            problems.append(
                "WHATSAPP_MAX_ATTEMPTS must be between 1 and "
                f"{MAX_ATTEMPTS_LIMIT}"
            )
        if not 0 < self.timeout_seconds <= 60:
            problems.append("delivery timeout must be between 0 and 60s")
        return problems

    @property
    def ready(self) -> bool:
        return not self.problems()

    def safe_summary(self) -> dict:
        phone = (self.phone_number_id or "").strip()
        return {
            "enabled": self.enabled,
            "ready": self.ready,
            "api_version": (self.api_version or "").strip() or None,
            "phone_number_id_masked": (
                "…" + phone[-4:] if len(phone) >= 4 else None
            ),
            "max_attempts": self.max_attempts,
            "problems": self.problems(),
        }


def load_whatsapp_config(
    environ: Mapping[str, str] | None = None,
) -> WhatsAppConfig:
    """Read configuration from the environment. Never reads a .env file."""
    env = os.environ if environ is None else environ
    issues: list[str] = []
    raw_enabled = (env.get("WHATSAPP_ENABLED") or "").strip().lower()
    if raw_enabled in _TRUE_VALUES:
        enabled = True
    elif raw_enabled in _FALSE_VALUES:
        enabled = False
    else:
        enabled = False
        issues.append(
            "WHATSAPP_ENABLED must be true or false"
        )

    raw_attempts = (env.get("WHATSAPP_MAX_ATTEMPTS") or "").strip()
    max_attempts = DEFAULT_MAX_ATTEMPTS
    if raw_attempts:
        try:
            max_attempts = int(raw_attempts)
        except ValueError:
            issues.append("WHATSAPP_MAX_ATTEMPTS must be an integer")

    config = WhatsAppConfig(
        enabled=enabled,
        access_token=(env.get("WHATSAPP_ACCESS_TOKEN") or "").strip() or None,
        phone_number_id=(
            env.get("WHATSAPP_PHONE_NUMBER_ID") or ""
        ).strip() or None,
        api_version=(
            env.get("WHATSAPP_API_VERSION") or ""
        ).strip() or None,
        max_attempts=max_attempts,
        issues=tuple(issues),
    )
    return config


@dataclass(frozen=True)
class TransportResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class ProviderResponse:
    outcome: str
    provider_message_id: str | None = None
    http_status: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    retry_after_seconds: int | None = None
    simulated: bool = False


class DeliveryProvider(Protocol):
    name: str
    is_ready: bool
    max_attempts: int

    def send(
        self,
        recipient_phone: str,
        message_body: str,
        *,
        idempotency_key: str | None = None,
    ) -> ProviderResponse: ...


def normalize_recipient_phone(value: str | None) -> str | None:
    """Digits for Meta's ``to`` field, or None for an implausible number.

    This is a syntax check only; it does not prove ownership or consent.
    """
    return db.normalize_phone_digits(value)


def sanitize_error(
    text: Any,
    *,
    secrets: tuple[str | None, ...] = (),
    limit: int = 300,
) -> str:
    """Redact credentials, long digit runs and excess whitespace."""
    value = str(text or "")
    for secret in secrets:
        if secret:
            value = value.replace(secret, "[redacted]")
    value = re.sub(r"(?i)bearer\s+\S+", "Bearer [redacted]", value)
    value = re.sub(r"\d{8,}", "[number]", value)
    value = " ".join(value.split())
    return value[:limit]


def _retry_after_seconds(headers: Mapping[str, str]) -> int | None:
    lowered = {str(key).lower(): value for key, value in headers.items()}
    raw = lowered.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = int(str(raw).strip())
    except ValueError:
        return None
    return max(0, min(seconds, 86_400))


def _provider_error(body: bytes) -> tuple[str | None, str | None]:
    """Extract only a code and a short sanitized message from an error."""
    try:
        payload = json.loads(body.decode("utf-8")) if body else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if not isinstance(payload, dict):
        return None, None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None, None
    code = error.get("code")
    message = (
        error.get("message")
        or error.get("error_user_title")
        or error.get("error_user_msg")
    )
    clean_code = sanitize_error(code, limit=100) if code is not None else None
    clean_message = sanitize_error(message) if message else None
    return clean_code or None, clean_message or None


def _default_transport(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout: float,
) -> TransportResponse:
    response = requests.post(
        url,
        headers=dict(headers),
        data=json.dumps(payload).encode("utf-8"),
        timeout=timeout,
    )
    return TransportResponse(
        status_code=response.status_code,
        headers=dict(response.headers),
        body=response.content,
    )


class WhatsAppCloudApiProvider:
    """Official WhatsApp Cloud API text-message sender.

    Networking is isolated behind ``transport`` so tests never touch the
    network. The configured token is sent only in the Authorization header
    and is scrubbed from all error text.
    """

    name = PROVIDER_WHATSAPP

    def __init__(
        self,
        config: WhatsAppConfig,
        *,
        transport=None,
    ) -> None:
        self.config = config
        self.max_attempts = config.max_attempts
        self._transport = transport or _default_transport
        self.is_ready = config.ready

    @property
    def endpoint(self) -> str:
        version = quote((self.config.api_version or "").strip(), safe=".")
        phone_id = quote((self.config.phone_number_id or "").strip(), safe="")
        return (
            f"https://graph.facebook.com/{version}/{phone_id}/messages"
        )

    def _failure(
        self,
        outcome: str,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        retry_after: int | None = None,
    ) -> ProviderResponse:
        return ProviderResponse(
            outcome=outcome,
            http_status=http_status,
            error_code=sanitize_error(code, limit=100) or code,
            error_message=sanitize_error(
                message, secrets=(self.config.access_token,)
            ),
            retry_after_seconds=retry_after,
        )

    def send(
        self,
        recipient_phone: str,
        message_body: str,
        *,
        idempotency_key: str | None = None,
    ) -> ProviderResponse:
        if not self.is_ready:
            return self._failure(
                OUTCOME_CONFIG_ERROR,
                "CONFIGURATION_INVALID",
                "; ".join(self.config.problems()),
            )
        recipient = normalize_recipient_phone(recipient_phone)
        if recipient is None:
            return self._failure(
                OUTCOME_INVALID_REQUEST,
                "INVALID_RECIPIENT",
                "Recipient phone number is not plausible.",
            )
        if not isinstance(message_body, str) or not message_body.strip():
            return self._failure(
                OUTCOME_INVALID_REQUEST,
                "EMPTY_MESSAGE",
                "Message body is empty.",
            )

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": False, "body": message_body},
        }
        headers = {
            "Authorization": f"Bearer {self.config.access_token}",
            "Content-Type": "application/json",
        }
        try:
            response = self._transport(
                self.endpoint,
                headers,
                payload,
                self.config.timeout_seconds,
            )
        except (TimeoutError, requests.Timeout):
            return self._failure(
                OUTCOME_UNKNOWN,
                "TIMEOUT",
                "Request timed out after it may have reached the provider.",
            )
        except requests.ConnectionError:
            return self._failure(
                OUTCOME_UNKNOWN,
                "CONNECTION_ERROR",
                "Network connection failed after the request may have been "
                "transmitted.",
            )
        except Exception as exc:  # never echo exception text (may leak)
            return self._failure(
                OUTCOME_UNKNOWN,
                "TRANSPORT_ERROR",
                f"Transport failed ({type(exc).__name__}).",
            )

        status = response.status_code
        body = response.body or b""
        provider_code, provider_message = _provider_error(body)

        if 200 <= status < 300:
            try:
                parsed = json.loads(body.decode("utf-8")) if body else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = None
            messages = (
                parsed.get("messages")
                if isinstance(parsed, dict) else None
            )
            provider_id = None
            if isinstance(messages, list) and messages:
                candidate = messages[0].get("id") if isinstance(
                    messages[0], dict
                ) else None
                if isinstance(candidate, str) and candidate.strip():
                    provider_id = candidate.strip()[:200]
            if provider_id is None:
                return self._failure(
                    OUTCOME_UNKNOWN,
                    "MISSING_PROVIDER_MESSAGE_ID",
                    "Provider accepted the HTTP request but did not return "
                    "a valid message ID.",
                    http_status=status,
                )
            return ProviderResponse(
                outcome=OUTCOME_ACCEPTED,
                provider_message_id=provider_id,
                http_status=status,
            )

        retry_after = _retry_after_seconds(response.headers)
        if status == 429:
            return self._failure(
                OUTCOME_RETRYABLE,
                provider_code or "RATE_LIMITED",
                provider_message or "Provider rate limit reached.",
                http_status=status,
                retry_after=retry_after,
            )
        if 500 <= status <= 599:
            return self._failure(
                OUTCOME_RETRYABLE,
                provider_code or f"SERVER_ERROR_{status}",
                provider_message or "Provider server error.",
                http_status=status,
                retry_after=retry_after,
            )
        if status in (401, 403):
            return self._failure(
                OUTCOME_AUTH_ERROR,
                "AUTHENTICATION_FAILED",
                "Provider rejected the configured credentials.",
                http_status=status,
            )
        if 400 <= status < 500:
            return self._failure(
                OUTCOME_INVALID_REQUEST,
                provider_code or "INVALID_REQUEST",
                provider_message or "Provider rejected the request.",
                http_status=status,
            )
        return self._failure(
            OUTCOME_UNKNOWN,
            f"UNEXPECTED_HTTP_STATUS_{status}",
            "Unexpected provider HTTP status; outcome is ambiguous.",
            http_status=status,
        )


class MockDeliveryProvider:
    """Deterministic simulated provider. Never touches the network."""

    name = PROVIDER_MOCK
    is_ready = True

    def __init__(self, *, max_attempts: int = DEFAULT_MAX_ATTEMPTS) -> None:
        self.max_attempts = max_attempts

    def send(
        self,
        recipient_phone: str,
        message_body: str,
        *,
        idempotency_key: str | None = None,
    ) -> ProviderResponse:
        recipient = normalize_recipient_phone(recipient_phone)
        if recipient is None:
            return ProviderResponse(
                outcome=OUTCOME_INVALID_REQUEST,
                error_code="INVALID_RECIPIENT",
                error_message="Recipient phone number is not plausible.",
                simulated=True,
            )
        digest = hashlib.sha256(
            f"{recipient}|{message_body}|{idempotency_key}".encode(
                "utf-8", errors="replace"
            )
        ).hexdigest()[:20]
        return ProviderResponse(
            outcome=OUTCOME_ACCEPTED,
            provider_message_id=f"mock-{digest}",
            http_status=None,
            simulated=True,
        )


def compute_idempotency_key(
    *,
    draft_id: int,
    approved_at: str | None,
    approved_text: str,
    approved_recipient_phone: str,
    provider: str,
) -> str:
    """Stable hash of the approved snapshot plus delivery intent."""
    payload = json.dumps(
        {
            "draft_id": draft_id,
            "approved_at": approved_at,
            "approved_text": approved_text,
            "recipient": normalize_recipient_phone(approved_recipient_phone),
            "provider": provider,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def backoff_seconds(attempt_count: int, retry_after: int | None) -> int:
    """Retry-After wins; otherwise capped exponential backoff (no scheduler)."""
    if retry_after is not None:
        return max(0, min(retry_after, 86_400))
    attempt = max(1, int(attempt_count))
    return min(900, 30 * (2 ** (attempt - 1)))


def _safe_exception_message(exc: Exception) -> str:
    return f"Provider call failed ({type(exc).__name__})"


def _dispatch_claimed(
    delivery_id: int,
    provider: DeliveryProvider,
    now: datetime,
) -> dict:
    delivery = db.get_message_delivery(delivery_id)
    if delivery is None:
        raise DeliveryBlockedError(f"delivery {delivery_id} not found")
    try:
        response = provider.send(
            delivery["recipient_phone_snapshot"],
            delivery["message_body_snapshot"],
            idempotency_key=delivery["idempotency_key"],
        )
    except Exception as exc:
        response = ProviderResponse(
            outcome=OUTCOME_UNKNOWN,
            error_code="PROVIDER_EXCEPTION",
            error_message=_safe_exception_message(exc),
        )

    if (
        response.outcome == OUTCOME_ACCEPTED
        and not (
            isinstance(response.provider_message_id, str)
            and response.provider_message_id.strip()
        )
    ):
        response = ProviderResponse(
            outcome=OUTCOME_UNKNOWN,
            http_status=response.http_status,
            error_code="MISSING_PROVIDER_MESSAGE_ID",
            error_message=(
                "Provider reported acceptance without a valid message ID."
            ),
        )

    if response.outcome == OUTCOME_ACCEPTED:
        db.finish_message_delivery_attempt(
            delivery_id,
            status="SENT",
            provider_message_id=response.provider_message_id.strip()[:200],
            http_status=response.http_status,
        )
    elif response.outcome == OUTCOME_RETRYABLE:
        delivery = db.get_message_delivery(delivery_id)
        attempts = delivery["attempt_count"]
        if attempts >= delivery["max_attempts"]:
            db.finish_message_delivery_attempt(
                delivery_id,
                status="FAILED",
                http_status=response.http_status,
                error_code="RETRIES_EXHAUSTED",
                error_message=(
                    f"Retries exhausted after {attempts} attempt(s): "
                    f"{response.error_code or 'provider error'}"
                ),
            )
        else:
            delay = backoff_seconds(
                attempts, response.retry_after_seconds
            )
            next_attempt = _format_utc(now + timedelta(seconds=delay))
            db.finish_message_delivery_attempt(
                delivery_id,
                status="RETRY_PENDING",
                http_status=response.http_status,
                error_code=response.error_code,
                error_message=response.error_message,
                next_attempt_at=next_attempt,
            )
    elif response.outcome in (
        OUTCOME_AUTH_ERROR,
        OUTCOME_INVALID_REQUEST,
        OUTCOME_CONFIG_ERROR,
    ):
        db.finish_message_delivery_attempt(
            delivery_id,
            status="FAILED",
            http_status=response.http_status,
            error_code=response.error_code,
            error_message=response.error_message,
        )
    else:
        db.finish_message_delivery_attempt(
            delivery_id,
            status="UNKNOWN",
            http_status=response.http_status,
            error_code=response.error_code or "AMBIGUOUS_OUTCOME",
            error_message=response.error_message
            or "Provider outcome could not be established.",
        )
    return db.get_message_delivery(delivery_id)


def _ensure_provider_ready(provider: DeliveryProvider) -> None:
    if not getattr(provider, "is_ready", False):
        config = getattr(provider, "config", None)
        problems = config.problems() if config is not None else []
        raise DeliveryConfigurationError(
            "WhatsApp delivery is not configured: "
            + (", ".join(problems) if problems else "provider not ready")
            + ". No message was sent."
        )


def deliver_approved_draft(
    draft_id: int,
    provider: DeliveryProvider,
    *,
    now: datetime | None = None,
) -> dict:
    """Create (once) and dispatch a delivery for an approved draft.

    Duplicate clicks reuse the same idempotency record. Re-dispatch happens
    only for a safe QUEUED record that was never claimed.
    """
    _ensure_provider_ready(provider)
    eligibility = db.get_delivery_eligibility(draft_id)
    if not eligibility["eligible"]:
        raise DeliveryBlockedError(
            "Delivery blocked: " + ", ".join(eligibility["problems"])
            + ". No message was sent."
        )
    draft = eligibility["draft"]
    key = compute_idempotency_key(
        draft_id=draft_id,
        approved_at=draft["approved_at"],
        approved_text=eligibility["approved_text"],
        approved_recipient_phone=eligibility["approved_recipient_phone"],
        provider=provider.name,
    )
    existing = db.get_message_delivery_by_idempotency_key(key)
    if existing is None:
        try:
            created = db.create_message_delivery(
                draft_id,
                provider=provider.name,
                idempotency_key=key,
                max_attempts=provider.max_attempts,
            )
        except ValueError as exc:
            raise DeliveryBlockedError(str(exc)) from exc
        delivery = created["delivery"]
    else:
        delivery = existing

    if delivery["status"] != "QUEUED":
        return delivery
    if not db.claim_message_delivery(delivery["id"]):
        return db.get_message_delivery(delivery["id"])
    return _dispatch_claimed(
        delivery["id"], provider, now or datetime.now(timezone.utc)
    )


def retry_message_delivery(
    delivery_id: int,
    provider: DeliveryProvider,
    *,
    now: datetime | None = None,
) -> dict:
    """Manually retry a RETRY_PENDING delivery, with revalidation."""
    _ensure_provider_ready(provider)
    delivery = db.get_message_delivery(delivery_id)
    if delivery is None:
        raise DeliveryBlockedError(f"delivery {delivery_id} not found")
    if delivery["status"] != "RETRY_PENDING":
        raise DeliveryBlockedError(
            f"delivery {delivery_id} is {delivery['status']}, not "
            "RETRY_PENDING; automatic retry is not allowed."
        )
    if delivery["provider"] != provider.name:
        raise DeliveryBlockedError(
            f"delivery {delivery_id} was created for provider "
            f"{delivery['provider']}, not {provider.name}; no message was "
            "sent."
        )
    eligibility = db.get_delivery_eligibility(
        delivery["followup_draft_id"]
    )
    if not eligibility["eligible"]:
        raise DeliveryBlockedError(
            "Retry blocked: " + ", ".join(eligibility["problems"])
            + ". No message was sent."
        )
    if (
        eligibility["approved_text"] != delivery["message_body_snapshot"]
        or normalize_recipient_phone(
            eligibility["approved_recipient_phone"]
        ) != normalize_recipient_phone(
            delivery["recipient_phone_snapshot"]
        )
    ):
        raise DeliveryBlockedError(
            "Retry blocked: the approved snapshot no longer matches this "
            "delivery record. No message was sent."
        )

    moment = now or datetime.now(timezone.utc)
    if not db.claim_message_delivery(
        delivery_id, now_text=_format_utc(moment)
    ):
        refreshed = db.get_message_delivery(delivery_id)
        if refreshed is not None and refreshed["status"] == "RETRY_PENDING":
            raise DeliveryBlockedError(
                "Retry is not due yet or attempts are exhausted. No message "
                "was sent."
            )
        if refreshed is not None:
            return refreshed
        raise DeliveryBlockedError(
            f"delivery {delivery_id} is no longer retryable."
        )
    return _dispatch_claimed(delivery_id, provider, moment)


def resolve_unknown(
    delivery_id: int,
    *,
    outcome: str,
    resolved_by: str,
    note: str,
    provider_message_id: str | None = None,
) -> None:
    """Operator-only manual resolution; delegates to persistence."""
    db.resolve_unknown_delivery(
        delivery_id,
        outcome=outcome,
        resolved_by=resolved_by,
        note=note,
        provider_message_id=provider_message_id,
    )


def webhook_support() -> dict:
    """Webhook delivery/read status updates are intentionally deferred."""
    return {
        "supported": False,
        "reason": (
            "No authenticated webhook receiver is deployed. SENT means Meta "
            "accepted the request; DELIVERED/READ are not available."
        ),
    }
