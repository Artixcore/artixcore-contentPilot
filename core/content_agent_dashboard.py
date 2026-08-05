"""Dashboard projections, security findings, and scheduling checks for the content agent team."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from core.apify_instagram import (
    normalize_competitor_handles,
    validate_actor_id,
    validate_instagram_handle,
    validate_posts_per_profile,
)
from core.content_agent_common import (
    _AGENT_KEYS,
    _AGENT_LABELS,
    _json_load_list,
)
from core.content_agent_sync import get_content_agent_settings
from core.content_intelligence_models import (
    ContentAgentRun,
    ContentAgentSettings,
    SocialPostSnapshot,
    SocialProfileSnapshot,
)
from core.errors import ConfigurationError, ValidationAppError


def get_content_agent_dashboard(session: Session) -> dict[str, Any]:
    settings = get_content_agent_settings(session)
    profile_count = session.execute(
        select(func.count(SocialProfileSnapshot.id)).where(
            SocialProfileSnapshot.is_active.is_(True)
        )
    ).scalar_one()
    post_count = session.execute(
        select(func.count(SocialPostSnapshot.id))
        .join(
            SocialProfileSnapshot,
            SocialProfileSnapshot.id == SocialPostSnapshot.profile_id,
        )
        .where(SocialProfileSnapshot.is_active.is_(True))
    ).scalar_one()
    latest_runs = session.execute(
        select(ContentAgentRun)
        .order_by(
            desc(ContentAgentRun.started_at),
            desc(ContentAgentRun.id),
        )
        .limit(50)
    ).scalars().all()

    latest_by_agent: dict[str, ContentAgentRun] = {}
    latest_cycle_id = latest_runs[0].cycle_id if latest_runs else None
    for run in latest_runs:
        if run.cycle_id != latest_cycle_id:
            continue
        if run.agent_key not in latest_by_agent:
            latest_by_agent[run.agent_key] = run

    top_rows = session.execute(
        select(SocialPostSnapshot, SocialProfileSnapshot)
        .join(
            SocialProfileSnapshot,
            SocialProfileSnapshot.id == SocialPostSnapshot.profile_id,
        )
        .where(SocialProfileSnapshot.is_active.is_(True))
        .order_by(
            desc(SocialPostSnapshot.engagement_rate),
            desc(SocialPostSnapshot.likes_count),
        )
        .limit(20)
    ).all()
    top_posts = [
        {
            "handle": profile.handle,
            "owned": bool(profile.is_owned),
            "type": post.content_type,
            "caption": post.caption[:240],
            "likes": post.likes_count,
            "comments": post.comments_count,
            "shares": post.shares_count,
            "views": post.views_count,
            "engagement_rate": round(
                float(post.engagement_rate or 0.0),
                4,
            ),
            "published_at": (
                post.published_at.isoformat()
                if post.published_at
                else None
            ),
            "permalink": post.permalink,
        }
        for post, profile in top_rows
    ]

    agent_states: dict[str, dict[str, Any]] = {}
    for key in _AGENT_KEYS:
        run = latest_by_agent.get(key)
        payload: dict[str, Any] = {}
        if run and run.output_json:
            try:
                parsed = json.loads(run.output_json)
                if isinstance(parsed, dict):
                    payload = parsed
            except json.JSONDecodeError:
                payload = {
                    "summary": "Stored output is invalid JSON."
                }
        agent_states[key] = {
            "label": _AGENT_LABELS[key],
            "status": run.status if run else "not_run",
            "started_at": run.started_at.isoformat() if run else None,
            "finished_at": (
                run.finished_at.isoformat()
                if run and run.finished_at
                else None
            ),
            "provider": run.provider_used if run else None,
            "model": run.model_used if run else None,
            "error_code": run.error_code if run else None,
            "error_message": run.error_message if run else None,
            "payload": payload,
        }

    return {
        "settings": settings,
        "profile_count": int(profile_count or 0),
        "post_count": int(post_count or 0),
        "latest_cycle_id": latest_cycle_id,
        "agents": agent_states,
        "top_posts": top_posts,
    }


def content_agent_security_findings(
    settings: ContentAgentSettings | None,
    *,
    provider_available: bool,
) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    if settings is None:
        return [
            {
                "severity": "warning",
                "title": "Agent team not configured",
                "message": (
                    "Add your Instagram handle and competitor handles before running ingestion."
                ),
                "action": "Save Content Agent Team settings.",
            }
        ]

    try:
        validate_actor_id(settings.apify_actor_id)
    except (ValidationAppError, ConfigurationError):
        findings.append(
            {
                "severity": "critical",
                "title": "Unsafe Apify actor ID",
                "message": (
                    "The configured actor ID failed the allowlist validation."
                ),
                "action": (
                    "Use an actor name or owner/actor pair with safe characters."
                ),
            }
        )
    try:
        own_handle = validate_instagram_handle(
            settings.own_instagram_handle,
            field="Your Instagram handle",
        )
        competitors = normalize_competitor_handles(
            _json_load_list(settings.competitor_handles_json),
            own_handle=own_handle,
        )
        validate_posts_per_profile(settings.posts_per_profile)
        interval = int(settings.minimum_interval_minutes)
        if not 15 <= interval <= 1_440:
            raise ValidationAppError(
                "Invalid content agent interval."
            )
        if len(competitors) < 3:
            findings.append(
                {
                    "severity": "info",
                    "title": "Limited competitor coverage",
                    "message": (
                        "Three to five competitor handles usually provide stronger evidence."
                    ),
                    "action": (
                        "Add more relevant competitors when available."
                    ),
                }
            )
    except (TypeError, ValueError, ValidationAppError):
        findings.append(
            {
                "severity": "critical",
                "title": "Invalid content intelligence settings",
                "message": (
                    "One or more saved handles, limits, or intervals failed validation."
                ),
                "action": (
                    "Review and save the Content Agent Team configuration again."
                ),
            }
        )

    if not os.getenv("APIFY_API_TOKEN", "").strip():
        findings.append(
            {
                "severity": "warning",
                "title": "Apify token missing",
                "message": (
                    "Instagram synchronization is disabled until APIFY_API_TOKEN is configured."
                ),
                "action": (
                    "Store the token in the secret manager or .env file, never in the dashboard."
                ),
            }
        )
    if not provider_available:
        findings.append(
            {
                "severity": "critical",
                "title": "AI provider unavailable",
                "message": (
                    "The five-agent team cannot run without OpenAI or Anthropic."
                ),
                "action": (
                    "Configure a valid provider key in Provider Settings."
                ),
            }
        )
    if settings.telegram_reports_enabled:
        if not os.getenv("TELEGRAM_BOT_TOKEN", "").strip():
            findings.append(
                {
                    "severity": "critical",
                    "title": "Telegram reporting misconfigured",
                    "message": (
                        "Reports are enabled but TELEGRAM_BOT_TOKEN is missing."
                    ),
                    "action": (
                        "Add the Telegram bot token to the secret manager."
                    ),
                }
            )
        report_ids = (
            os.getenv("TELEGRAM_REPORT_CHAT_IDS")
            or os.getenv("TELEGRAM_ALERT_CHAT_IDS")
            or os.getenv("TELEGRAM_ADMIN_IDS", "")
        ).strip()
        if not report_ids:
            findings.append(
                {
                    "severity": "critical",
                    "title": "Telegram recipients missing",
                    "message": (
                        "Reports are enabled but no report chat IDs are configured."
                    ),
                    "action": (
                        "Set TELEGRAM_REPORT_CHAT_IDS to comma-separated numeric IDs."
                    ),
                }
            )
    if settings.schedule_enabled and settings.minimum_interval_minutes < 15:
        findings.append(
            {
                "severity": "critical",
                "title": "Unsafe schedule interval",
                "message": (
                    "The configured interval is below the 15-minute safety boundary."
                ),
                "action": "Increase the interval to at least 15 minutes.",
            }
        )
    if not findings:
        findings.append(
            {
                "severity": "success",
                "title": "Content agent security posture is healthy",
                "message": (
                    "Secrets remain environment-only, network hosts are fixed, inputs are bounded, "
                    "and outputs require human review."
                ),
                "action": (
                    "Continue monitoring provider, Apify, Telegram, and CI security alerts."
                ),
            }
        )
    return findings


def is_cycle_due(
    settings: ContentAgentSettings,
    *,
    now: datetime | None = None,
) -> bool:
    if not settings.schedule_enabled:
        return False
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    if settings.last_cycle_at is None:
        return True
    last = settings.last_cycle_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    elapsed_minutes = (
        current - last.astimezone(timezone.utc)
    ).total_seconds() / 60
    return elapsed_minutes >= max(
        15,
        min(settings.minimum_interval_minutes, 1_440),
    )
