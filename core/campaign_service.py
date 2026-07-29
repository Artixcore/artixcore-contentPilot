"""Validated campaign, calendar, and reusable content-template services."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from core.audit import log_audit_event
from core.auth import AuthenticatedUser
from core.errors import ValidationAppError
from core.models import Campaign, PLATFORMS, Post
from core.product_models import CampaignItem, ContentTemplate
from core.tenancy import WorkspaceContext, require_workspace_permission

_CAMPAIGN_STATUSES = frozenset({"draft", "active", "paused", "completed", "archived"})
_ITEM_STATUSES = frozenset(
    {"planned", "draft", "pending_approval", "approved", "scheduled", "published", "cancelled", "failed"}
)
_CONTENT_TYPES = frozenset({"post", "article", "carousel", "video", "story", "email", "ad"})


def _text(value: object, *, field: str, minimum: int = 1, maximum: int = 255) -> str:
    clean = " ".join(str(value or "").strip().split())
    if not minimum <= len(clean) <= maximum:
        raise ValidationAppError(f"{field} must contain between {minimum} and {maximum} characters.")
    return clean


def _long_text(value: object, *, field: str, maximum: int = 20_000) -> str:
    clean = str(value or "").strip()
    if len(clean) > maximum:
        raise ValidationAppError(f"{field} cannot exceed {maximum} characters.")
    return clean


def _aware_utc(value: datetime | None, *, field: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValidationAppError(f"{field} must be a valid date and time.")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _platform(value: object) -> str:
    platform = str(value or "").strip().lower()
    if platform not in PLATFORMS:
        raise ValidationAppError("Select a supported publishing platform.")
    return platform


def _json_list(values: Iterable[object], *, maximum_items: int = 100) -> str:
    clean = []
    for value in values:
        item = " ".join(str(value or "").strip().split())
        if item and item not in clean:
            clean.append(item[:100])
        if len(clean) > maximum_items:
            raise ValidationAppError("Too many list items were provided.")
    return json.dumps(clean, ensure_ascii=False, separators=(",", ":"))


def create_template(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    name: str,
    platform: str,
    category: str,
    body_template: str,
    hashtags: list[str] | None = None,
    default_cta: str = "",
) -> ContentTemplate:
    require_workspace_permission(context, "content:write")
    model = ContentTemplate(
        name=_text(name, field="Template name", minimum=2),
        platform=_platform(platform),
        category=_text(category or "general", field="Category", maximum=100).lower(),
        body_template=_long_text(body_template, field="Template body", maximum=50_000),
        default_hashtags_json=_json_list(hashtags or [], maximum_items=50),
        default_cta=_long_text(default_cta, field="Default CTA", maximum=512),
        status="active",
        created_by_user_id=actor.id,
    )
    if not model.body_template:
        raise ValidationAppError("Template body is required.")
    duplicate = session.scalar(
        select(ContentTemplate.id).where(ContentTemplate.name == model.name)
    )
    if duplicate:
        raise ValidationAppError("A template with this name already exists in the workspace.")
    session.add(model)
    session.flush()
    log_audit_event(
        session,
        action="content_template.created",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="content_template",
        resource_id=model.id,
        event_data={"platform": model.platform, "category": model.category},
    )
    session.commit()
    session.refresh(model)
    return model


def list_templates(session: Session, *, active_only: bool = True) -> list[ContentTemplate]:
    query = select(ContentTemplate)
    if active_only:
        query = query.where(ContentTemplate.status == "active")
    return list(session.scalars(query.order_by(ContentTemplate.name.asc())).all())


def archive_template(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    template_id: int,
) -> ContentTemplate:
    require_workspace_permission(context, "content:write")
    model = session.get(ContentTemplate, int(template_id))
    if model is None:
        raise ValidationAppError("Template was not found.")
    model.status = "archived"
    log_audit_event(
        session,
        action="content_template.archived",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="content_template",
        resource_id=model.id,
    )
    session.commit()
    session.refresh(model)
    return model


def create_campaign(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    name: str,
    goal: str,
    description: str,
    platforms: list[str],
    start_date: datetime | None,
    end_date: datetime | None,
    posts_per_week: int = 3,
) -> Campaign:
    require_workspace_permission(context, "content:write")
    safe_platforms = sorted({_platform(item) for item in platforms})
    if not safe_platforms:
        raise ValidationAppError("Select at least one campaign platform.")
    start = _aware_utc(start_date, field="Start date")
    end = _aware_utc(end_date, field="End date")
    if start and end and end < start:
        raise ValidationAppError("Campaign end date cannot be before its start date.")
    frequency = int(posts_per_week)
    if not 1 <= frequency <= 100:
        raise ValidationAppError("Posts per week must be between 1 and 100.")

    model = Campaign(
        name=_text(name, field="Campaign name", minimum=2),
        goal=_long_text(goal, field="Campaign goal", maximum=512),
        description=_long_text(description, field="Campaign description"),
        platforms=json.dumps(safe_platforms, separators=(",", ":")),
        start_date=start,
        end_date=end,
        posts_per_week=frequency,
        status="draft",
    )
    session.add(model)
    session.flush()
    log_audit_event(
        session,
        action="campaign.created",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="campaign",
        resource_id=model.id,
        event_data={"platforms": safe_platforms, "posts_per_week": frequency},
    )
    session.commit()
    session.refresh(model)
    return model


def update_campaign_status(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    campaign_id: int,
    status: str,
) -> Campaign:
    require_workspace_permission(context, "content:write")
    safe_status = str(status or "").strip().lower()
    if safe_status not in _CAMPAIGN_STATUSES:
        raise ValidationAppError("Select a valid campaign status.")
    campaign = session.get(Campaign, int(campaign_id))
    if campaign is None:
        raise ValidationAppError("Campaign was not found.")
    campaign.status = safe_status
    log_audit_event(
        session,
        action="campaign.status_changed",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="campaign",
        resource_id=campaign.id,
        event_data={"status": safe_status},
    )
    session.commit()
    session.refresh(campaign)
    return campaign


def list_campaigns(session: Session, *, include_archived: bool = False) -> list[Campaign]:
    query = select(Campaign)
    if not include_archived:
        query = query.where(Campaign.status != "archived")
    return list(session.scalars(query.order_by(Campaign.created_at.desc())).all())


def add_campaign_item(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    campaign_id: int,
    title: str,
    platform: str,
    content_type: str,
    brief: str = "",
    scheduled_at: datetime | None = None,
    post_id: int | None = None,
) -> CampaignItem:
    require_workspace_permission(context, "content:write")
    campaign = session.get(Campaign, int(campaign_id))
    if campaign is None or campaign.status == "archived":
        raise ValidationAppError("Campaign was not found or is archived.")
    safe_platform = _platform(platform)
    safe_type = str(content_type or "post").strip().lower()
    if safe_type not in _CONTENT_TYPES:
        raise ValidationAppError("Select a supported content type.")
    scheduled = _aware_utc(scheduled_at, field="Scheduled date")
    linked_post = None
    if post_id is not None:
        linked_post = session.get(Post, int(post_id))
        if linked_post is None:
            raise ValidationAppError("The selected post was not found in this workspace.")
        if linked_post.platform != safe_platform:
            raise ValidationAppError("Campaign item platform must match the linked post platform.")
    if scheduled:
        collision = session.scalar(
            select(CampaignItem.id).where(
                CampaignItem.platform == safe_platform,
                CampaignItem.scheduled_at == scheduled,
                CampaignItem.status.notin_(("cancelled", "failed")),
            )
        )
        if collision:
            raise ValidationAppError("Another workspace item is already scheduled for this platform and time.")
        if campaign.start_date and scheduled < _aware_utc(campaign.start_date, field="Start date"):
            raise ValidationAppError("Scheduled date cannot be before the campaign start date.")
        if campaign.end_date and scheduled > _aware_utc(campaign.end_date, field="End date"):
            raise ValidationAppError("Scheduled date cannot be after the campaign end date.")

    model = CampaignItem(
        campaign_id=campaign.id,
        post_id=linked_post.id if linked_post else None,
        title=_text(title, field="Item title", minimum=2),
        platform=safe_platform,
        content_type=safe_type,
        brief=_long_text(brief, field="Content brief"),
        scheduled_at=scheduled,
        status="scheduled" if scheduled else "planned",
        created_by_user_id=actor.id,
    )
    session.add(model)
    session.flush()
    log_audit_event(
        session,
        action="campaign.item_created",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="campaign_item",
        resource_id=model.id,
        event_data={
            "campaign_id": campaign.id,
            "platform": safe_platform,
            "scheduled_at": scheduled.isoformat() if scheduled else None,
        },
    )
    session.commit()
    session.refresh(model)
    return model


def update_campaign_item_status(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    item_id: int,
    status: str,
) -> CampaignItem:
    require_workspace_permission(context, "content:write")
    safe_status = str(status or "").strip().lower()
    if safe_status not in _ITEM_STATUSES:
        raise ValidationAppError("Select a valid calendar-item status.")
    model = session.get(CampaignItem, int(item_id))
    if model is None:
        raise ValidationAppError("Campaign item was not found.")
    model.status = safe_status
    log_audit_event(
        session,
        action="campaign.item_status_changed",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="campaign_item",
        resource_id=model.id,
        event_data={"status": safe_status},
    )
    session.commit()
    session.refresh(model)
    return model


def list_calendar_items(
    session: Session,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    platform: str | None = None,
    campaign_id: int | None = None,
) -> list[CampaignItem]:
    query = select(CampaignItem)
    start_utc = _aware_utc(start, field="Calendar start")
    end_utc = _aware_utc(end, field="Calendar end")
    if start_utc:
        query = query.where(CampaignItem.scheduled_at >= start_utc)
    if end_utc:
        query = query.where(CampaignItem.scheduled_at <= end_utc)
    if platform:
        query = query.where(CampaignItem.platform == _platform(platform))
    if campaign_id is not None:
        query = query.where(CampaignItem.campaign_id == int(campaign_id))
    query = query.where(
        or_(CampaignItem.scheduled_at.is_not(None), CampaignItem.status == "planned")
    ).order_by(CampaignItem.scheduled_at.asc().nullslast(), CampaignItem.sort_order.asc())
    return list(session.scalars(query).all())
