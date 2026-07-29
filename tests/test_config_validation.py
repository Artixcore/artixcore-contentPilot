"""Tests for fail-closed deployment configuration validation."""

import pytest

from core.config_validation import validate_startup_configuration
from core.errors import ConfigurationError


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
    ):
        monkeypatch.delenv(key, raising=False)


def test_development_defaults_are_allowed(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("APP_DEBUG", "false")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    validate_startup_configuration()


def test_production_rejects_sqlite(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_DEBUG", "false")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///data/contentpilot.db")
    monkeypatch.setenv("ACCESS_CONTROL_MODE", "cloudflare_access")
    monkeypatch.setenv("HTTPS_TERMINATION_ENABLED", "true")

    with pytest.raises(ConfigurationError):
        validate_startup_configuration()


def test_production_rejects_debug_mode(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_DEBUG", "true")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db/contentpilot")
    monkeypatch.setenv("ACCESS_CONTROL_MODE", "cloudflare_access")
    monkeypatch.setenv("HTTPS_TERMINATION_ENABLED", "true")

    with pytest.raises(ConfigurationError):
        validate_startup_configuration()


def test_production_requires_access_control_and_https(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_DEBUG", "false")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db/contentpilot")
    monkeypatch.delenv("ACCESS_CONTROL_MODE", raising=False)
    monkeypatch.setenv("HTTPS_TERMINATION_ENABLED", "false")

    with pytest.raises(ConfigurationError):
        validate_startup_configuration()


def test_production_rejects_insecure_oauth_redirect(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_DEBUG", "false")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db/contentpilot")
    monkeypatch.setenv("ACCESS_CONTROL_MODE", "reverse_proxy")
    monkeypatch.setenv("HTTPS_TERMINATION_ENABLED", "true")
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


def test_valid_production_configuration_passes(monkeypatch):
    _clear_optional_security_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_DEBUG", "false")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@db/contentpilot")
    monkeypatch.setenv("ACCESS_CONTROL_MODE", "cloudflare_access")
    monkeypatch.setenv("HTTPS_TERMINATION_ENABLED", "true")
    monkeypatch.setenv("LINKEDIN_REDIRECT_URI", "https://contentpilot.example.com/linkedin/callback")
    validate_startup_configuration()
