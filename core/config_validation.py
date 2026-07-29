"""Fail-closed startup checks for security-sensitive deployment configuration."""

from __future__ import annotations

import os
from urllib.parse import parse_qs, urlsplit

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


def _validate_database(database_url: str, production: bool) -> None:
    if not database_url:
        raise ConfigurationError("DATABASE_URL is required.")
    if not production:
        return

    parsed = urlsplit(database_url)
    if not parsed.scheme.startswith("postgresql"):
        raise ConfigurationError("Production DATABASE_URL must use PostgreSQL.")
    if not parsed.hostname or not parsed.path.strip("/"):
        raise ConfigurationError("Production DATABASE_URL must include a host and database name.")

    sslmode = (parse_qs(parsed.query).get("sslmode") or [""])[0].lower()
    if sslmode not in {"require", "verify-ca", "verify-full"}:
        raise ConfigurationError(
            "Production PostgreSQL must use sslmode=require, verify-ca, or verify-full."
        )


def _validate_integer(name: str, minimum: int, maximum: int) -> None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be between {minimum} and {maximum}.")


def _validate_encryption(production: bool) -> None:
    configured = bool(os.getenv("CONTENTPILOT_ENCRYPTION_KEYS", "").strip())
    if production and not configured:
        raise ConfigurationError("CONTENTPILOT_ENCRYPTION_KEYS is required in production.")
    if configured:
        from core.encryption import active_key_id

        active_key_id()


def _validate_api_key_pepper(production: bool) -> None:
    pepper = os.getenv("WORKSPACE_API_KEY_PEPPER", "").strip()
    if production and len(pepper) < 32:
        raise ConfigurationError(
            "WORKSPACE_API_KEY_PEPPER must contain at least 32 characters in production."
        )
    if pepper and len(pepper) < 32:
        raise ConfigurationError("WORKSPACE_API_KEY_PEPPER must contain at least 32 characters.")


def _validate_bootstrap_pair() -> None:
    email = os.getenv("BOOTSTRAP_ADMIN_EMAIL", "").strip()
    password = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    password_hash = os.getenv("BOOTSTRAP_ADMIN_PASSWORD_HASH", "").strip()
    if email and not (password or password_hash):
        raise ConfigurationError(
            "BOOTSTRAP_ADMIN_EMAIL requires BOOTSTRAP_ADMIN_PASSWORD or BOOTSTRAP_ADMIN_PASSWORD_HASH."
        )
    if (password or password_hash) and not email:
        raise ConfigurationError("BOOTSTRAP_ADMIN_EMAIL is required with bootstrap credentials.")
    if password and password_hash:
        raise ConfigurationError(
            "Set only one of BOOTSTRAP_ADMIN_PASSWORD or BOOTSTRAP_ADMIN_PASSWORD_HASH."
        )


def validate_startup_configuration() -> None:
    """Validate deployment settings before database or network services start."""
    app_env = os.getenv("APP_ENV", "development").strip().lower()
    if app_env not in _ALLOWED_ENVS:
        raise ConfigurationError("APP_ENV must be development, test, staging, or production.")

    production = app_env == "production"
    if production and _truthy("APP_DEBUG"):
        raise ConfigurationError("APP_DEBUG must be false in production.")

    database_url = os.getenv("DATABASE_URL", "sqlite:///data/contentpilot.db").strip()
    _validate_database(database_url, production)
    _validate_encryption(production)
    _validate_api_key_pepper(production)
    _validate_bootstrap_pair()

    if production:
        access_mode = os.getenv("ACCESS_CONTROL_MODE", "").strip().lower()
        if access_mode not in _ALLOWED_ACCESS_MODES:
            raise ConfigurationError(
                "Production requires ACCESS_CONTROL_MODE to be cloudflare_access, vpn, "
                "reverse_proxy, or private_network."
            )
        if not _truthy("HTTPS_TERMINATION_ENABLED"):
            raise ConfigurationError("HTTPS_TERMINATION_ENABLED must be true in production.")
        if _truthy("TRUST_PROXY_HEADERS") and access_mode not in {
            "cloudflare_access",
            "reverse_proxy",
        }:
            raise ConfigurationError(
                "TRUST_PROXY_HEADERS may only be enabled behind a trusted reverse proxy."
            )

    if _truthy("ALERTS_ENABLED"):
        if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
            raise ConfigurationError("TELEGRAM_BOT_TOKEN is required when alerts are enabled.")
        alert_ids = os.getenv("TELEGRAM_ALERT_CHAT_IDS") or os.getenv("TELEGRAM_ADMIN_IDS", "")
        if not alert_ids.strip():
            raise ConfigurationError("At least one Telegram alert chat ID is required.")

    for key in _REDIRECT_KEYS:
        _validate_redirect(key, os.getenv(key, "").strip(), production)

    integer_fields = {
        "DEFAULT_API_TIMEOUT_SECONDS": (1, 300),
        "LONG_TASK_TIMEOUT_SECONDS": (5, 1_800),
        "DATABASE_TIMEOUT_SECONDS": (1, 300),
        "DATABASE_POOL_SIZE": (1, 50),
        "DATABASE_MAX_OVERFLOW": (0, 100),
        "DATABASE_POOL_RECYCLE_SECONDS": (60, 86_400),
        "ALERT_COOLDOWN_SECONDS": (60, 86_400),
        "AUTH_SESSION_HOURS": (1, 168),
        "AUTH_MAX_FAILED_LOGINS": (3, 20),
        "AUTH_LOCK_MINUTES": (5, 1_440),
        "WORKER_POLL_SECONDS": (1, 60),
        "WORKER_IDLE_SECONDS": (1, 300),
    }
    for key, (minimum, maximum) in integer_fields.items():
        _validate_integer(key, minimum, maximum)
