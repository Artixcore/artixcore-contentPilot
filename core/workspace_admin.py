"""Workspace administration queries and safe settings updates."""

from __future__ import annotations

import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.audit import log_audit_event
from core.auth import AuthenticatedUser
from core.errors import ValidationAppError
from core.security_models import UserAccount
from core.tenant_models import (
    Workspace,
    WorkspaceApiKey,
    WorkspaceInvitation,
    WorkspaceMembership,
)
from core.tenancy import WorkspaceContext, require_workspace_permission


def list_workspace_members(
    session: Session,
    *,
    context: WorkspaceContext,
) -> list[tuple[WorkspaceMembership, UserAccount]]:
    require_workspace_permission(context, "workspace:read")
    return list(
        session.execute(
            select(WorkspaceMembership, UserAccount)
            .join(UserAccount, UserAccount.id == WorkspaceMembership.user_id)
            .where(WorkspaceMembership.workspace_id == context.workspace_id)
            .order_by(WorkspaceMembership.role.asc(), UserAccount.display_name.asc())
        ).all()
    )


def list_pending_invitations(
    session: Session,
    *,
    context: WorkspaceContext,
) -> list[WorkspaceInvitation]:
    require_workspace_permission(context, "members:manage")
    return list(
        session.scalars(
            select(WorkspaceInvitation)
            .where(
                WorkspaceInvitation.workspace_id == context.workspace_id,
                WorkspaceInvitation.status == "pending",
            )
            .order_by(WorkspaceInvitation.created_at.desc())
        ).all()
    )


def revoke_invitation(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    invitation_id: int,
) -> None:
    require_workspace_permission(context, "members:manage")
    invitation = session.scalar(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.id == int(invitation_id),
            WorkspaceInvitation.workspace_id == context.workspace_id,
        )
    )
    if invitation is None or invitation.status != "pending":
        raise ValidationAppError("Pending invitation was not found.")
    invitation.status = "revoked"
    log_audit_event(
        session,
        action="workspace.invitation_revoked",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="workspace_invitation",
        resource_id=invitation.id,
        event_data={"workspace_id": context.workspace_id, "email": invitation.email},
    )
    session.commit()


def list_workspace_api_keys(
    session: Session,
    *,
    context: WorkspaceContext,
) -> list[WorkspaceApiKey]:
    require_workspace_permission(context, "api_keys:manage")
    return list(
        session.scalars(
            select(WorkspaceApiKey)
            .where(WorkspaceApiKey.workspace_id == context.workspace_id)
            .order_by(WorkspaceApiKey.created_at.desc())
        ).all()
    )


def update_workspace_settings(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    name: str,
    timezone_name: str,
    locale: str,
    default_language: str,
    usage_limit_monthly: int,
    settings: dict | None = None,
) -> Workspace:
    require_workspace_permission(context, "workspace:admin")
    clean_name = " ".join(str(name or "").strip().split())
    if not 2 <= len(clean_name) <= 255:
        raise ValidationAppError("Workspace name must contain between 2 and 255 characters.")
    clean_timezone = str(timezone_name or "").strip()
    try:
        ZoneInfo(clean_timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValidationAppError("Select a valid IANA timezone.") from exc
    clean_locale = str(locale or "").strip()
    if not 2 <= len(clean_locale) <= 32 or not all(
        character.isalnum() or character in {"-", "_"} for character in clean_locale
    ):
        raise ValidationAppError("Locale contains unsupported characters.")
    clean_language = " ".join(str(default_language or "").strip().split())
    if not 2 <= len(clean_language) <= 64:
        raise ValidationAppError("Default language must contain between 2 and 64 characters.")
    safe_limit = int(usage_limit_monthly)
    if not 0 <= safe_limit <= 100_000_000:
        raise ValidationAppError("Monthly usage limit is outside the supported range.")
    try:
        settings_json = json.dumps(settings or {}, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValidationAppError("Workspace settings must be JSON serializable.") from exc
    if len(settings_json) > 20_000:
        raise ValidationAppError("Workspace settings are too large.")

    workspace = session.get(Workspace, context.workspace_id)
    if workspace is None:
        raise ValidationAppError("Workspace was not found.")
    workspace.name = clean_name
    workspace.timezone = clean_timezone
    workspace.locale = clean_locale
    workspace.default_language = clean_language
    workspace.usage_limit_monthly = safe_limit
    workspace.settings_json = settings_json
    log_audit_event(
        session,
        action="workspace.settings_updated",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="workspace",
        resource_id=workspace.id,
        event_data={
            "timezone": clean_timezone,
            "locale": clean_locale,
            "usage_limit_monthly": safe_limit,
        },
    )
    session.commit()
    session.refresh(workspace)
    return workspace
