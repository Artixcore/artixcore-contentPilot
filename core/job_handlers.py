"""Explicit background-job handler registry. No dynamic imports or arbitrary code execution."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.errors import ValidationAppError
from core.jobs import JobError
from core.operations_models import IntegrationConnection, SystemNotification
from core.security_models import AuthSession, EncryptedCredential
from core.validation import validate_positive_id

JobHandler = Callable[[Session, dict[str, Any]], dict[str, Any]]


def handle_integration_health_check(
    session: Session,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Check local integration configuration without exposing or decrypting credentials."""
    connection_id = validate_positive_id(payload.get("connection_id"), field="Connection ID")
    connection = session.get(IntegrationConnection, connection_id)
    if connection is None:
        raise JobError("Integration connection was not found.", retryable=False)

    now = datetime.now(timezone.utc)
    connection.last_health_check_at = now
    if connection.status == "disabled":
        return {"connection_id": connection.id, "status": "disabled", "checked": False}

    if connection.token_expires_at:
        expiry = connection.token_expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= now:
            connection.status = "expired"
            connection.last_error_code = "TOKEN_EXPIRED"
            connection.last_error_message = "The stored token metadata indicates that the token has expired."
            session.flush()
            return {"connection_id": connection.id, "status": "expired", "checked": True}

    credential_names = [
        name
        for name in (
            connection.access_credential_name,
            connection.refresh_credential_name,
        )
        if name
    ]
    if not connection.access_credential_name:
        connection.status = "degraded"
        connection.last_error_code = "ACCESS_CREDENTIAL_MISSING"
        connection.last_error_message = "No active access credential is assigned."
        session.flush()
        return {"connection_id": connection.id, "status": "degraded", "checked": True}

    active_credentials = set(
        session.scalars(
            select(EncryptedCredential.credential_name).where(
                EncryptedCredential.credential_name.in_(credential_names),
                EncryptedCredential.is_active.is_(True),
            )
        ).all()
    )
    missing = sorted(set(credential_names) - active_credentials)
    if missing:
        connection.status = "degraded"
        connection.last_error_code = "CREDENTIAL_INACTIVE"
        connection.last_error_message = "One or more assigned credentials are missing or inactive."
        session.flush()
        return {
            "connection_id": connection.id,
            "status": "degraded",
            "checked": True,
            "missing_credentials": missing,
        }

    connection.status = "connected"
    connection.last_success_at = now
    connection.last_error_code = None
    connection.last_error_message = None
    session.flush()
    return {
        "connection_id": connection.id,
        "platform": connection.platform,
        "status": "connected",
        "checked": True,
        "remote_api_checked": False,
    }


def handle_expired_session_cleanup(
    session: Session,
    payload: dict[str, Any],
) -> dict[str, Any]:
    del payload
    now = datetime.now(timezone.utc)
    sessions = list(
        session.scalars(
            select(AuthSession).where(
                AuthSession.expires_at < now,
                AuthSession.revoked_at.is_(None),
            )
        ).all()
    )
    for auth_session in sessions:
        auth_session.revoked_at = now
        auth_session.revoke_reason = "expired_session_cleanup"
    session.flush()
    return {"revoked_sessions": len(sessions)}


def handle_expired_notification_cleanup(
    session: Session,
    payload: dict[str, Any],
) -> dict[str, Any]:
    del payload
    now = datetime.now(timezone.utc)
    notifications = list(
        session.scalars(
            select(SystemNotification).where(
                SystemNotification.expires_at.is_not(None),
                SystemNotification.expires_at < now,
            )
        ).all()
    )
    for notification in notifications:
        session.delete(notification)
    session.flush()
    return {"deleted_notifications": len(notifications)}


JOB_HANDLERS: dict[str, JobHandler] = {
    "integration.health_check": handle_integration_health_check,
    "auth.sessions_cleanup": handle_expired_session_cleanup,
    "notifications.cleanup": handle_expired_notification_cleanup,
}


def execute_registered_job(
    session: Session,
    *,
    job_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    handler = JOB_HANDLERS.get(job_type)
    if handler is None:
        raise JobError(f"No registered handler exists for job type '{job_type}'.", retryable=False)
    if not isinstance(payload, dict):
        raise ValidationAppError("Job payload must be a JSON object.")
    return handler(session, payload)
