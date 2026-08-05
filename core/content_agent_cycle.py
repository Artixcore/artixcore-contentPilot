"""Full content-agent cycle with cached-data fallback and optional Telegram reporting."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.content_agent_execution import run_content_agent_team
from core.content_agent_reports import (
    build_content_agent_report,
    send_content_agent_report,
)
from core.content_agent_sync import (
    get_content_agent_settings,
    sync_instagram_intelligence,
)
from core.content_intelligence_models import (
    SocialPostSnapshot,
    SocialProfileSnapshot,
)
from core.error_handler import handle_exception
from core.errors import ValidationAppError
from core.models import utc_now


def run_full_content_cycle(
    session: Session,
    *,
    sync_data: bool = True,
    send_telegram: bool | None = None,
    provider_mode: str = "auto",
) -> dict[str, Any]:
    settings = get_content_agent_settings(session)
    if settings is None:
        raise ValidationAppError(
            "Configure Content Agent Team settings before running a cycle."
        )

    sync_summary: dict[str, Any] | None = None
    sync_error: dict[str, Any] | None = None
    if sync_data:
        try:
            sync_summary = sync_instagram_intelligence(
                session,
                settings=settings,
            )
        except Exception as exc:
            existing_count = session.execute(
                select(func.count(SocialPostSnapshot.id))
                .join(
                    SocialProfileSnapshot,
                    SocialProfileSnapshot.id
                    == SocialPostSnapshot.profile_id,
                )
                .where(SocialProfileSnapshot.is_active.is_(True))
            ).scalar_one()
            if not existing_count:
                raise
            sync_error = handle_exception(
                exc,
                context="content_agent_sync_using_cached_data",
            )

    team = run_content_agent_team(
        session,
        settings=settings,
        provider_mode=provider_mode,
    )
    settings = get_content_agent_settings(session)
    if settings is not None:
        settings.last_cycle_at = utc_now()
        session.commit()

    telegram_requested = (
        settings.telegram_reports_enabled
        if send_telegram is None and settings
        else bool(send_telegram)
    )
    report_text = build_content_agent_report(
        cycle_id=team["cycle_id"],
        own_handle=(
            settings.own_instagram_handle if settings else ""
        ),
        sync_summary=sync_summary,
        agent_results=team["results"],
        failed_agents=team["failures"],
    )
    telegram_delivered = 0
    telegram_error: dict[str, Any] | None = None
    if telegram_requested:
        try:
            telegram_delivered = send_content_agent_report(report_text)
        except Exception as exc:
            telegram_error = handle_exception(
                exc,
                context="content_agent_telegram_report",
            )

    return {
        **team,
        "sync": sync_summary,
        "sync_error": sync_error,
        "telegram_requested": telegram_requested,
        "telegram_delivered": telegram_delivered,
        "telegram_error": telegram_error,
        "report_text": report_text,
    }
