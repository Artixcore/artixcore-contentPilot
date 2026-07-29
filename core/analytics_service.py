"""Workspace analytics aggregation with bounded queries and safe calculations."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from core.errors import ValidationAppError
from core.models import Campaign, Post, PostAnalytics
from core.operations_models import BackgroundJob
from core.product_models import CampaignItem, LeadRecord, UsageEvent
from core.tenancy import WorkspaceContext, require_workspace_permission

_MAX_RANGE_DAYS = 730


@dataclass(frozen=True)
class AnalyticsSummary:
    posts_created: int
    posts_published: int
    posts_failed: int
    scheduled_items: int
    active_campaigns: int
    total_impressions: int
    total_reach: int
    total_engagements: int
    total_clicks: int
    engagement_rate: float
    click_through_rate: float
    leads_created: int
    qualified_leads: int
    won_leads: int
    lead_conversion_rate: float
    jobs_succeeded: int
    jobs_failed: int
    job_success_rate: float
    usage_quantity: int
    estimated_cost: float

    def to_dict(self) -> dict:
        return asdict(self)


def _utc(value: datetime | None, *, default: datetime) -> datetime:
    if value is None:
        return default
    if not isinstance(value, datetime):
        raise ValidationAppError("Analytics date range is invalid.")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_date_range(
    start: datetime | None,
    end: datetime | None,
) -> tuple[datetime, datetime]:
    now = datetime.now(timezone.utc)
    safe_end = _utc(end, default=now)
    safe_start = _utc(start, default=safe_end - timedelta(days=30))
    if safe_end < safe_start:
        raise ValidationAppError("Analytics end date cannot be before the start date.")
    if safe_end - safe_start > timedelta(days=_MAX_RANGE_DAYS):
        raise ValidationAppError(f"Analytics date range cannot exceed {_MAX_RANGE_DAYS} days.")
    if safe_end > now + timedelta(days=1):
        raise ValidationAppError("Analytics end date cannot be in the distant future.")
    return safe_start, safe_end


def _rate(numerator: int | float, denominator: int | float) -> float:
    if not denominator:
        return 0.0
    return round((float(numerator) / float(denominator)) * 100.0, 2)


def get_analytics_summary(
    session: Session,
    *,
    context: WorkspaceContext,
    start: datetime | None = None,
    end: datetime | None = None,
) -> AnalyticsSummary:
    require_workspace_permission(context, "analytics:read")
    start_utc, end_utc = validate_date_range(start, end)

    post_row = session.execute(
        select(
            func.count(Post.id),
            func.coalesce(func.sum(case((Post.status == "published", 1), else_=0)), 0),
            func.coalesce(func.sum(case((Post.status == "failed", 1), else_=0)), 0),
        ).where(Post.created_at >= start_utc, Post.created_at <= end_utc)
    ).one()
    posts_created, posts_published, posts_failed = (int(value or 0) for value in post_row)

    scheduled_items = int(
        session.scalar(
            select(func.count(CampaignItem.id)).where(
                CampaignItem.scheduled_at >= start_utc,
                CampaignItem.scheduled_at <= end_utc,
                CampaignItem.status.notin_(("cancelled", "failed")),
            )
        )
        or 0
    )
    active_campaigns = int(
        session.scalar(
            select(func.count(Campaign.id)).where(Campaign.status.in_(("active", "paused")))
        )
        or 0
    )

    analytics_row = session.execute(
        select(
            func.coalesce(func.sum(PostAnalytics.impressions), 0),
            func.coalesce(func.sum(PostAnalytics.reach), 0),
            func.coalesce(func.sum(PostAnalytics.likes), 0),
            func.coalesce(func.sum(PostAnalytics.comments), 0),
            func.coalesce(func.sum(PostAnalytics.shares), 0),
            func.coalesce(func.sum(PostAnalytics.clicks), 0),
        ).where(PostAnalytics.created_at >= start_utc, PostAnalytics.created_at <= end_utc)
    ).one()
    impressions, reach, likes, comments, shares, clicks = (int(value or 0) for value in analytics_row)
    engagements = likes + comments + shares

    lead_row = session.execute(
        select(
            func.count(LeadRecord.id),
            func.coalesce(
                func.sum(
                    case(
                        (LeadRecord.status.in_(("qualified", "contacted", "proposal", "won")), 1),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(func.sum(case((LeadRecord.status == "won", 1), else_=0)), 0),
        ).where(LeadRecord.created_at >= start_utc, LeadRecord.created_at <= end_utc)
    ).one()
    leads_created, qualified_leads, won_leads = (int(value or 0) for value in lead_row)

    job_row = session.execute(
        select(
            func.coalesce(func.sum(case((BackgroundJob.status == "succeeded", 1), else_=0)), 0),
            func.coalesce(
                func.sum(
                    case((BackgroundJob.status.in_(("failed", "dead_letter")), 1), else_=0)
                ),
                0,
            ),
        ).where(BackgroundJob.created_at >= start_utc, BackgroundJob.created_at <= end_utc)
    ).one()
    jobs_succeeded, jobs_failed = (int(value or 0) for value in job_row)

    usage_row = session.execute(
        select(
            func.coalesce(func.sum(UsageEvent.quantity), 0),
            func.coalesce(
                func.sum(UsageEvent.quantity * func.coalesce(UsageEvent.unit_cost, 0.0)),
                0.0,
            ),
        ).where(UsageEvent.created_at >= start_utc, UsageEvent.created_at <= end_utc)
    ).one()
    usage_quantity = int(usage_row[0] or 0)
    estimated_cost = round(float(usage_row[1] or 0.0), 4)

    return AnalyticsSummary(
        posts_created=posts_created,
        posts_published=posts_published,
        posts_failed=posts_failed,
        scheduled_items=scheduled_items,
        active_campaigns=active_campaigns,
        total_impressions=impressions,
        total_reach=reach,
        total_engagements=engagements,
        total_clicks=clicks,
        engagement_rate=_rate(engagements, impressions),
        click_through_rate=_rate(clicks, impressions),
        leads_created=leads_created,
        qualified_leads=qualified_leads,
        won_leads=won_leads,
        lead_conversion_rate=_rate(won_leads, leads_created),
        jobs_succeeded=jobs_succeeded,
        jobs_failed=jobs_failed,
        job_success_rate=_rate(jobs_succeeded, jobs_succeeded + jobs_failed),
        usage_quantity=usage_quantity,
        estimated_cost=estimated_cost,
    )


def get_platform_breakdown(
    session: Session,
    *,
    context: WorkspaceContext,
    start: datetime | None = None,
    end: datetime | None = None,
) -> list[dict]:
    require_workspace_permission(context, "analytics:read")
    start_utc, end_utc = validate_date_range(start, end)
    rows = session.execute(
        select(
            PostAnalytics.platform,
            func.coalesce(func.sum(PostAnalytics.impressions), 0),
            func.coalesce(func.sum(PostAnalytics.reach), 0),
            func.coalesce(func.sum(PostAnalytics.likes), 0),
            func.coalesce(func.sum(PostAnalytics.comments), 0),
            func.coalesce(func.sum(PostAnalytics.shares), 0),
            func.coalesce(func.sum(PostAnalytics.clicks), 0),
        )
        .where(PostAnalytics.created_at >= start_utc, PostAnalytics.created_at <= end_utc)
        .group_by(PostAnalytics.platform)
        .order_by(func.sum(PostAnalytics.impressions).desc())
    ).all()
    result = []
    for platform, impressions, reach, likes, comments, shares, clicks in rows:
        engagement = int(likes or 0) + int(comments or 0) + int(shares or 0)
        result.append(
            {
                "platform": platform,
                "impressions": int(impressions or 0),
                "reach": int(reach or 0),
                "engagements": engagement,
                "clicks": int(clicks or 0),
                "engagement_rate": _rate(engagement, int(impressions or 0)),
                "click_through_rate": _rate(int(clicks or 0), int(impressions or 0)),
            }
        )
    return result


def record_usage_event(
    session: Session,
    *,
    context: WorkspaceContext,
    event_type: str,
    quantity: int = 1,
    unit_cost: float | None = None,
    actor_user_id: int | None = None,
    metadata: dict | None = None,
) -> UsageEvent:
    require_workspace_permission(context, "workspace:read")
    clean_type = str(event_type or "").strip().lower()
    if not clean_type or len(clean_type) > 100 or not all(
        character.isalnum() or character in {"_", ".", "-"} for character in clean_type
    ):
        raise ValidationAppError("Usage event type is invalid.")
    safe_quantity = int(quantity)
    if not 0 <= safe_quantity <= 100_000_000:
        raise ValidationAppError("Usage quantity is outside the supported range.")
    safe_cost = None if unit_cost is None else float(unit_cost)
    if safe_cost is not None and not 0 <= safe_cost <= 1_000_000:
        raise ValidationAppError("Usage unit cost is outside the supported range.")
    metadata_json = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))
    if len(metadata_json) > 20_000:
        raise ValidationAppError("Usage metadata is too large.")
    model = UsageEvent(
        event_type=clean_type,
        quantity=safe_quantity,
        unit_cost=safe_cost,
        actor_user_id=actor_user_id,
        metadata_json=metadata_json,
    )
    session.add(model)
    session.commit()
    session.refresh(model)
    return model
