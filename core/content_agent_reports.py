"""Safe Telegram delivery and report formatting for the ContentPilot agent team."""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from core.errors import ConfigurationError, ExternalAPIError, ValidationAppError

_TELEGRAM_TOKEN_RE = re.compile(r"^[0-9]{5,20}:[A-Za-z0-9_-]{20,128}$")
_MAX_TELEGRAM_TEXT = 3_800


def report_chat_ids_from_env() -> tuple[str, ...]:
    raw = (
        os.getenv("TELEGRAM_REPORT_CHAT_IDS")
        or os.getenv("TELEGRAM_ALERT_CHAT_IDS")
        or os.getenv("TELEGRAM_ADMIN_IDS", "")
    )
    result: list[str] = []
    for item in raw.split(","):
        value = item.strip()
        if not value:
            continue
        if not value.lstrip("-").isdigit() or len(value) > 24:
            raise ConfigurationError(
                "Telegram report chat IDs must be comma-separated numeric IDs."
            )
        if value not in result:
            result.append(value)
    return tuple(result)


def _validated_token() -> str:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ConfigurationError(
            "TELEGRAM_BOT_TOKEN is required for content agent reports."
        )
    if not _TELEGRAM_TOKEN_RE.fullmatch(token):
        raise ConfigurationError(
            "TELEGRAM_BOT_TOKEN has an invalid format."
        )
    return token


def _safe_line(value: Any, *, maximum: int = 500) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split())
    return text[:maximum]


def _agent_summary(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "No result"
    return _safe_line(payload.get("summary") or "Completed", maximum=350)


def build_content_agent_report(
    *,
    cycle_id: str,
    own_handle: str,
    sync_summary: dict[str, Any] | None,
    agent_results: dict[str, dict[str, Any]],
    failed_agents: dict[str, str],
) -> str:
    labels = {
        "ideator": "Ideator",
        "hook_script": "Hook & Script",
        "planner": "Planner",
        "analyst": "Analyst",
        "dm_manager": "DM Manager",
    }
    lines = [
        "Artixcore ContentPilot Agent Team",
        "",
        f"Instagram: @{_safe_line(own_handle, maximum=30)}",
        f"Cycle: {_safe_line(cycle_id, maximum=64)}",
    ]
    if sync_summary:
        lines.extend(
            [
                f"Profiles synced: {int(sync_summary.get('profiles', 0))}",
                f"Posts imported/updated: {int(sync_summary.get('posts_upserted', 0))}",
            ]
        )
    lines.append("")

    for key, label in labels.items():
        if key in failed_agents:
            lines.append(
                f"{label}: FAILED - {_safe_line(failed_agents[key], maximum=220)}"
            )
        else:
            lines.append(
                f"{label}: {_agent_summary(agent_results.get(key))}"
            )

    lines.extend(
        [
            "",
            "All generated material remains draft-only and requires human review before publishing.",
        ]
    )
    return "\n".join(lines)[:_MAX_TELEGRAM_TEXT]


def send_content_agent_report(
    text: str,
    chat_ids: tuple[str, ...] | None = None,
) -> int:
    message = str(text or "").strip()
    if not message:
        raise ValidationAppError("Telegram report cannot be empty.")
    if len(message) > _MAX_TELEGRAM_TEXT:
        message = message[:_MAX_TELEGRAM_TEXT]

    token = _validated_token()
    recipients = chat_ids or report_chat_ids_from_env()
    if not recipients:
        raise ConfigurationError(
            "At least one TELEGRAM_REPORT_CHAT_IDS, TELEGRAM_ALERT_CHAT_IDS, or TELEGRAM_ADMIN_IDS value is required."
        )

    endpoint = f"https://api.telegram.org/bot{token}/sendMessage"
    delivered = 0
    try:
        with httpx.Client(
            timeout=httpx.Timeout(10.0, connect=5.0),
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "Artixcore-ContentPilot/1.0"},
        ) as client:
            failure_statuses: list[int] = []
            for chat_id in recipients:
                response = client.post(
                    endpoint,
                    json={
                        "chat_id": chat_id,
                        "text": message,
                        "disable_web_page_preview": True,
                    },
                )
                if response.is_success:
                    delivered += 1
                    continue
                if response.status_code == 401:
                    raise ConfigurationError(
                        "Telegram rejected the configured bot token."
                    )
                failure_statuses.append(response.status_code)

            if delivered == 0 and failure_statuses:
                retryable = any(
                    status == 429 or status >= 500
                    for status in failure_statuses
                )
                raise ExternalAPIError(
                    "Telegram report delivery failed for every configured recipient.",
                    service="telegram",
                    reason=(
                        "Telegram returned one or more non-success responses. "
                        "Recipient details were not logged."
                    ),
                    retryable=retryable,
                )
    except (ConfigurationError, ExternalAPIError):
        raise
    except httpx.TimeoutException as exc:
        raise ExternalAPIError(
            "Telegram report delivery timed out.",
            service="telegram",
            reason="Telegram did not respond within the configured timeout.",
            original_exception=exc,
            retryable=True,
        ) from exc
    except httpx.HTTPError as exc:
        raise ExternalAPIError(
            "Telegram report delivery could not connect.",
            service="telegram",
            reason=f"Network client error: {type(exc).__name__}",
            original_exception=exc,
            retryable=True,
        ) from exc

    return delivered


def artifact_json_preview(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {"summary": "Stored artifact could not be decoded."}
    return parsed if isinstance(parsed, dict) else {"result": parsed}
