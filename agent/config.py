"""Runtime configuration for the MILAAN WhatsApp agent.

Everything comes from environment variables (optionally loaded from a
`.env` file in the project root). Nothing here has a secret default.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing env vars win."""
    path = path or ROOT / ".env"
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if " #" in value and not value.startswith(("'", '"')):
            value = value.split(" #", 1)[0].strip()  # inline comment
        value = value.strip('"').strip("'")
        os.environ.setdefault(key, value)


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _phones(name: str) -> tuple[str, ...]:
    raw = os.environ.get(name, "")
    return tuple(normalize_phone(p) for p in raw.split(",") if p.strip())


def normalize_phone(value: str | None) -> str:
    """'whatsapp:+91 98765-43210' -> '+919876543210'."""
    if not value:
        return ""
    text = value.strip()
    if text.lower().startswith("whatsapp:"):
        text = text[len("whatsapp:"):]
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return ""
    if len(digits) == 10:  # bare Indian mobile number
        digits = "91" + digits
    return "+" + digits


@dataclass
class Settings:
    # LLM (any OpenAI-compatible endpoint: Gemini, OpenAI, Groq, OpenRouter)
    llm_api_key: str = ""
    llm_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    llm_model: str = "gemini-3.5-flash"
    llm_vision_model: str = ""
    llm_timeout_s: int = 60
    gemini_api_key: str = ""  # used for voice notes (native audio input)

    # Twilio WhatsApp
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_whatsapp_from: str = "whatsapp:+14155238886"  # sandbox number
    twilio_validate_signature: bool = True
    # Optional second Twilio account used ONLY for supplier messages (trial accounts
    # allow one active WhatsApp recipient, so the supplier phone can use its own trial).
    supplier_twilio_account_sid: str = ""
    supplier_twilio_auth_token: str = ""
    supplier_twilio_whatsapp_from: str = ""
    public_url: str = ""  # e.g. https://abcd.ngrok-free.app

    # WhatsApp provider: 'twilio' (default) or 'meta' (WhatsApp Cloud API, Meta test number).
    whatsapp_provider: str = "twilio"
    meta_access_token: str = ""
    meta_phone_number_id: str = ""
    meta_api_version: str = ""  # exactly as shown in the Meta app (e.g. in its sample curl); never guessed
    meta_app_secret: str = ""  # verifies X-Hub-Signature-256 on incoming webhooks
    meta_verify_token: str = ""  # any string you choose; typed into the Meta dashboard too

    # Roles
    trader_phones: tuple[str, ...] = field(default_factory=tuple)

    # Behaviour
    db_path: Path = ROOT / "agent_data" / "milaan_agent.db"
    media_dir: Path = ROOT / "agent_data" / "media"
    extract_double_read: bool = True
    max_chases: int = 3
    chase_interval_days: int = 7
    demo_commands: bool = True  # '#advance 7' time travel for the demo
    send_mode: str = "twilio"  # 'twilio' or 'console'

    @property
    def vision_model(self) -> str:
        return self.llm_vision_model or self.llm_model

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key)

    @property
    def supplier_twilio_enabled(self) -> bool:
        return bool(self.supplier_twilio_account_sid and self.supplier_twilio_auth_token
                    and self.supplier_twilio_whatsapp_from)

    @property
    def twilio_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token)

    @property
    def meta_enabled(self) -> bool:
        return bool(self.meta_access_token and self.meta_phone_number_id and self.meta_api_version)


def get_settings() -> Settings:
    load_dotenv()
    env = os.environ
    s = Settings()
    s.llm_api_key = env.get("LLM_API_KEY", "")
    s.llm_base_url = env.get("LLM_BASE_URL", s.llm_base_url)
    s.llm_model = env.get("LLM_MODEL", s.llm_model)
    s.llm_vision_model = env.get("LLM_VISION_MODEL", "")
    s.llm_timeout_s = int(env.get("LLM_TIMEOUT_S", s.llm_timeout_s))
    s.gemini_api_key = env.get("GEMINI_API_KEY", "") or (
        s.llm_api_key if "generativelanguage" in s.llm_base_url else ""
    )
    s.twilio_account_sid = env.get("TWILIO_ACCOUNT_SID", "")
    s.twilio_auth_token = env.get("TWILIO_AUTH_TOKEN", "")
    s.twilio_whatsapp_from = env.get("TWILIO_WHATSAPP_FROM", s.twilio_whatsapp_from)
    s.twilio_validate_signature = _bool("TWILIO_VALIDATE_SIGNATURE", True)
    s.supplier_twilio_account_sid = env.get("SUPPLIER_TWILIO_ACCOUNT_SID", "")
    s.supplier_twilio_auth_token = env.get("SUPPLIER_TWILIO_AUTH_TOKEN", "")
    s.supplier_twilio_whatsapp_from = env.get("SUPPLIER_TWILIO_WHATSAPP_FROM", "")
    s.public_url = env.get("PUBLIC_URL", "").rstrip("/")
    s.whatsapp_provider = (env.get("WHATSAPP_PROVIDER", "") or "twilio").strip().lower()
    s.meta_access_token = env.get("META_ACCESS_TOKEN", "").strip()
    s.meta_phone_number_id = env.get("META_PHONE_NUMBER_ID", "").strip()
    s.meta_api_version = env.get("META_API_VERSION", "").strip()
    s.meta_app_secret = env.get("META_APP_SECRET", "").strip()
    s.meta_verify_token = env.get("META_VERIFY_TOKEN", "").strip()
    s.trader_phones = _phones("TRADER_PHONES")
    if env.get("AGENT_DB_PATH"):
        s.db_path = Path(env["AGENT_DB_PATH"])
    if env.get("AGENT_MEDIA_DIR"):
        s.media_dir = Path(env["AGENT_MEDIA_DIR"])
    s.extract_double_read = _bool("EXTRACT_DOUBLE_READ", True)
    s.max_chases = int(env.get("MAX_CHASES", s.max_chases))
    s.chase_interval_days = int(env.get("CHASE_INTERVAL_DAYS", s.chase_interval_days))
    s.demo_commands = _bool("DEMO_COMMANDS", True)
    s.send_mode = env.get("SEND_MODE", s.send_mode)
    return s
