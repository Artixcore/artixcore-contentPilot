"""Best-effort operational alerts with secret redaction and duplicate suppression."""

from __future__ import annotations

import hashlib
import os
import threading
import time

import httpx

from core.errors import AppError
from core.logging_config import get_logger, sanitize_log_message

logger = get_logger(__name__)

_LOCK = threading.Lock()
_LAST_SENT: dict[str, float] = {}
_DEFAULT_ALERT_CODES = frozenset(
    {
        "AI_PROVIDER_ERROR",
        "DATABASE_ERROR",
        "EXTERNAL_API_ERROR",
        "PUBLISHING_ERROR",
        "TELEGRAM_ERROR",
        "TIMEOUT",
        "UNEXPECTED_ERROR",
    }
)


def _enabled() -> bool:
    return os.getenv("ALERTS_ENABLED", "false").strip().lower() in {"1", "true", "yes"}


def _alert_codes() -> frozenset[str]:
    raw = os.getenv("ALERT_ERROR_CODES", "").strip()
    if not raw:
        return _DEFAULT_ALERT_CODES
    return frozenset(part.strip().upper() for part in raw.split(",") if part.strip())


def _admin_ids() -> tuple[str, ...]:
    raw = os.getenv("TELEGRAM_ALERT_CHAT_IDS") or os.getenv("TELEGRAM_ADMIN_IDS", "")
    ids = []
    for item in raw.split(","):
        value = item.strip()
        if value and value.lstrip("-").isdigit() and len(value) <= 24:
            ids.append(value)
    return tuple(dict.fromkeys(ids))


def _should_send(fingerprint: str) -> bool:
    try:
        cooldown = max(60, min(int(os.getenv("ALERT_COOLDOWN_SECONDS", "300")), 86_400))
    except ValueError:
        cooldown = 300

    now = time.monotonic()
    with _LOCK:
        previous = _LAST_SENT.get(fingerprint, 0.0)
        if now - previous < cooldown:
            return False
        _LAST_SENT[fingerprint] = now
        if len(_LAST_SENT) > 1_000:
            cutoff = now - cooldown
            stale = [key for key, sent_at in _LAST_SENT.items() if sent_at < cutoff]
            for key in stale:
                _LAST_SENT.pop(key, None)
    return True


def notify_operational_alert(error: AppError, *, context: str | None = None) -> bool:
    """Send a sanitized Telegram alert. Failures never escape to application code."""
    if not _enabled() or error.error_code.upper() not in _alert_codes():
        return False

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = _admin_ids()
    if not token or not chat_ids:
        return False

    safe_context = sanitize_log_message(context or "application")[:120]
    safe_message = sanitize_log_message(error.message)[:300]
    safe_reason = sanitize_log_message(error.reason)[:500]
    fingerprint = hashlib.sha256(
        f"{error.error_code}|{safe_context}|{safe_message}|{safe_reason}".encode("utf-8")
    ).hexdigest()
    if not _should_send(fingerprint):
        return False

    text = (
        "ContentPilot operational alert\n\n"
        f"Code: {error.error_code}\n"
        f"Context: {safe_context}\n"
        f"Message: {safe_message}\n"
        f"Reason: {safe_reason}\n"
        f"Retryable: {'yes' if error.retryable else 'no'}"
    )
    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    delivered = False

    try:
        with httpx.Client(timeout=httpx.Timeout(5.0), follow_redirects=False, trust_env=False) as client:
            for chat_id in chat_ids:
                response = client.post(
                    endpoint,
                    json={
                        "chat_id": chat_id,
                        "text": text,
                        "disable_web_page_preview": True,
                    },
                )
                if response.is_success:
                    delivered = True
                else:
                    logger.warning("Operational alert delivery failed with status=%s", response.status_code)
    except Exception as exc:
        logger.warning("Operational alert delivery failed: %s", type(exc).__name__)
    return delivered
