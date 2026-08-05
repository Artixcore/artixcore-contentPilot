"""Workspace-scoped models for competitor intelligence and the content agent team."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base, utc_now
from core.tenancy_base import TenantScopedMixin


class ContentAgentSettings(TenantScopedMixin, Base):
    __tablename__ = "content_agent_settings"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            name="uq_content_agent_settings_workspace",
        ),
        CheckConstraint(
            "posts_per_profile BETWEEN 1 AND 100",
            name="ck_content_agent_posts_per_profile",
        ),
        CheckConstraint(
            "minimum_interval_minutes BETWEEN 15 AND 1440",
            name="ck_content_agent_interval",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    own_instagram_handle: Mapped[str] = mapped_column(
        String(64), nullable=False, default=""
    )
    competitor_handles_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]"
    )
    apify_actor_id: Mapped[str] = mapped_column(
        String(255), nullable=False, default="apify/instagram-scraper"
    )
    posts_per_profile: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    schedule_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    minimum_interval_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1440
    )
    telegram_reports_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    last_cycle_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class SocialProfileSnapshot(TenantScopedMixin, Base):
    __tablename__ = "social_profile_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "platform",
            "handle",
            name="uq_social_profile_workspace_handle",
        ),
        CheckConstraint(
            "platform IN ('instagram')",
            name="ck_social_profile_platform",
        ),
        Index(
            "ix_social_profile_workspace_owned_active",
            "workspace_id",
            "is_owned",
            "is_active",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(
        String(32), nullable=False, default="instagram"
    )
    handle: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    profile_url: Mapped[str] = mapped_column(String(512), nullable=False)
    is_owned: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_sync_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class SocialPostSnapshot(TenantScopedMixin, Base):
    __tablename__ = "social_post_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "profile_id",
            "external_id",
            name="uq_social_post_workspace_external",
        ),
        CheckConstraint("likes_count >= 0", name="ck_social_post_likes"),
        CheckConstraint("comments_count >= 0", name="ck_social_post_comments"),
        CheckConstraint("shares_count >= 0", name="ck_social_post_shares"),
        CheckConstraint("views_count >= 0", name="ck_social_post_views"),
        Index(
            "ix_social_posts_workspace_profile_published",
            "workspace_id",
            "profile_id",
            "published_at",
        ),
        Index(
            "ix_social_posts_workspace_engagement",
            "workspace_id",
            "engagement_rate",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("social_profile_snapshots.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(64), nullable=False, default="post"
    )
    caption: Mapped[str] = mapped_column(Text, nullable=False, default="")
    permalink: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    thumbnail_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    likes_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    comments_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    shares_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    views_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    engagement_rate: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0
    )
    hashtags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    raw_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    source_metadata_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}"
    )
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class ContentAgentRun(TenantScopedMixin, Base):
    __tablename__ = "content_agent_runs"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "cycle_id",
            "agent_key",
            name="uq_content_agent_run_cycle_agent",
        ),
        CheckConstraint(
            "agent_key IN ('ideator','hook_script','planner','analyst','dm_manager')",
            name="ck_content_agent_run_agent",
        ),
        CheckConstraint(
            "status IN ('running','succeeded','failed','skipped')",
            name="ck_content_agent_run_status",
        ),
        Index(
            "ix_content_agent_runs_workspace_cycle",
            "workspace_id",
            "cycle_id",
            "started_at",
        ),
        Index(
            "ix_content_agent_runs_workspace_agent",
            "workspace_id",
            "agent_key",
            "started_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[str] = mapped_column(String(64), nullable=False)
    agent_key: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="running"
    )
    input_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_used: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model_used: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ContentAgentArtifact(TenantScopedMixin, Base):
    __tablename__ = "content_agent_artifacts"
    __table_args__ = (
        CheckConstraint(
            "status IN ('draft','reviewed','accepted','rejected','archived')",
            name="ck_content_agent_artifact_status",
        ),
        Index(
            "ix_content_agent_artifacts_workspace_cycle",
            "workspace_id",
            "cycle_id",
            "created_at",
        ),
        Index(
            "ix_content_agent_artifacts_workspace_agent",
            "workspace_id",
            "agent_key",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    cycle_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("content_agent_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_key: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    artifact_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="draft"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )
