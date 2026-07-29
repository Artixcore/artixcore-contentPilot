"""Tests for fail-closed deployment configuration validation."""

import pytest

from core.config_validation import validate_startup_configuration
from core.errors import ConfigurationError

_TEST_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
_VALID_POSTGRES = (
    "postgresql+psycopg://user:pass@db.example.com:5432/contentpilot?sslmode=require"
)


def _clear_optional_security_env(monkeypatch):
    for key in (
        "LINKEDIN_REDIRECT_URI",
        "X_REDIRECT_URI",
        "META_REDIRECT_URI",
        "ALERTS_ENABLED",
        "TELEGRAM_ALERT_CHAT_IDS",
        "TELEGRAM_ADMIN_IDS",
        "TELEGRAM_BOT_TOKEN",
        "ALERT_COOLDOWN_SECONDS",
        "ACCESS_CONTROL_MODE",
        "HTTPS_TERMINATION_ENABLED",
        "TRUST_PROXY_HEADERS",
        "BOOTSTRAP_ADMIN_EMAIL",
        "BOOTSTRAP_ADMIN_PASSWORD",
        "BOOTSTRAP_ADMIN_PASSWORD_HASH",
    ):
        monkeypatch.delenv(key, raising=False)


def _set_valid_production(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_DEBUG", "false")
    monkeypatch.setenv("DATABASE_URL", _VALID_POSTGRES)
    monkeypatch.setenv("ACCESS_CONTROL_MODE", "cloudflare_access")
    monkeypatch.setenv("HTTPS_TERMINATION_ENABLED", "true")
    monkeypatch.setenv("CONTENTPILOT_ENCRYPTION_KEYS", _TEST_FERNET_KEY)


def test_development_defaults_are_allowed(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_DEBUG", "false")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    validate_startup_configuration()


def test_production_rejects_sqlite(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    _set_valid_production(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///data/contentpilot.db")
    with pytest.raises(ConfigurationError):
        validate_startup_configuration()


def test_production_rejects_debug_mode(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    _set_valid_production(monkeypatch)
    monkeypatch.setenv("APP_DEBUG", "true")
    with pytest.raises(ConfigurationError):
        validate_startup_configuration()


def test_production_requires_access_control_and_https(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    _set_valid_production(monkeypatch)
    monkeypatch.delenv("ACCESS_CONTROL_MODE", raising=False)
    monkeypatch.setenv("HTTPS_TERMINATION_ENABLED", "false")
    with pytest.raises(ConfigurationError):
        validate_startup_configuration()


def test_production_rejects_insecure_oauth_redirect(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    _set_valid_production(monkeypatch)
    monkeypatch.setenv("ACCESS_CONTROL_MODE", "reverse_proxy")
    monkeypatch.setenv("LINKEDIN_REDIRECT_URI", "http://contentpilot.example.com/callback")
    with pytest.raises(ConfigurationError):
        validate_startup_configuration()


def test_alerts_require_token_and_destination(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("ALERTS_ENABLED", "true")
    with pytest.raises(ConfigurationError):
        validate_startup_configuration()


def test_production_requires_encryption_key(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    _set_valid_production(monkeypatch)
    monkeypatch.delenv("CONTENTPILOT_ENCRYPTION_KEYS", raising=False)
    with pytest.raises(ConfigurationError, match="CONTENTPILOT_ENCRYPTION_KEYS"):
        validate_startup_configuration()


def test_production_requires_database_tls(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    _set_valid_production(monkeypatch)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+psycopg://user:pass@db.example.com:5432/contentpilot",
    )
    with pytest.raises(ConfigurationError, match="sslmode"):
        validate_startup_configuration()


def test_bootstrap_credentials_must_be_paired(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "owner@example.com")
    with pytest.raises(ConfigurationError, match="BOOTSTRAP_ADMIN_EMAIL"):
        validate_startup_configuration()


def test_valid_production_configuration_passes(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    _set_valid_production(monkeypatch)
    monkeypatch.setenv(
        "LINKEDIN_REDIRECT_URI",
        "https://contentpilot.example.com/linkedin/callback",
    )
    validate_startup_configuration()
