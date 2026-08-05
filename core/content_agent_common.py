"""Shared constants and bounded serialization for the ContentPilot agent team."""

from __future__ import annotations

import json
import math
import re
from typing import Any

from core.errors import ValidationAppError
from core.utils import parse_json_response
from core.validation import normalize_text

_AGENT_KEYS = ("ideator", "hook_script", "planner", "analyst", "dm_manager")
_AGENT_LABELS = {
    "ideator": "Ideator",
    "hook_script": "Hook & Script",
    "planner": "Planner",
    "analyst": "Analyst",
    "dm_manager": "DM Manager",
}
_AGENT_OUTPUT_KEYS = {
    "ideator": "ideas",
    "hook_script": "scripts",
    "planner": "calendar",
    "analyst": "opportunities",
    "dm_manager": "reply_playbooks",
}
_AGENT_INSTRUCTIONS = {
    "ideator": (
        "Find evidence-backed content opportunities. Return JSON with summary and ideas. "
        "Each idea should include title, angle, platform, format, and evidence."
    ),
    "hook_script": (
        "Turn the strongest opportunities into practical content. Return JSON with summary and scripts. "
        "Each script should include title, platform, hook, script, CTA, and source_evidence."
    ),
    "planner": (
        "Build a seven-day draft calendar. Return JSON with summary and calendar. "
        "Each item should include day, time, platform, content_type, title, objective, and source_idea."
    ),
    "analyst": (
        "Analyze the real profile and post metrics. Return JSON with summary, strengths, weaknesses, "
        "opportunities, risks, and metrics_to_watch. Never invent unavailable numbers."
    ),
    "dm_manager": (
        "Create human-reviewed engagement and DM playbooks. Return JSON with summary, reply_playbooks, "
        "engagement_actions, escalation_rules, and safety_notes. Do not claim that messages were sent."
    ),
}
_MAX_CONTEXT_CHARS = 100_000
_MAX_PROMPT_CHARS = 180_000
_MAX_OUTPUT_CHARS = 100_000
_MAX_AGENT_ITEMS = 30


def _json_load_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed if str(item).strip()]


def _json_dumps(value: Any, *, maximum: int = _MAX_OUTPUT_CHARS) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    if len(encoded) > maximum:
        raise ValidationAppError(
            "Generated agent output exceeded the safe storage limit."
        )
    return encoded


def _non_negative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_text(value: Any, *, field: str, maximum: int) -> str:
    raw = "" if value is None else str(value)
    return normalize_text(
        raw[:maximum],
        field=field,
        min_length=0,
        max_length=maximum,
    )


def _sanitize_json_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "[maximum depth reached]"
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return 0.0
        return max(min(value, 1_000_000_000.0), -1_000_000_000.0)
    if isinstance(value, str):
        return _safe_text(value, field="Agent output", maximum=4_000)
    if isinstance(value, list):
        return [
            _sanitize_json_value(item, depth=depth + 1)
            for item in value[:_MAX_AGENT_ITEMS]
        ]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:50]:
            key = re.sub(r"[^A-Za-z0-9_-]+", "_", str(raw_key).strip())[:64]
            if key:
                result[key] = _sanitize_json_value(
                    raw_value,
                    depth=depth + 1,
                )
        return result
    return _safe_text(str(value), field="Agent output", maximum=4_000)


def _validate_agent_payload(agent_key: str, raw_text: str) -> dict[str, Any]:
    parsed = parse_json_response(raw_text)
    if not isinstance(parsed, dict):
        summary = _safe_text(
            raw_text,
            field="Agent summary",
            maximum=4_000,
        )
        parsed = {
            "summary": summary or "Agent completed without structured output."
        }
    sanitized = _sanitize_json_value(parsed)
    if not isinstance(sanitized, dict):
        sanitized = {"summary": str(sanitized)}

    summary = sanitized.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        sanitized["summary"] = f"{_AGENT_LABELS[agent_key]} completed."
    required_list = _AGENT_OUTPUT_KEYS[agent_key]
    if required_list not in sanitized or not isinstance(
        sanitized[required_list], list
    ):
        sanitized[required_list] = []
    _json_dumps(sanitized)
    return sanitized
