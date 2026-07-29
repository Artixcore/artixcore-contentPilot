"""Persistent in-app notifications with deduplication and safe rendering data."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from core.auth import AuthenticatedUser
from core.errors import ValidationAppError
from core.operations_models import SystemNotification
from core.validation import normalize_text

_SEVERITIES = frozenset({"info", "success", "warning", "error", "critical"})


def create_notification(
    session: Session,
    *,
    title: str,
    message: str,
    severity: str = "info",
    recipient_user_id: int | None = None,
    action_label: str | None = None,
    action_page: str | None = None,
    deduplication_key: str | None = None,
    expires_in_hours: int | None = None,
    commit: bool = True,
) -> SystemNotification:
    """Create or update a notification.

    Set commit=False when the notification must be committed atomically with a
    surrounding business transaction. Existing callers retain commit=True.
    """
    safe_severity = str(severity or "info").strip().lower()
    if safe_severity not in _SEVERITIES:
        raise ValidationAppError("Notification severity is invalid.")
    safe_title = normalize_text(title, field="Notification title", min_length=1, max_length=255)
    safe_message = normalize_text(message, field="Notification message", min_length=1, max_length=10_000)
    safe_action_label = (
        normalize_text(action_label, field="Action label", max_length=100, allow_newlines=False)
        if action_label
        else None
    )
    safe_action_page = (
        normalize_text(action_page, field="Action page", max_length=100, allow_newlines=False)
        if action_page
        else None
    )
    safe_key = (
        normalize_text(
            deduplication_key,
            field="Deduplication key",
            max_length=128,
            allow_newlines=False,
        )
        if deduplication_key
        else None
    )

    if safe_key:
        existing = session.scalar(
            select(SystemNotification)
            .where(
                SystemNotification.deduplication_key == safe_key,
                SystemNotification.recipient_user_id == recipient_user_id,
                SystemNotification.is_read.is_(False),
            )
            .order_by(SystemNotification.created_at.desc())
            .limit(1)
        )
        if existing:
            existing.title = safe_title
            existing.message = safe_message
            existing.severity = safe_severity
            existing.created_at = datetime.now(timezone.utc)
            if commit:
                session.commit()
                session.refresh(existing)
            else:
                session.flush()
            return existing

    expires_at = None
    if expires_in_hours is not None:
        safe_hours = min(max(int(expires_in_hours), 1), 24 * 365)
        expires_at = datetime.now(timezone.utc) + timedelta(hours=safe_hours)

    notification = SystemNotification(
        recipient_user_id=recipient_user_id,
        severity=safe_severity,
        title=safe_title,
        message=safe_message,
        action_label=safe_action_label,
        action_page=safe_action_page,
        deduplication_key=safe_key,
        expires_at=expires_at,
    )
    session.add(notification)
    if commit:
        session.commit()
        session.refresh(notification)
    else:
        session.flush()
    return notification


def list_notifications(
    session: Session,
    *,
    user: AuthenticatedUser,
    unread_only: bool = False,
    limit: int = 100,
) -> list[SystemNotification]:
    now = datetime.now(timezone.utc)
    query = select(SystemNotification).where(
        or_(
            SystemNotification.recipient_user_id.is_(None),
            SystemNotification.recipient_user_id == user.id,
        ),
        or_(SystemNotification.expires_at.is_(None), SystemNotification.expires_at > now),
    )
    if unread_only:
        query = query.where(SystemNotification.is_read.is_(False))
    query = query.order_by(SystemNotification.created_at.desc()).limit(
        min(max(int(limit), 1), 500)
    )
    return list(session.scalars(query).all())


def mark_notification_read(
    session: Session,
    *,
    notification_id: int,
    user: AuthenticatedUser,
) -> SystemNotification:
    notification = session.get(SystemNotification, int(notification_id))
    if notification is None:
        raise ValidationAppError("Notification was not found.")
    if notification.recipient_user_id not in {None, user.id} and not user.can("manage_security"):
        raise ValidationAppError("You cannot modify this notification.")
    notification.is_read = True
    notification.read_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(notification)
    return notification


def mark_all_read(session: Session, *, user: AuthenticatedUser) -> int:
    notifications = list_notifications(session, user=user, unread_only=True, limit=500)
    now = datetime.now(timezone.utc)
    changed = 0
    for notification in notifications:
        if notification.recipient_user_id in {None, user.id}:
            notification.is_read = True
            notification.read_at = now
            changed += 1
    session.commit()
    return changed
