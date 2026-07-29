"""Fail-closed startup checks for security-sensitive deployment configuration."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

from core.errors import ConfigurationError

_ALLOWED_ENVS = frozenset({"development", "test", "staging", "production"})
_ALLOWED_ACCESS_MODES = frozenset({"cloudflare_access", "vpn", "reverse_proxy", "private_network"})
_REDIRECT_KEYS = (
    "LINKEDIN_REDIRECT_URI",
    "X_REDIRECT_URI",
    "META_REDIRECT_URI",
)


def _truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _validate_redirect(name: str, value: str, production: bool) -> None:
    if not value:
        return
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigurationError(f"{name} must be a valid HTTP(S) URL.")
    if parsed.username or parsed.password:
        raise ConfigurationError(f"{name} cannot contain embedded credentials.")
    if production and parsed.scheme != "https":
        raise ConfigurationError(f"{name} must use HTTPS in production.")


def validate_startup_configuration() -> None:
    """Validate deployment settings before database or network services start."""
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    if app_env not in _ALLOWED_ENVS:
        raise ConfigurationError("APP_ENV must be development, test, staging, or production.")

    production = app_env == "production"
    if production and _truthy("APP_DEBUG"):
        raise ConfigurationError("APP_DEBUG must be false in production.")

    database_url = os.getenv("DATABASE_URL", "sqlite:///data/contentpilot.db").strip()
    if not database_url:
        raise ConfigurationError("DATABASE_URL is required.")
    if production and database_url.lower().startswith("sqlite"):
        raise ConfigurationError("SQLite is not allowed for production deployments.")

    if production:
        access_mode = os.getenv("ACCESS_CONTROL_MODE", "").strip().lower()
        if access_mode not in _ALLOWED_ACCESS_MODES:
            raise ConfigurationError(
                "Production requires ACCESS_CONTROL_MODE to be cloudflare_access, vpn, "
                "reverse_proxy, or private_network."
            )
        if not _truthy("HTTPS_TERMINATION_ENABLED"):
            raise ConfigurationError("HTTPS_TERMINATION_ENABLED must be true in production.")

    if _truthy("ALERTS_ENABLED"):
        if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
            raise ConfigurationError("TELEGRAM_BOT_TOKEN is required when alerts are enabled.")
        alert_ids = os.getenv("TELEGRAM_ALERT_CHAT_IDS") or os.getenv("TELEGRAM_ADMIN_IDS", "")
        if not alert_ids.strip():
            raise ConfigurationError("At least one Telegram alert chat ID is required.")

    for key in _REDIRECT_KEYS:
        _validate_redirect(key, os.getenv(key, "").strip(), production)

    timeout_fields = {
        "DEFAULT_API_TIMEOUT_SECONDS": (1, 300),
        "LONG_TASK_TIMEOUT_SECONDS": (5, 1_800),
        "DATABASE_TIMEOUT_SECONDS": (1, 300),
        "ALERT_COOLDOWN_SECONDS": (60, 86_400),
    }
    for key, (minimum, maximum) in timeout_fields.items():
        raw = os.getenv(key, "").strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigurationError(f"{key} must be an integer.") from exc
        if not minimum <= value <= maximum:
            raise ConfigurationError(f"{key} must be between {minimum} and {maximum}.")
