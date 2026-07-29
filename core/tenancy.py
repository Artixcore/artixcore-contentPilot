"""Fail-closed organization and workspace isolation services."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, with_loader_criteria

from core.audit import log_audit_event
from core.auth import AuthenticatedUser, normalize_email
from core.errors import AppError, ConfigurationError, ValidationAppError
from core.security_models import UserAccount
from core.tenant_models import (
    Organization,
    Workspace,
    WorkspaceApiKey,
    WorkspaceInvitation,
    WorkspaceMembership,
)
from core.tenancy_base import TenantScopedMixin

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,98}[a-z0-9]$")
_WORKSPACE_ROLES = frozenset({"owner", "admin", "editor", "reviewer", "viewer"})
_INVITABLE_ROLES = frozenset({"admin", "editor", "reviewer", "viewer"})
_API_SCOPES = frozenset(
    {
        "content:read",
        "content:write",
        "content:approve",
        "content:publish",
        "analytics:read",
        "integrations:read",
        "integrations:write",
        "webhooks:write",
        "workspace:read",
        "workspace:admin",
    }
)
_ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    "owner": frozenset({"*"}),
    "admin": frozenset(
        {
            "workspace:read",
            "workspace:admin",
            "members:manage",
            "content:read",
            "content:write",
            "content:approve",
            "content:publish",
            "analytics:read",
            "integrations:read",
            "integrations:write",
            "api_keys:manage",
        }
    ),
    "editor": frozenset(
        {
            "workspace:read",
            "content:read",
            "content:write",
            "analytics:read",
            "integrations:read",
        }
    ),
    "reviewer": frozenset(
        {"workspace:read", "content:read", "content:approve", "analytics:read"}
    ),
    "viewer": frozenset({"workspace:read", "content:read", "analytics:read"}),
}


class WorkspaceAccessError(AppError):
    default_error_code = "WORKSPACE_ACCESS_DENIED"
    default_user_action = "Select an authorized workspace or ask an owner for access."
    retryable_default = False


@dataclass(frozen=True)
class WorkspaceContext:
    workspace_id: int
    organization_id: int
    workspace_name: str
    organization_name: str
    role: str
    user_id: int

    def can(self, permission: str) -> bool:
        permissions = _ROLE_PERMISSIONS.get(self.role, frozenset())
        return "*" in permissions or permission in permissions


@dataclass(frozen=True)
class ApiKeyResult:
    api_key: str
    model: WorkspaceApiKey


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _slug(value: object, *, field: str) -> str:
    raw = str(value or "").strip().lower()
    normalized = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    normalized = re.sub(r"-+", "-", normalized)
    if len(normalized) < 3 or len(normalized) > 100 or not _SLUG_RE.fullmatch(normalized):
        raise ValidationAppError(
            f"{field} slug must contain 3 to 100 lowercase letters, numbers, or hyphens."
        )
    return normalized


def _name(value: object, *, field: str) -> str:
    clean = " ".join(str(value or "").strip().split())
    if not 2 <= len(clean) <= 255:
        raise ValidationAppError(f"{field} must contain between 2 and 255 characters.")
    return clean


def _workspace_role(value: object, *, invitational: bool = False) -> str:
    role = str(value or "").strip().lower()
    allowed = _INVITABLE_ROLES if invitational else _WORKSPACE_ROLES
    if role not in allowed:
        raise ValidationAppError("Select a valid workspace role.")
    return role


def _safe_json(value: Any, *, max_length: int = 20_000) -> str:
    try:
        serialized = json.dumps(value if value is not None else {}, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValidationAppError("Workspace settings must be JSON serializable.") from exc
    if len(serialized) > max_length:
        raise ValidationAppError("Workspace settings are too large.")
    return serialized


def require_workspace_permission(context: WorkspaceContext | None, permission: str) -> None:
    if context is None:
        raise WorkspaceAccessError("An active workspace is required.")
    if not context.can(permission):
        raise WorkspaceAccessError("Your workspace role does not allow this action.")


def set_session_workspace(
    session: Session,
    workspace: WorkspaceContext | int | None,
    *,
    tenant_bypass: bool = False,
) -> Session:
    """Attach the active workspace to a SQLAlchemy session."""
    session.info["tenant_bypass"] = bool(tenant_bypass)
    if isinstance(workspace, WorkspaceContext):
        session.info["workspace_id"] = int(workspace.workspace_id)
        session.info["workspace_context"] = workspace
    elif workspace is not None:
        session.info["workspace_id"] = int(workspace)
        session.info.pop("workspace_context", None)
    else:
        session.info.pop("workspace_id", None)
        session.info.pop("workspace_context", None)
    return session


def bootstrap_default_tenant(
    session: Session,
    owner: AuthenticatedUser,
) -> WorkspaceContext:
    """Create the initial organization and workspace without duplicating records."""
    session.info["tenant_bypass"] = True
    organization = session.scalar(select(Organization).order_by(Organization.id.asc()).limit(1))
    if organization is None:
        organization = Organization(
            name=os.getenv("BOOTSTRAP_ORGANIZATION_NAME", "Artixcore").strip() or "Artixcore",
            slug=_slug(os.getenv("BOOTSTRAP_ORGANIZATION_SLUG", "artixcore"), field="Organization"),
            owner_user_id=owner.id,
            billing_owner_user_id=owner.id,
            status="active",
            plan_code="starter",
        )
        session.add(organization)
        session.flush()

    workspace = session.scalar(
        select(Workspace)
        .where(Workspace.organization_id == organization.id)
        .order_by(Workspace.id.asc())
        .limit(1)
    )
    if workspace is None:
        workspace = Workspace(
            organization_id=organization.id,
            name=os.getenv("BOOTSTRAP_WORKSPACE_NAME", "Artixcore").strip() or "Artixcore",
            slug=_slug(os.getenv("BOOTSTRAP_WORKSPACE_SLUG", "artixcore"), field="Workspace"),
            timezone=os.getenv("BUSINESS_TIMEZONE", "Asia/Dhaka")[:64],
            locale=os.getenv("BUSINESS_LOCALE", "en-BD")[:32],
            default_language=os.getenv("BUSINESS_LANGUAGE", "English")[:64],
        )
        session.add(workspace)
        session.flush()

    membership = session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace.id,
            WorkspaceMembership.user_id == owner.id,
        )
    )
    if membership is None:
        membership = WorkspaceMembership(
            workspace_id=workspace.id,
            user_id=owner.id,
            role="owner",
            status="active",
            invited_by_user_id=owner.id,
        )
        session.add(membership)

    log_audit_event(
        session,
        action="tenant.bootstrap",
        actor_user_id=owner.id,
        actor_email=owner.email,
        resource_type="workspace",
        resource_id=workspace.id,
        event_data={"organization_id": organization.id, "workspace_id": workspace.id},
    )
    session.commit()
    session.info["tenant_bypass"] = False
    return WorkspaceContext(
        workspace_id=workspace.id,
        organization_id=organization.id,
        workspace_name=workspace.name,
        organization_name=organization.name,
        role="owner",
        user_id=owner.id,
    )


def list_accessible_workspaces(
    session: Session,
    user: AuthenticatedUser,
) -> list[WorkspaceContext]:
    """List only active workspaces where the user has active membership."""
    session.info["tenant_bypass"] = True
    rows = session.execute(
        select(Workspace, Organization, WorkspaceMembership)
        .join(Organization, Organization.id == Workspace.organization_id)
        .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
        .where(
            WorkspaceMembership.user_id == user.id,
            WorkspaceMembership.status == "active",
            Workspace.status == "active",
            Organization.status == "active",
        )
        .order_by(Organization.name.asc(), Workspace.name.asc())
    ).all()
    session.info["tenant_bypass"] = False
    return [
        WorkspaceContext(
            workspace_id=workspace.id,
            organization_id=organization.id,
            workspace_name=workspace.name,
            organization_name=organization.name,
            role=membership.role,
            user_id=user.id,
        )
        for workspace, organization, membership in rows
    ]


def resolve_workspace(
    session: Session,
    user: AuthenticatedUser,
    requested_workspace_id: int | None = None,
) -> WorkspaceContext:
    contexts = list_accessible_workspaces(session, user)
    if not contexts:
        if user.role == "owner":
            return bootstrap_default_tenant(session, user)
        raise WorkspaceAccessError("This account has no active workspace membership.")
    if requested_workspace_id is None:
        return contexts[0]
    for context in contexts:
        if context.workspace_id == int(requested_workspace_id):
            return context
    raise WorkspaceAccessError("The selected workspace is unavailable.")


def create_organization(
    session: Session,
    *,
    actor: AuthenticatedUser,
    name: str,
    slug: str,
    workspace_name: str | None = None,
    workspace_slug: str | None = None,
) -> WorkspaceContext:
    if actor.role not in {"owner", "super_admin"}:
        raise WorkspaceAccessError("Only an owner or super administrator can create organizations.")
    safe_name = _name(name, field="Organization name")
    safe_slug = _slug(slug, field="Organization")
    safe_workspace_name = _name(workspace_name or safe_name, field="Workspace name")
    safe_workspace_slug = _slug(workspace_slug or safe_slug, field="Workspace")

    session.info["tenant_bypass"] = True
    if session.scalar(select(Organization.id).where(Organization.slug == safe_slug)):
        raise ValidationAppError("An organization with this slug already exists.")

    organization = Organization(
        name=safe_name,
        slug=safe_slug,
        owner_user_id=actor.id,
        billing_owner_user_id=actor.id,
        status="active",
        plan_code="starter",
    )
    session.add(organization)
    session.flush()
    workspace = Workspace(
        organization_id=organization.id,
        name=safe_workspace_name,
        slug=safe_workspace_slug,
        timezone=os.getenv("BUSINESS_TIMEZONE", "Asia/Dhaka")[:64],
        locale=os.getenv("BUSINESS_LOCALE", "en-BD")[:32],
    )
    session.add(workspace)
    session.flush()
    session.add(
        WorkspaceMembership(
            workspace_id=workspace.id,
            user_id=actor.id,
            role="owner",
            status="active",
            invited_by_user_id=actor.id,
        )
    )
    log_audit_event(
        session,
        action="organization.created",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="organization",
        resource_id=organization.id,
        event_data={"workspace_id": workspace.id, "slug": safe_slug},
    )
    session.commit()
    session.info["tenant_bypass"] = False
    return WorkspaceContext(
        workspace_id=workspace.id,
        organization_id=organization.id,
        workspace_name=workspace.name,
        organization_name=organization.name,
        role="owner",
        user_id=actor.id,
    )


def create_workspace(
    session: Session,
    *,
    actor: AuthenticatedUser,
    organization_id: int,
    name: str,
    slug: str,
    timezone_name: str = "Asia/Dhaka",
    locale: str = "en-BD",
) -> WorkspaceContext:
    session.info["tenant_bypass"] = True
    organization = session.get(Organization, int(organization_id))
    if organization is None or organization.status != "active":
        raise ValidationAppError("Organization was not found or is inactive.")
    if organization.owner_user_id != actor.id and actor.role not in {"owner", "super_admin"}:
        raise WorkspaceAccessError("You cannot create a workspace for this organization.")
    safe_name = _name(name, field="Workspace name")
    safe_slug = _slug(slug, field="Workspace")
    duplicate = session.scalar(
        select(Workspace.id).where(
            Workspace.organization_id == organization.id,
            Workspace.slug == safe_slug,
        )
    )
    if duplicate:
        raise ValidationAppError("This workspace slug is already used in the organization.")
    workspace = Workspace(
        organization_id=organization.id,
        name=safe_name,
        slug=safe_slug,
        timezone=str(timezone_name or "Asia/Dhaka")[:64],
        locale=str(locale or "en-BD")[:32],
    )
    session.add(workspace)
    session.flush()
    session.add(
        WorkspaceMembership(
            workspace_id=workspace.id,
            user_id=actor.id,
            role="owner",
            status="active",
            invited_by_user_id=actor.id,
        )
    )
    log_audit_event(
        session,
        action="workspace.created",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="workspace",
        resource_id=workspace.id,
        event_data={"organization_id": organization.id, "slug": safe_slug},
    )
    session.commit()
    session.info["tenant_bypass"] = False
    return WorkspaceContext(
        workspace_id=workspace.id,
        organization_id=organization.id,
        workspace_name=workspace.name,
        organization_name=organization.name,
        role="owner",
        user_id=actor.id,
    )


def invite_member(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    email: str,
    role: str,
    expires_hours: int = 72,
) -> str:
    require_workspace_permission(context, "members:manage")
    normalized_email = normalize_email(email)
    safe_role = _workspace_role(role, invitational=True)
    hours = min(max(int(expires_hours), 1), 168)
    raw_token = secrets.token_urlsafe(40)
    token_hash = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    existing_user = session.scalar(
        select(UserAccount).where(func.lower(UserAccount.email) == normalized_email).limit(1)
    )
    if existing_user:
        existing_membership = session.scalar(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == context.workspace_id,
                WorkspaceMembership.user_id == existing_user.id,
            )
        )
        if existing_membership and existing_membership.status == "active":
            raise ValidationAppError("This user is already an active workspace member.")

    pending = session.scalars(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.workspace_id == context.workspace_id,
            WorkspaceInvitation.email == normalized_email,
            WorkspaceInvitation.status == "pending",
        )
    ).all()
    for invitation in pending:
        invitation.status = "revoked"

    invitation = WorkspaceInvitation(
        workspace_id=context.workspace_id,
        email=normalized_email,
        role=safe_role,
        token_hash=token_hash,
        status="pending",
        invited_by_user_id=actor.id,
        expires_at=_utc_now() + timedelta(hours=hours),
    )
    session.add(invitation)
    log_audit_event(
        session,
        action="workspace.invitation_created",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="workspace",
        resource_id=context.workspace_id,
        event_data={"email": normalized_email, "role": safe_role},
    )
    session.commit()
    return raw_token


def accept_invitation(
    session: Session,
    *,
    user: AuthenticatedUser,
    raw_token: str,
) -> WorkspaceContext:
    token = str(raw_token or "").strip()
    if len(token) < 32 or len(token) > 200:
        raise ValidationAppError("Invitation token is invalid.")
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    session.info["tenant_bypass"] = True
    invitation = session.scalar(
        select(WorkspaceInvitation).where(
            WorkspaceInvitation.token_hash == token_hash,
            WorkspaceInvitation.status == "pending",
        )
    )
    if invitation is None:
        raise WorkspaceAccessError("Invitation is invalid, expired, or already used.")
    if (_aware(invitation.expires_at) or _utc_now()) <= _utc_now():
        invitation.status = "expired"
        session.commit()
        raise WorkspaceAccessError("Invitation has expired.")
    if normalize_email(invitation.email) != normalize_email(user.email):
        raise WorkspaceAccessError("Invitation belongs to a different account.")

    membership = session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == invitation.workspace_id,
            WorkspaceMembership.user_id == user.id,
        )
    )
    if membership is None:
        membership = WorkspaceMembership(
            workspace_id=invitation.workspace_id,
            user_id=user.id,
            role=invitation.role,
            status="active",
            invited_by_user_id=invitation.invited_by_user_id,
        )
        session.add(membership)
    else:
        membership.role = invitation.role
        membership.status = "active"
    invitation.status = "accepted"
    invitation.accepted_at = _utc_now()
    log_audit_event(
        session,
        action="workspace.invitation_accepted",
        actor_user_id=user.id,
        actor_email=user.email,
        resource_type="workspace",
        resource_id=invitation.workspace_id,
        event_data={"role": invitation.role},
    )
    session.commit()
    return resolve_workspace(session, user, invitation.workspace_id)


def set_membership_role(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    membership_id: int,
    role: str,
) -> WorkspaceMembership:
    require_workspace_permission(context, "members:manage")
    safe_role = _workspace_role(role)
    membership = session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.id == int(membership_id),
            WorkspaceMembership.workspace_id == context.workspace_id,
        )
    )
    if membership is None:
        raise ValidationAppError("Workspace membership was not found.")
    if membership.role == "owner" and safe_role != "owner":
        owner_count = int(
            session.scalar(
                select(func.count(WorkspaceMembership.id)).where(
                    WorkspaceMembership.workspace_id == context.workspace_id,
                    WorkspaceMembership.role == "owner",
                    WorkspaceMembership.status == "active",
                )
            )
            or 0
        )
        if owner_count <= 1:
            raise WorkspaceAccessError("A workspace must keep at least one active owner.")
    if safe_role == "owner" and context.role != "owner":
        raise WorkspaceAccessError("Only a workspace owner can promote another owner.")
    membership.role = safe_role
    log_audit_event(
        session,
        action="workspace.membership_role_changed",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="workspace_membership",
        resource_id=membership.id,
        event_data={"workspace_id": context.workspace_id, "role": safe_role},
    )
    session.commit()
    session.refresh(membership)
    return membership


def remove_member(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    membership_id: int,
) -> None:
    require_workspace_permission(context, "members:manage")
    membership = session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.id == int(membership_id),
            WorkspaceMembership.workspace_id == context.workspace_id,
        )
    )
    if membership is None:
        raise ValidationAppError("Workspace membership was not found.")
    if membership.user_id == actor.id:
        raise WorkspaceAccessError("Use ownership transfer before removing your own membership.")
    if membership.role == "owner":
        owner_count = int(
            session.scalar(
                select(func.count(WorkspaceMembership.id)).where(
                    WorkspaceMembership.workspace_id == context.workspace_id,
                    WorkspaceMembership.role == "owner",
                    WorkspaceMembership.status == "active",
                )
            )
            or 0
        )
        if owner_count <= 1:
            raise WorkspaceAccessError("The final active workspace owner cannot be removed.")
    membership.status = "suspended"
    log_audit_event(
        session,
        action="workspace.member_suspended",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="workspace_membership",
        resource_id=membership.id,
        event_data={"workspace_id": context.workspace_id, "user_id": membership.user_id},
    )
    session.commit()


def _api_pepper() -> bytes:
    pepper = os.getenv("WORKSPACE_API_KEY_PEPPER", "").strip()
    if len(pepper) < 32:
        raise ConfigurationError(
            "WORKSPACE_API_KEY_PEPPER must contain at least 32 characters.",
            user_action="Set a strong random API-key pepper in deployment secrets.",
        )
    return pepper.encode("utf-8")


def _hash_api_key(raw_key: str) -> str:
    return hmac.new(_api_pepper(), raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


def create_workspace_api_key(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    name: str,
    scopes: list[str],
    expires_days: int | None = None,
) -> ApiKeyResult:
    require_workspace_permission(context, "api_keys:manage")
    safe_name = _name(name, field="API key name")[:100]
    safe_scopes = sorted({str(scope).strip().lower() for scope in scopes})
    if not safe_scopes or any(scope not in _API_SCOPES for scope in safe_scopes):
        raise ValidationAppError("Select at least one valid API-key scope.")
    duplicate = session.scalar(
        select(WorkspaceApiKey.id).where(
            WorkspaceApiKey.workspace_id == context.workspace_id,
            WorkspaceApiKey.name == safe_name,
        )
    )
    if duplicate:
        raise ValidationAppError("An API key with this name already exists in the workspace.")
    prefix = f"cp_{secrets.token_hex(4)}"
    raw_key = f"{prefix}_{secrets.token_urlsafe(36)}"
    model = WorkspaceApiKey(
        workspace_id=context.workspace_id,
        name=safe_name,
        key_prefix=prefix,
        key_hash=_hash_api_key(raw_key),
        scopes_json=json.dumps(safe_scopes, separators=(",", ":")),
        is_active=True,
        created_by_user_id=actor.id,
        expires_at=(
            _utc_now() + timedelta(days=min(max(int(expires_days), 1), 3650))
            if expires_days
            else None
        ),
    )
    session.add(model)
    session.flush()
    log_audit_event(
        session,
        action="workspace.api_key_created",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="workspace_api_key",
        resource_id=model.id,
        event_data={"workspace_id": context.workspace_id, "name": safe_name, "scopes": safe_scopes},
    )
    session.commit()
    session.refresh(model)
    return ApiKeyResult(api_key=raw_key, model=model)


def verify_workspace_api_key(session: Session, raw_key: str, required_scope: str) -> WorkspaceContext:
    key = str(raw_key or "").strip()
    if len(key) < 40 or len(key) > 300 or not key.startswith("cp_"):
        raise WorkspaceAccessError("API key is invalid.")
    prefix = key.rsplit("_", 1)[0][:16]
    key_hash = _hash_api_key(key)
    session.info["tenant_bypass"] = True
    model = session.scalar(
        select(WorkspaceApiKey).where(
            WorkspaceApiKey.key_prefix == prefix,
            WorkspaceApiKey.key_hash == key_hash,
            WorkspaceApiKey.is_active.is_(True),
        )
    )
    if model is None or model.revoked_at is not None:
        raise WorkspaceAccessError("API key is invalid or revoked.")
    if _aware(model.expires_at) and _aware(model.expires_at) <= _utc_now():
        raise WorkspaceAccessError("API key has expired.")
    scopes = set(json.loads(model.scopes_json or "[]"))
    if required_scope not in scopes:
        raise WorkspaceAccessError("API key does not include the required scope.")
    workspace = session.get(Workspace, model.workspace_id)
    organization = session.get(Organization, workspace.organization_id if workspace else 0)
    if workspace is None or organization is None:
        raise WorkspaceAccessError("API key workspace is unavailable.")
    model.last_used_at = _utc_now()
    session.commit()
    session.info["tenant_bypass"] = False
    return WorkspaceContext(
        workspace_id=workspace.id,
        organization_id=organization.id,
        workspace_name=workspace.name,
        organization_name=organization.name,
        role="owner",
        user_id=model.created_by_user_id,
    )


def revoke_workspace_api_key(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    key_id: int,
) -> None:
    require_workspace_permission(context, "api_keys:manage")
    model = session.scalar(
        select(WorkspaceApiKey).where(
            WorkspaceApiKey.id == int(key_id),
            WorkspaceApiKey.workspace_id == context.workspace_id,
        )
    )
    if model is None:
        raise ValidationAppError("API key was not found.")
    model.is_active = False
    model.revoked_at = _utc_now()
    log_audit_event(
        session,
        action="workspace.api_key_revoked",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="workspace_api_key",
        resource_id=model.id,
        event_data={"workspace_id": context.workspace_id, "name": model.name},
    )
    session.commit()


def _scope_orm_execute(execute_state) -> None:
    if not execute_state.is_select or execute_state.session.info.get("tenant_bypass"):
        return
    workspace_id = execute_state.session.info.get("workspace_id")
    effective_workspace_id = int(workspace_id) if workspace_id is not None else -1
    execute_state.statement = execute_state.statement.options(
        with_loader_criteria(
            TenantScopedMixin,
            lambda cls: cls.workspace_id == effective_workspace_id,
            include_aliases=True,
        )
    )


def _validate_tenant_flush(session: Session, _flush_context, _instances) -> None:
    if session.info.get("tenant_bypass"):
        return
    workspace_id = session.info.get("workspace_id")
    for collection, action in (
        (session.new, "create"),
        (session.dirty, "modify"),
        (session.deleted, "delete"),
    ):
        for obj in collection:
            if not isinstance(obj, TenantScopedMixin):
                continue
            object_workspace_id = getattr(obj, "workspace_id", None)
            if workspace_id is None:
                raise WorkspaceAccessError(
                    f"Cannot {action} workspace-scoped data without an active workspace."
                )
            if object_workspace_id is None and action == "create":
                setattr(obj, "workspace_id", int(workspace_id))
            elif int(object_workspace_id or -1) != int(workspace_id):
                raise WorkspaceAccessError(
                    f"Cannot {action} data belonging to another workspace."
                )


def install_tenant_session_hooks() -> None:
    """Install automatic read filters and write guards once per process."""
    if getattr(Session, "_contentpilot_tenant_hooks", False):
        return
    event.listen(Session, "do_orm_execute", _scope_orm_execute)
    event.listen(Session, "before_flush", _validate_tenant_flush)
    setattr(Session, "_contentpilot_tenant_hooks", True)
