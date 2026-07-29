"""Tests for core.error_handler."""

import importlib

from core.errors import AppError, RateLimitError, ValidationAppError
from core.utils import sanitize_text


def _reload_error_handler(monkeypatch, *, app_env: str, debug: str = "false"):
    monkeypatch.setenv("APP_ENV", app_env)
    monkeypatch.setenv("APP_DEBUG", debug)
    import core.error_handler as error_handler

    return importlib.reload(error_handler)


def test_formats_public_validation_error(monkeypatch):
    error_handler = _reload_error_handler(monkeypatch, app_env="test")
    err = ValidationAppError("Invalid value", reason="The field is invalid.")
    result = error_handler.format_user_error(err)

    assert result["success"] is False
    assert result["message"] == "Invalid value"
    assert result["reason"] == "The field is invalid."
    assert result["error_code"] == "VALIDATION_ERROR"


def test_sanitizes_secrets():
    raw = "Bearer sk-1234567890abcdef and Authorization: Bearer secret-token"
    cleaned = sanitize_text(raw)
    assert "sk-1234567890abcdef" not in cleaned
    assert "secret-token" not in cleaned


def test_hides_traceback_and_internal_reason_in_production(monkeypatch):
    error_handler = _reload_error_handler(monkeypatch, app_env="production", debug="false")
    result = error_handler.format_user_error(ValueError("database password=secret-value"))

    assert "traceback" not in result
    assert "secret-value" not in result["reason"]
    assert result["metadata"] == {}
    assert result["error_code"] == "UNEXPECTED_ERROR"


def test_debug_flag_does_not_enable_traceback_outside_development(monkeypatch):
    error_handler = _reload_error_handler(monkeypatch, app_env="production", debug="true")
    result = error_handler.format_user_error(ValueError("boom"))
    assert "traceback" not in result


def test_development_requires_explicit_debug_for_traceback(monkeypatch):
    error_handler = _reload_error_handler(monkeypatch, app_env="development", debug="false")
    assert "traceback" not in error_handler.format_user_error(ValueError("boom"))

    error_handler = _reload_error_handler(monkeypatch, app_env="development", debug="true")
    assert "traceback" in error_handler.format_user_error(ValueError("boom"))


def test_marks_retryable_errors(monkeypatch):
    error_handler = _reload_error_handler(monkeypatch, app_env="test")
    assert error_handler.is_retryable_error(RateLimitError()) is True
    assert error_handler.is_retryable_error(ValidationAppError("bad input")) is False


def test_handle_exception_structure(monkeypatch):
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    error_handler = _reload_error_handler(monkeypatch, app_env="test")
    result = error_handler.handle_exception(ValidationAppError("Invalid"), context="test")

    assert result["success"] is False
    assert result["retryable"] is False
    assert result["message"] == "Invalid"


def test_safe_error_message(monkeypatch):
    error_handler = _reload_error_handler(monkeypatch, app_env="test")
    assert error_handler.safe_error_message(AppError("User message")) == "User message"


def test_legacy_publish_error_mapping_does_not_raise_name_error(monkeypatch):
    error_handler = _reload_error_handler(monkeypatch, app_env="test")
    from core.publishing import PublishError

    result = error_handler.format_user_error(PublishError("Publish failed"))
    assert result["error_code"] == "PUBLISHING_ERROR"


def test_legacy_chatbot_error_mapping_does_not_raise_name_error(monkeypatch):
    error_handler = _reload_error_handler(monkeypatch, app_env="test")
    from chatbot.chatbot_agent import ChatbotAgentError

    result = error_handler.format_user_error(ChatbotAgentError("Chat failed"))
    assert result["error_code"] == "CHATBOT_ERROR"
