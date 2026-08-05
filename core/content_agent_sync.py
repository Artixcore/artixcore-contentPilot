"""Configuration, Apify synchronization, and persistence for content intelligence."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.apify_instagram import (
    ApifyInstagramClient,
    InstagramPostRecord,
    normalize_competitor_handles,
    validate_actor_id,
    validate_instagram_handle,
    validate_posts_per_profile,
)
from core.content_agent_common import _json_dumps, _json_load_list
from core.content_intelligence_models import (
    ContentAgentSettings,
    SocialPostSnapshot,
    SocialProfileSnapshot,
)
from core.error_handler import format_user_error
from core.errors import ValidationAppError
from core.models import utc_now
from core.rate_limiter import check_rate_limit


def get_content_agent_settings(
    session: Session,
) -> ContentAgentSettings | None:
    return session.execute(select(ContentAgentSettings)).scalars().first()


def save_content_agent_settings(
    session: Session,
    *,
    own_handle: object,
    competitor_handles: list[object] | tuple[object, ...],
    apify_actor_id: object,
    posts_per_profile: object,
    schedule_enabled: bool,
    minimum_interval_minutes: object,
    telegram_reports_enabled: bool,
) -> ContentAgentSettings:
    safe_own = validate_instagram_handle(
        own_handle,
        field="Your Instagram handle",
    )
    safe_competitors = normalize_competitor_handles(
        competitor_handles,
        own_handle=safe_own,
    )
    safe_actor = validate_actor_id(apify_actor_id)
    safe_posts = validate_posts_per_profile(posts_per_profile)
    try:
        safe_interval = int(minimum_interval_minutes)
    except (TypeError, ValueError) as exc:
        raise ValidationAppError(
            "Agent cycle interval must be an integer."
        ) from exc
    if not 15 <= safe_interval <= 1_440:
        raise ValidationAppError(
            "Agent cycle interval must be between 15 and 1440 minutes."
        )

    settings = get_content_agent_settings(session)
    if settings is None:
        settings = ContentAgentSettings()
        session.add(settings)
    settings.own_instagram_handle = safe_own
    settings.competitor_handles_json = _json_dumps(
        safe_competitors,
        maximum=2_000,
    )
    settings.apify_actor_id = safe_actor
    settings.posts_per_profile = safe_posts
    settings.schedule_enabled = bool(schedule_enabled)
    settings.minimum_interval_minutes = safe_interval
    settings.telegram_reports_enabled = bool(telegram_reports_enabled)
    try:
        session.commit()
        session.refresh(settings)
    except Exception:
        session.rollback()
        raise
    return settings


def _upsert_profile(
    session: Session,
    *,
    handle: str,
    is_owned: bool,
) -> SocialProfileSnapshot:
    profile = session.execute(
        select(SocialProfileSnapshot).where(
            SocialProfileSnapshot.platform == "instagram",
            SocialProfileSnapshot.handle == handle,
        )
    ).scalars().first()
    if profile is None:
        profile = SocialProfileSnapshot(
            platform="instagram",
            handle=handle,
            profile_url=f"https://www.instagram.com/{handle}/",
        )
        session.add(profile)
    profile.is_owned = bool(is_owned)
    profile.is_active = True
    profile.profile_url = f"https://www.instagram.com/{handle}/"
    return profile


def _persist_post(
    session: Session,
    *,
    profile: SocialProfileSnapshot,
    record: InstagramPostRecord,
) -> bool:
    existing = session.execute(
        select(SocialPostSnapshot).where(
            SocialPostSnapshot.profile_id == profile.id,
            SocialPostSnapshot.external_id == record.external_id,
        )
    ).scalars().first()
    created = existing is None
    post = existing or SocialPostSnapshot(
        profile_id=profile.id,
        external_id=record.external_id,
        raw_digest=record.raw_digest,
    )
    if created:
        session.add(post)
    post.content_type = record.content_type
    post.caption = record.caption
    post.permalink = record.permalink
    post.thumbnail_url = record.thumbnail_url
    post.published_at = record.published_at
    post.likes_count = record.likes_count
    post.comments_count = record.comments_count
    post.shares_count = record.shares_count
    post.views_count = record.views_count
    post.engagement_rate = record.engagement_rate
    post.hashtags_json = _json_dumps(list(record.hashtags), maximum=5_000)
    post.raw_digest = record.raw_digest
    post.source_metadata_json = _json_dumps(
        record.source_metadata,
        maximum=10_000,
    )
    return created


def sync_instagram_intelligence(
    session: Session,
    *,
    settings: ContentAgentSettings | None = None,
    client: ApifyInstagramClient | None = None,
) -> dict[str, Any]:
    settings = settings or get_content_agent_settings(session)
    if settings is None:
        raise ValidationAppError(
            "Configure Content Agent Team settings before syncing data."
        )

    own = validate_instagram_handle(
        settings.own_instagram_handle,
        field="Your Instagram handle",
    )
    competitors = normalize_competitor_handles(
        _json_load_list(settings.competitor_handles_json),
        own_handle=own,
    )
    handles = [own, *competitors]
    actor_id = validate_actor_id(settings.apify_actor_id)
    posts_per_profile = validate_posts_per_profile(settings.posts_per_profile)
    check_rate_limit(
        "content_intelligence_sync",
        key=f"workspace:{session.info.get('workspace_id', 'unknown')}",
    )

    configured = {handle.casefold() for handle in handles}
    existing_profiles = session.execute(
        select(SocialProfileSnapshot).where(
            SocialProfileSnapshot.platform == "instagram"
        )
    ).scalars().all()
    for existing_profile in existing_profiles:
        is_configured = existing_profile.handle.casefold() in configured
        existing_profile.is_active = is_configured
        existing_profile.is_owned = (
            is_configured and existing_profile.handle == own
        )

    profiles: dict[str, SocialProfileSnapshot] = {}
    for handle in handles:
        profiles[handle] = _upsert_profile(
            session,
            handle=handle,
            is_owned=handle == own,
        )
    try:
        session.commit()
        for profile in profiles.values():
            session.refresh(profile)
    except Exception:
        session.rollback()
        raise

    apify = client or ApifyInstagramClient()
    try:
        records = apify.fetch_posts(
            handles=handles,
            actor_id=actor_id,
            posts_per_profile=posts_per_profile,
        )
    except Exception as exc:
        safe_error = format_user_error(exc)
        for profile in profiles.values():
            profile.last_sync_error = str(
                safe_error.get("message", "Instagram sync failed.")
            )[:2_000]
        try:
            session.commit()
        except Exception:
            session.rollback()
        raise

    created_count = 0
    updated_count = 0
    now = utc_now()
    try:
        for record in records:
            profile = profiles.get(record.handle)
            if profile is None:
                continue
            if _persist_post(session, profile=profile, record=record):
                created_count += 1
            else:
                updated_count += 1
        for profile in profiles.values():
            profile.last_synced_at = now
            profile.last_sync_error = None
        session.commit()
    except Exception:
        session.rollback()
        raise

    return {
        "profiles": len(profiles),
        "records_received": len(records),
        "posts_created": created_count,
        "posts_updated": updated_count,
        "posts_upserted": created_count + updated_count,
        "synced_at": now.isoformat(),
    }
