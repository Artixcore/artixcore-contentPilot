"""Self-service account security and audit review operations."""

from __future__ import annotations

from datetime import datetime, timezone

import pyotp
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.audit import log_audit_event
from core.auth import (
    AuthenticatedUser,
    AuthenticationError,
    AuthorizationError,
    hash_password,
    validate_password,
    verify_password,
)
from core.encryption import decrypt_text
from core.security_models import AuditEvent, AuthSession, UserAccount


def change_own_password(
    session: Session,
    *,
    user: AuthenticatedUser,
    current_password: str,
    new_password: str,
) -> None:
    model = session.get(UserAccount, user.id)
    if model is None or not model.is_active or not verify_password(model.password_hash, current_password):
        raise AuthenticationError("Current password is incorrect.")
    validated = validate_password(new_password, email=model.email)
    if verify_password(model.password_hash, validated):
        raise AuthenticationError("New password must be different from the current password.")

    now = datetime.now(timezone.utc)
    model.password_hash = hash_password(validated)
    model.password_changed_at = now
    for auth_session in session.scalars(
        select(AuthSession).where(
            AuthSession.user_id == model.id,
            AuthSession.revoked_at.is_(None),
        )
    ):
        auth_session.revoked_at = now
        auth_session.revoke_reason = "password_changed"
    log_audit_event(
        session,
        action="auth.password_changed",
        actor_user_id=user.id,
        actor_email=user.email,
        resource_type="user",
        resource_id=user.id,
    )
    session.commit()


def disable_mfa(
    session: Session,
    *,
    user: AuthenticatedUser,
    password: str,
    totp_code: str,
) -> None:
    model = session.get(UserAccount, user.id)
    if model is None or not model.is_active or not verify_password(model.password_hash, password):
        raise AuthenticationError("Password or authentication code is invalid.")
    if not model.mfa_enabled or not model.mfa_secret_encrypted:
        raise AuthenticationError("MFA is not enabled for this account.")

    secret = decrypt_text(
        model.mfa_secret_encrypted,
        associated_context=f"user:{model.id}:mfa",
    )
    if not pyotp.TOTP(secret).verify(str(totp_code or "").strip(), valid_window=1):
        raise AuthenticationError("Password or authentication code is invalid.")

    model.mfa_enabled = False
    model.mfa_secret_encrypted = None
    log_audit_event(
        session,
        action="auth.mfa_disabled",
        actor_user_id=user.id,
        actor_email=user.email,
        resource_type="user",
        resource_id=user.id,
    )
    session.commit()


def list_audit_events(
    session: Session,
    *,
    actor: AuthenticatedUser,
    limit: int = 100,
) -> list[AuditEvent]:
    if not (actor.can("view_audit") or actor.can("manage_security")):
        raise AuthorizationError("You do not have permission to view audit events.")
    safe_limit = min(max(int(limit), 1), 500)
    return list(
        session.scalars(
            select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(safe_limit)
        ).all()
    )


def purge_expired_sessions(
    session: Session,
    *,
    actor: AuthenticatedUser,
) -> int:
    if not actor.can("manage_security"):
        raise AuthorizationError("You do not have permission to purge sessions.")
    now = datetime.now(timezone.utc)
    expired = list(
        session.scalars(
            select(AuthSession).where(
                AuthSession.expires_at < now,
                AuthSession.revoked_at.is_(None),
            )
        ).all()
    )
    for auth_session in expired:
        auth_session.revoked_at = now
        auth_session.revoke_reason = "expired_session_cleanup"
    log_audit_event(
        session,
        action="auth.expired_sessions_purged",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="auth_session",
        event_data={"count": len(expired)},
    )
    session.commit()
    return len(expired)
