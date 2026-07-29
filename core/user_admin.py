"""Administrative user lifecycle operations with owner safeguards and audit logging."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.audit import log_audit_event
from core.auth import (
    ROLE_OWNER,
    AuthenticatedUser,
    AuthorizationError,
    _to_authenticated,
    hash_password,
    require_permission,
    validate_password,
    validate_role,
)
from core.errors import ValidationAppError
from core.security_models import AuthSession, UserAccount


def list_users(session: Session, actor: AuthenticatedUser) -> list[UserAccount]:
    require_permission(actor, "manage_users")
    return list(session.scalars(select(UserAccount).order_by(UserAccount.created_at.asc())).all())


def _active_owner_count(session: Session) -> int:
    return int(
        session.scalar(
            select(func.count(UserAccount.id)).where(
                UserAccount.role == ROLE_OWNER,
                UserAccount.is_active.is_(True),
            )
        )
        or 0
    )


def update_user_role(
    session: Session,
    *,
    user_id: int,
    role: str,
    actor: AuthenticatedUser,
) -> AuthenticatedUser:
    require_permission(actor, "manage_users")
    target = session.get(UserAccount, int(user_id))
    if target is None:
        raise ValidationAppError("User account was not found.")
    new_role = validate_role(role)
    if new_role == ROLE_OWNER and actor.role != ROLE_OWNER:
        raise AuthorizationError("Only an owner can assign the owner role.")
    if target.role == ROLE_OWNER and new_role != ROLE_OWNER and _active_owner_count(session) <= 1:
        raise ValidationAppError("The final active owner cannot be demoted.")

    previous = target.role
    target.role = new_role
    log_audit_event(
        session,
        action="user.role_updated",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="user",
        resource_id=target.id,
        event_data={"from": previous, "to": new_role},
    )
    session.commit()
    session.refresh(target)
    return _to_authenticated(target)


def set_user_active(
    session: Session,
    *,
    user_id: int,
    active: bool,
    actor: AuthenticatedUser,
) -> AuthenticatedUser:
    require_permission(actor, "manage_users")
    target = session.get(UserAccount, int(user_id))
    if target is None:
        raise ValidationAppError("User account was not found.")
    if target.id == actor.id and not active:
        raise ValidationAppError("You cannot deactivate your own account.")
    if target.role == ROLE_OWNER and target.is_active and not active and _active_owner_count(session) <= 1:
        raise ValidationAppError("The final active owner cannot be deactivated.")

    target.is_active = bool(active)
    if not target.is_active:
        for auth_session in session.scalars(
            select(AuthSession).where(
                AuthSession.user_id == target.id,
                AuthSession.revoked_at.is_(None),
            )
        ):
            from datetime import datetime, timezone

            auth_session.revoked_at = datetime.now(timezone.utc)
            auth_session.revoke_reason = "account_deactivated"

    log_audit_event(
        session,
        action="user.status_updated",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="user",
        resource_id=target.id,
        event_data={"active": target.is_active},
    )
    session.commit()
    session.refresh(target)
    return _to_authenticated(target)


def reset_user_password(
    session: Session,
    *,
    user_id: int,
    new_password: str,
    actor: AuthenticatedUser,
) -> None:
    require_permission(actor, "manage_users")
    target = session.get(UserAccount, int(user_id))
    if target is None:
        raise ValidationAppError("User account was not found.")
    if target.role == ROLE_OWNER and actor.role != ROLE_OWNER and target.id != actor.id:
        raise AuthorizationError("Only an owner can reset another owner's password.")

    target.password_hash = hash_password(validate_password(new_password, email=target.email))
    from datetime import datetime, timezone

    target.password_changed_at = datetime.now(timezone.utc)
    for auth_session in session.scalars(
        select(AuthSession).where(
            AuthSession.user_id == target.id,
            AuthSession.revoked_at.is_(None),
        )
    ):
        auth_session.revoked_at = datetime.now(timezone.utc)
        auth_session.revoke_reason = "password_reset"

    log_audit_event(
        session,
        action="user.password_reset",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="user",
        resource_id=target.id,
    )
    session.commit()
