"""Regression tests for organization, workspace, membership, and tenant isolation."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from core.auth import ROLE_OWNER, ROLE_VIEWER, create_user
from core.models import Post
from core.tenant_migration import backfill_legacy_workspace, verify_tenant_integrity
from core.tenant_models import WorkspaceMembership
from core.tenant_runtime import bind_workspace
from core.tenancy import (
    WorkspaceAccessError,
    accept_invitation,
    bootstrap_default_tenant,
    create_organization,
    invite_member,
    list_accessible_workspaces,
    resolve_workspace,
)
from core.workspace_api_keys import create_workspace_api_key, verify_workspace_api_key


def _owner(db_session):
    return create_user(
        db_session,
        email="tenant-owner@example.com",
        display_name="Tenant Owner",
        password="TenantOwnerSecure!9274",
        role=ROLE_OWNER,
    )


def _bootstrap(db_session):
    owner = _owner(db_session)
    context = bootstrap_default_tenant(db_session, owner)
    backfill_legacy_workspace(db_session, context.workspace_id)
    bind_workspace(db_session, context)
    return owner, context


def test_workspace_tables_and_default_membership(db_session):
    owner, context = _bootstrap(db_session)
    verify_tenant_integrity(db_session)
    contexts = list_accessible_workspaces(db_session, owner)
    assert [item.workspace_id for item in contexts] == [context.workspace_id]
    membership = db_session.scalar(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == context.workspace_id,
            WorkspaceMembership.user_id == owner.id,
        )
    )
    assert membership is not None
    assert membership.role == "owner"


def test_automatic_assignment_and_read_isolation(db_session):
    owner, first = _bootstrap(db_session)
    post = Post(platform="linkedin", topic="Workspace one", status="draft")
    db_session.add(post)
    db_session.commit()
    assert post.workspace_id == first.workspace_id

    second = create_organization(
        db_session,
        actor=owner,
        name="Second Organization",
        slug="second-organization",
        workspace_name="Second Workspace",
        workspace_slug="second-workspace",
    )
    bind_workspace(db_session, second)
    assert db_session.scalar(select(Post).where(Post.id == post.id)) is None

    second_post = Post(platform="facebook", topic="Workspace two", status="draft")
    db_session.add(second_post)
    db_session.commit()
    assert second_post.workspace_id == second.workspace_id

    bind_workspace(db_session, first)
    visible = list(db_session.scalars(select(Post).order_by(Post.id)).all())
    assert [item.id for item in visible] == [post.id]


def test_cross_workspace_update_is_blocked(db_session):
    owner, first = _bootstrap(db_session)
    post = Post(platform="linkedin", topic="Protected", status="draft")
    db_session.add(post)
    db_session.commit()

    second = create_organization(
        db_session,
        actor=owner,
        name="Isolation Organization",
        slug="isolation-organization",
        workspace_name="Isolation Workspace",
        workspace_slug="isolation-workspace",
    )
    db_session.info["tenant_bypass"] = True
    loaded = db_session.get(Post, post.id)
    db_session.info["tenant_bypass"] = False
    bind_workspace(db_session, second)
    loaded.topic = "Forbidden cross-tenant change"
    with pytest.raises(WorkspaceAccessError):
        db_session.commit()
    db_session.rollback()

    bind_workspace(db_session, first)
    original = db_session.get(Post, post.id)
    assert original.topic == "Protected"


def test_invitation_acceptance_and_workspace_resolution(db_session):
    owner, context = _bootstrap(db_session)
    viewer = create_user(
        db_session,
        email="tenant-viewer@example.com",
        display_name="Tenant Viewer",
        password="TenantViewerSecure!9274",
        role=ROLE_VIEWER,
        actor=owner,
    )
    token = invite_member(
        db_session,
        context=context,
        actor=owner,
        email=viewer.email,
        role="viewer",
    )
    accepted = accept_invitation(db_session, user=viewer, raw_token=token)
    assert accepted.workspace_id == context.workspace_id
    assert accepted.role == "viewer"
    assert resolve_workspace(db_session, viewer, context.workspace_id).workspace_id == context.workspace_id


def test_workspace_api_keys_are_hashed_scoped_and_authorized(db_session):
    owner, context = _bootstrap(db_session)
    result = create_workspace_api_key(
        db_session,
        context=context,
        actor=owner,
        name="Automation",
        scopes=["content:read", "content:write"],
        expires_days=30,
    )
    assert "." in result.api_key
    assert result.api_key not in result.model.key_hash
    resolved = verify_workspace_api_key(db_session, result.api_key, "content:read")
    assert resolved.workspace_id == context.workspace_id
    with pytest.raises(WorkspaceAccessError):
        verify_workspace_api_key(db_session, result.api_key, "workspace:admin")
