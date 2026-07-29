"""Operational models for durable jobs, notifications, and integration health."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from core.models import Base, utc_now
from core.tenancy_base import TenantScopedMixin


class BackgroundJob(TenantScopedMixin, Base):
    __tablename__ = "background_jobs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','cancelled','dead_letter')",
            name="ck_background_jobs_status",
        ),
        CheckConstraint("priority BETWEEN 0 AND 100", name="ck_background_jobs_priority"),
        CheckConstraint("attempts >= 0", name="ck_background_jobs_attempts"),
        CheckConstraint("max_attempts BETWEEN 1 AND 20", name="ck_background_jobs_max_attempts"),
        UniqueConstraint(
            "workspace_id", "idempotency_key", name="uq_background_jobs_workspace_idempotency"
        ),
        Index(
            "ix_background_jobs_claim",
            "workspace_id",
            "status",
            "available_at",
            "priority",
            "created_at",
        ),
        Index("ix_background_jobs_type_status", "workspace_id", "job_type", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_type: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    requested_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    locked_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class SystemNotification(TenantScopedMixin, Base):
    __tablename__ = "system_notifications"
    __table_args__ = (
        CheckConstraint(
            "severity IN ('info','success','warning','error','critical')",
            name="ck_system_notifications_severity",
        ),
        UniqueConstraint(
            "workspace_id", "recipient_user_id", "deduplication_key",
            name="uq_notifications_workspace_recipient_dedup",
        ),
        Index(
            "ix_system_notifications_unread",
            "workspace_id",
            "recipient_user_id",
            "is_read",
            "created_at",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recipient_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, default="info")
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    action_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action_page: Mapped[str | None] = mapped_column(String(100), nullable=True)
    deduplication_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class IntegrationConnection(TenantScopedMixin, Base):
    __tablename__ = "integration_connections"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "platform", "account_key", name="uq_integration_workspace_account"
        ),
        CheckConstraint(
            "status IN ('disconnected','connecting','connected','degraded','expired','disabled')",
            name="ck_integration_connections_status",
        ),
        Index("ix_integration_connections_status", "workspace_id", "status", "platform"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    platform: Mapped[str] = mapped_column(String(50), nullable=False)
    account_key: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="disconnected")
    access_credential_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    refresh_credential_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )


class WebhookReceipt(TenantScopedMixin, Base):
    __tablename__ = "webhook_receipts"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "provider", "event_id", name="uq_webhook_workspace_provider_event"
        ),
        CheckConstraint(
            "status IN ('received','processing','processed','rejected','failed')",
            name="ck_webhook_receipts_status",
        ),
        Index("ix_webhook_receipts_status_created", "workspace_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    event_id: Mapped[str] = mapped_column(String(255), nullable=False)
    event_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="received")
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
