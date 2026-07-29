"""Global exception handling and user-facing error formatting."""

from __future__ import annotations

import os
import traceback
from typing import Any

from core.errors import (
    AIProviderError,
    AppError,
    ChatbotError,
    PublishingError,
    ValidationAppError,
)
from core.logging_config import get_logger, sanitize_log_message

logger = get_logger(__name__)

APP_ENV = os.getenv("APP_ENV", "development").lower()
APP_DEBUG = os.getenv("APP_DEBUG", "false").lower() in ("true", "1", "yes")
SHOW_TRACEBACK = APP_ENV == "development" and APP_DEBUG

_NON_RETRYABLE_CODES = frozenset(
    {
        "VALIDATION_ERROR",
        "AUTH_CONFIG_ERROR",
        "CONFIGURATION_ERROR",
        "UNSAFE_CONTENT",
        "FILE_UPLOAD_ERROR",
        "TRAINING_DATA_ERROR",
        "PROVIDER_UNAVAILABLE",
    }
)

_PUBLIC_REASON_CODES = frozenset(
    {
        "VALIDATION_ERROR",
        "AUTH_CONFIG_ERROR",
        "CONFIGURATION_ERROR",
        "UNSAFE_CONTENT",
        "FILE_UPLOAD_ERROR",
        "TRAINING_DATA_ERROR",
        "PROVIDER_UNAVAILABLE",
        "RATE_LIMIT",
        "LOAD_LIMIT",
    }
)


def is_retryable_error(error: BaseException) -> bool:
    """Return whether the error is safe to retry."""
    if isinstance(error, AppError):
        return error.retryable and error.error_code not in _NON_RETRYABLE_CODES

    msg = str(error).lower()
    name = type(error).__name__.lower()

    if any(k in msg for k in ("invalid api key", "authentication", "permission denied", "validation")):
        return False
    if any(k in msg for k in ("rate limit", "timeout", "locked", "connection", "503", "502", "504")):
        return True
    if "timeout" in name or "ratelimit" in name or "connection" in name:
        return True
    return isinstance(error, (ConnectionError, TimeoutError))


def _map_legacy_exception(error: BaseException) -> AppError:
    """Map legacy project exceptions to AppError."""
    from chatbot.chatbot_agent import ChatbotAgentError
    from core.agent import AgentValidationError
    from core.publishing import PublishError
    from providers.base import ProviderError, ProviderUnavailableError

    if isinstance(error, AppError):
        return error

    if isinstance(error, AgentValidationError):
        return ValidationAppError(
            getattr(error, "message", str(error)),
            original_exception=error,
        )

    if isinstance(error, PublishError):
        return PublishingError(
            getattr(error, "message", str(error)),
            original_exception=error,
        )

    if isinstance(error, ChatbotAgentError):
        return ChatbotError(
            getattr(error, "message", str(error)),
            original_exception=error,
        )

    if isinstance(error, ProviderUnavailableError):
        from core.errors import ProviderUnavailableAppError

        return ProviderUnavailableAppError(
            getattr(error, "message", str(error)),
            original_exception=error,
        )

    if isinstance(error, ProviderError):
        msg = getattr(error, "message", str(error))
        retryable = is_retryable_error(error)
        if "not configured" in msg.lower() or ("invalid" in msg.lower() and "key" in msg.lower()):
            retryable = False
        return AIProviderError(
            "The AI provider could not complete the request.",
            provider=getattr(error, "provider", ""),
            reason=msg,
            original_exception=error,
            retryable=retryable,
        )

    msg = str(error) or type(error).__name__
    return AppError(
        "An unexpected error occurred.",
        reason=sanitize_log_message(msg),
        error_code="UNEXPECTED_ERROR",
        original_exception=error,
        retryable=is_retryable_error(error),
    )


def _public_result(app_err: AppError) -> dict[str, Any]:
    result = app_err.to_dict()
    if APP_ENV not in {"development", "test"} and app_err.error_code not in _PUBLIC_REASON_CODES:
        result["reason"] = "The operation could not be completed safely. Internal details were logged."
        result["metadata"] = {}
    return result


def format_user_error(error: BaseException) -> dict[str, Any]:
    """Format an exception into a safe user-facing error dictionary."""
    app_err = _map_legacy_exception(error)
    result = _public_result(app_err)
    if SHOW_TRACEBACK:
        original = app_err.original_exception or error
        result["traceback"] = traceback.format_exception(
            type(original),
            original,
            original.__traceback__,
        )
    return result


def handle_exception(error: BaseException, context: str | None = None) -> dict[str, Any]:
    """Log, alert, and format an exception without allowing alert failures to escape."""
    app_err = _map_legacy_exception(error)
    log_exception(error, context=context, mapped_error=app_err)
    try:
        from core.alerts import notify_operational_alert

        notify_operational_alert(app_err, context=context)
    except Exception as alert_error:
        logger.warning("Operational alert dispatch failed: %s", type(alert_error).__name__)
    return _public_result(app_err)


def log_exception(
    error: BaseException,
    context: str | None = None,
    mapped_error: AppError | None = None,
) -> None:
    """Log an exception with sanitized details."""
    app_err = mapped_error or _map_legacy_exception(error)
    ctx = f" context={sanitize_log_message(context)}" if context else ""
    log_msg = (
        f"{app_err.error_code}{ctx}: {sanitize_log_message(app_err.message)} "
        f"| reason={sanitize_log_message(app_err.reason)}"
    )
    if app_err.original_exception:
        log_msg += f" | original={type(app_err.original_exception).__name__}"
    if SHOW_TRACEBACK:
        logger.error(log_msg, exc_info=app_err.original_exception or error)
    else:
        logger.error(log_msg)


def safe_error_message(error: BaseException) -> str:
    """Return a single safe string for display."""
    data = format_user_error(error)
    return data.get("message", "An unexpected error occurred.")
