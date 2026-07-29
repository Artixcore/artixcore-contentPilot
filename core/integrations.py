"""Integration connection registry and encrypted credential references."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.audit import log_audit_event
from core.auth import AuthenticatedUser, require_permission
from core.errors import ValidationAppError
from core.jobs import enqueue_job
from core.operations_models import IntegrationConnection
from core.security_models import EncryptedCredential
from core.validation import normalize_text

_ALLOWED_PLATFORMS = frozenset(
    {
        "linkedin",
        "facebook",
        "instagram",
        "twitter",
        "youtube",
        "telegram",
        "website",
    }
)
_ALLOWED_STATUSES = frozenset(
    {"disconnected", "connecting", "connected", "degraded", "expired", "disabled"}
)


def _platform(value: object) -> str:
    platform = normalize_text(
        value,
        field="Platform",
        min_length=1,
        max_length=50,
        allow_newlines=False,
    ).lower()
    if platform not in _ALLOWED_PLATFORMS:
        raise ValidationAppError("Unsupported integration platform.")
    return platform


def _credential_exists(session: Session, name: str | None) -> str | None:
    if not name:
        return None
    clean = normalize_text(
        name,
        field="Credential name",
        min_length=2,
        max_length=255,
        allow_newlines=False,
    ).lower()
    exists = session.scalar(
        select(EncryptedCredential.id).where(
            EncryptedCredential.credential_name == clean,
            EncryptedCredential.is_active.is_(True),
        )
    )
    if not exists:
        raise ValidationAppError(f"Encrypted credential '{clean}' was not found or is inactive.")
    return clean


def upsert_connection(
    session: Session,
    *,
    platform: str,
    account_key: str,
    display_name: str,
    actor: AuthenticatedUser,
    access_credential_name: str | None = None,
    refresh_credential_name: str | None = None,
    external_account_id: str | None = None,
) -> IntegrationConnection:
    require_permission(actor, "manage_integrations")
    safe_platform = _platform(platform)
    safe_account_key = normalize_text(
        account_key,
        field="Account key",
        min_length=2,
        max_length=255,
        allow_newlines=False,
    )
    safe_name = normalize_text(
        display_name,
        field="Display name",
        min_length=2,
        max_length=255,
        allow_newlines=False,
    )
    access_name = _credential_exists(session, access_credential_name)
    refresh_name = _credential_exists(session, refresh_credential_name)
    external_id = (
        normalize_text(
            external_account_id,
            field="External account ID",
            max_length=255,
            allow_newlines=False,
        )
        if external_account_id
        else None
    )

    connection = session.scalar(
        select(IntegrationConnection).where(
            IntegrationConnection.platform == safe_platform,
            IntegrationConnection.account_key == safe_account_key,
        )
    )
    action = "integration.created"
    if connection is None:
        connection = IntegrationConnection(
            platform=safe_platform,
            account_key=safe_account_key,
            display_name=safe_name,
            status="disconnected",
            created_by_user_id=actor.id,
        )
        session.add(connection)
    else:
        action = "integration.updated"
        connection.display_name = safe_name

    connection.access_credential_name = access_name
    connection.refresh_credential_name = refresh_name
    connection.external_account_id = external_id
    try:
        session.flush()
        log_audit_event(
            session,
            action=action,
            actor_user_id=actor.id,
            actor_email=actor.email,
            resource_type="integration_connection",
            resource_id=connection.id,
            event_data={
                "platform": safe_platform,
                "account_key": safe_account_key,
                "access_credential_configured": bool(access_name),
                "refresh_credential_configured": bool(refresh_name),
            },
        )
        session.commit()
        session.refresh(connection)
        return connection
    except Exception:
        session.rollback()
        raise


def set_connection_status(
    session: Session,
    *,
    connection_id: int,
    status: str,
    actor: AuthenticatedUser,
    error_code: str | None = None,
    error_message: str | None = None,
) -> IntegrationConnection:
    require_permission(actor, "manage_integrations")
    clean_status = str(status or "").strip().lower()
    if clean_status not in _ALLOWED_STATUSES:
        raise ValidationAppError("Integration status is invalid.")
    connection = session.get(IntegrationConnection, int(connection_id))
    if connection is None:
        raise ValidationAppError("Integration connection was not found.")

    connection.status = clean_status
    connection.last_health_check_at = datetime.now(timezone.utc)
    connection.last_error_code = (
        normalize_text(error_code, field="Error code", max_length=100, allow_newlines=False)
        if error_code
        else None
    )
    connection.last_error_message = (
        normalize_text(error_message, field="Error message", max_length=2_000)
        if error_message
        else None
    )
    if clean_status == "connected":
        connection.last_success_at = datetime.now(timezone.utc)
        connection.last_error_code = None
        connection.last_error_message = None

    log_audit_event(
        session,
        action="integration.status_updated",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="integration_connection",
        resource_id=connection.id,
        outcome="success" if clean_status == "connected" else "warning",
        event_data={"platform": connection.platform, "status": clean_status},
    )
    session.commit()
    session.refresh(connection)
    return connection


def queue_health_check(
    session: Session,
    *,
    connection_id: int,
    actor: AuthenticatedUser,
) -> int:
    require_permission(actor, "manage_integrations")
    connection = session.get(IntegrationConnection, int(connection_id))
    if connection is None:
        raise ValidationAppError("Integration connection was not found.")
    job = enqueue_job(
        session,
        job_type="integration.health_check",
        payload={"connection_id": connection.id, "platform": connection.platform},
        actor=actor,
        priority=70,
        max_attempts=3,
        idempotency_key=f"integration-health:{connection.id}:{datetime.now(timezone.utc):%Y%m%d%H}",
    )
    return job.id


def list_connections(
    session: Session,
    *,
    actor: AuthenticatedUser,
) -> list[IntegrationConnection]:
    require_permission(actor, "manage_integrations")
    return list(
        session.scalars(
            select(IntegrationConnection).order_by(
                IntegrationConnection.platform.asc(),
                IntegrationConnection.display_name.asc(),
            )
        ).all()
    )
