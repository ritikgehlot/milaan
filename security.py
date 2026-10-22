"""Runtime safety settings for MILAAN deployments.

Values come only from process environment. No credentials are read from
``.env`` files and no secrets are written to logs or the database.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}
_PRODUCTION_ENVS = {"production", "prod"}


def _bool(name: str, value: str | None, *, default: bool) -> bool:
    text = (value or "").strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"{name} must be true or false")


@dataclass(frozen=True)
class SecurityConfig:
    environment: str
    auth_required: bool
    allowed_email_domains: tuple[str, ...]

    @property
    def production(self) -> bool:
        return self.environment in _PRODUCTION_ENVS


def load_security_config(
    environ: Mapping[str, str] | None = None,
) -> SecurityConfig:
    """Load deployment policy; production requires explicit OIDC sign-in."""
    env = os.environ if environ is None else environ
    environment = (env.get("APP_ENV") or "development").strip().lower()
    if not environment:
        raise ValueError("APP_ENV must not be empty")
    default_auth = environment in _PRODUCTION_ENVS
    auth_required = _bool(
        "MILAAN_AUTH_REQUIRED", env.get("MILAAN_AUTH_REQUIRED"),
        default=default_auth,
    ) if env.get("MILAAN_AUTH_REQUIRED") is not None else default_auth
    domains = tuple(
        domain.strip().lower().lstrip("@")
        for domain in (env.get("MILAAN_ALLOWED_EMAIL_DOMAINS") or "").split(",")
        if domain.strip()
    )
    return SecurityConfig(environment, auth_required, domains)


def email_is_allowed(email: str | None, domains: tuple[str, ...]) -> bool:
    """Allow every verified identity when no domain policy is configured."""
    normalized = (email or "").strip().lower()
    if not normalized or "@" not in normalized:
        return False
    return not domains or normalized.rsplit("@", 1)[1] in domains
