"""Hardened workspace API-key creation, verification, and revocation."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.audit import log_audit_event
from core.auth import AuthenticatedUser
from core.errors import ConfigurationError, ValidationAppError
from core.tenant_models import Organization, Workspace, WorkspaceApiKey
from core.tenancy import ApiKeyResult, WorkspaceAccessError, WorkspaceContext, require_workspace_permission

API_SCOPES = frozenset(
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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _pepper() -> bytes:
    value = os.getenv("WORKSPACE_API_KEY_PEPPER", "").strip()
    if len(value) < 32:
        raise ConfigurationError(
            "WORKSPACE_API_KEY_PEPPER must contain at least 32 characters.",
            user_action="Set a strong random API-key pepper in deployment secrets.",
        )
    return value.encode("utf-8")


def _hash(raw_key: str) -> str:
    return hmac.new(_pepper(), raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


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
    clean_name = " ".join(str(name or "").strip().split())
    if not 2 <= len(clean_name) <= 100:
        raise ValidationAppError("API key name must contain between 2 and 100 characters.")
    clean_scopes = sorted({str(scope).strip().lower() for scope in scopes})
    if not clean_scopes or any(scope not in API_SCOPES for scope in clean_scopes):
        raise ValidationAppError("Select at least one valid API-key scope.")
    if session.scalar(
        select(WorkspaceApiKey.id).where(
            WorkspaceApiKey.workspace_id == context.workspace_id,
            WorkspaceApiKey.name == clean_name,
        )
    ):
        raise ValidationAppError("An API key with this name already exists in the workspace.")

    prefix = f"cp_{secrets.token_hex(4)}"
    raw_key = f"{prefix}.{secrets.token_urlsafe(36)}"
    model = WorkspaceApiKey(
        workspace_id=context.workspace_id,
        name=clean_name,
        key_prefix=prefix,
        key_hash=_hash(raw_key),
        scopes_json=json.dumps(clean_scopes, separators=(",", ":")),
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
        event_data={"name": clean_name, "scopes": clean_scopes},
    )
    session.commit()
    session.refresh(model)
    return ApiKeyResult(api_key=raw_key, model=model)


def verify_workspace_api_key(
    session: Session,
    raw_key: str,
    required_scope: str,
) -> WorkspaceContext:
    key = str(raw_key or "").strip()
    if len(key) < 40 or len(key) > 300 or "." not in key:
        raise WorkspaceAccessError("API key is invalid.")
    prefix, _secret = key.split(".", 1)
    if len(prefix) != 11 or not prefix.startswith("cp_"):
        raise WorkspaceAccessError("API key is invalid.")

    supplied_hash = _hash(key)
    session.info["tenant_bypass"] = True
    try:
        candidates = list(
            session.scalars(
                select(WorkspaceApiKey).where(
                    WorkspaceApiKey.key_prefix == prefix,
                    WorkspaceApiKey.is_active.is_(True),
                )
            ).all()
        )
        model = next(
            (
                candidate
                for candidate in candidates
                if hmac.compare_digest(candidate.key_hash, supplied_hash)
            ),
            None,
        )
        if model is None or model.revoked_at is not None:
            raise WorkspaceAccessError("API key is invalid or revoked.")
        expires_at = _aware(model.expires_at)
        if expires_at and expires_at <= _utc_now():
            raise WorkspaceAccessError("API key has expired.")
        try:
            scopes = set(json.loads(model.scopes_json or "[]"))
        except json.JSONDecodeError as exc:
            raise WorkspaceAccessError("API key scopes are invalid.") from exc
        if required_scope not in scopes:
            raise WorkspaceAccessError("API key does not include the required scope.")

        workspace = session.get(Workspace, model.workspace_id)
        organization = session.get(Organization, workspace.organization_id if workspace else 0)
        if workspace is None or organization is None:
            raise WorkspaceAccessError("API key workspace is unavailable.")
        if workspace.status != "active" or organization.status != "active":
            raise WorkspaceAccessError("API key workspace is inactive.")
        model.last_used_at = _utc_now()
        session.commit()
        return WorkspaceContext(
            workspace_id=workspace.id,
            organization_id=organization.id,
            workspace_name=workspace.name,
            organization_name=organization.name,
            role="owner",
            user_id=model.created_by_user_id,
        )
    finally:
        session.info["tenant_bypass"] = False


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
        event_data={"name": model.name},
    )
    session.commit()
