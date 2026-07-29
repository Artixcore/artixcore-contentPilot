"""Regression tests for authentication, RBAC, sessions, and owner safeguards."""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, select

from core.auth import (
    ROLE_CONTENT_CREATOR,
    ROLE_OWNER,
    ROLE_VIEWER,
    AuthenticationError,
    AuthorizationError,
    authenticate_user,
    bootstrap_owner,
    create_user,
    logout,
    require_permission,
    resolve_session,
    validate_csrf,
)
from core.errors import ValidationAppError
from core.security_models import AuditEvent, AuthSession, UserAccount
from core.user_admin import set_user_active, update_user_role

_OWNER_PASSWORD = "SecureOwner!9274"
_VIEWER_PASSWORD = "SecureViewer!9274"


def _owner(db_session):
    return create_user(
        db_session,
        email="owner@example.com",
        display_name="Primary Owner",
        password=_OWNER_PASSWORD,
        role=ROLE_OWNER,
    )


def test_security_tables_are_registered(db_session):
    tables = set(inspect(db_session.get_bind()).get_table_names())
    assert {
        "user_accounts",
        "auth_sessions",
        "audit_events",
        "encrypted_credentials",
    }.issubset(tables)


def test_login_session_csrf_and_logout(db_session):
    owner = _owner(db_session)
    tokens = authenticate_user(
        db_session,
        email="OWNER@example.com",
        password=_OWNER_PASSWORD,
        user_agent="pytest-agent",
        ip_address="203.0.113.10",
    )

    assert tokens.user.id == owner.id
    assert tokens.user.role == ROLE_OWNER
    assert validate_csrf(db_session, tokens.session_token, tokens.csrf_token) is True

    resolved = resolve_session(
        db_session,
        tokens.session_token,
        user_agent="pytest-agent",
        ip_address="203.0.113.10",
    )
    assert resolved is not None
    assert resolved.email == "owner@example.com"

    assert (
        resolve_session(
            db_session,
            tokens.session_token,
            user_agent="different-agent",
            ip_address="203.0.113.10",
        )
        is None
    )

    logout(db_session, tokens.session_token, actor=owner)
    assert resolve_session(db_session, tokens.session_token, user_agent="pytest-agent") is None


def test_invalid_credentials_return_generic_error_and_audit(db_session):
    _owner(db_session)
    with pytest.raises(AuthenticationError, match="Invalid email, password"):
        authenticate_user(
            db_session,
            email="owner@example.com",
            password="DefinitelyWrong!123",
        )

    event = db_session.scalar(
        select(AuditEvent).where(AuditEvent.action == "auth.login").order_by(AuditEvent.id.desc())
    )
    assert event is not None
    assert event.outcome == "failure"
    assert "DefinitelyWrong" not in (event.event_data or "")


def test_role_permissions_are_enforced(db_session):
    owner = _owner(db_session)
    viewer = create_user(
        db_session,
        email="viewer@example.com",
        display_name="Viewer",
        password=_VIEWER_PASSWORD,
        role=ROLE_VIEWER,
        actor=owner,
    )

    require_permission(viewer, "read")
    with pytest.raises(AuthorizationError):
        require_permission(viewer, "publish_content")

    updated = update_user_role(
        db_session,
        user_id=viewer.id,
        role=ROLE_CONTENT_CREATOR,
        actor=owner,
    )
    assert updated.can("create_content") is True
    assert updated.can("publish_content") is False


def test_final_owner_cannot_be_demoted_or_deactivated(db_session):
    owner = _owner(db_session)
    with pytest.raises(ValidationAppError, match="final active owner"):
        update_user_role(
            db_session,
            user_id=owner.id,
            role=ROLE_VIEWER,
            actor=owner,
        )
    with pytest.raises(ValidationAppError):
        set_user_active(db_session, user_id=owner.id, active=False, actor=owner)


def test_bootstrap_owner_is_idempotent(db_session, monkeypatch):
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "bootstrap@example.com")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", "BootstrapSecure!9274")
    first = bootstrap_owner(db_session)
    second = bootstrap_owner(db_session)
    assert first is not None
    assert second is not None
    assert first.id == second.id
    assert db_session.query(UserAccount).count() == 1


def test_session_tokens_are_stored_only_as_hashes(db_session):
    _owner(db_session)
    tokens = authenticate_user(
        db_session,
        email="owner@example.com",
        password=_OWNER_PASSWORD,
    )
    stored = db_session.scalar(select(AuthSession).limit(1))
    assert stored is not None
    assert tokens.session_token not in stored.token_hash
    assert len(stored.token_hash) == 64
    assert tokens.csrf_token not in stored.csrf_hash
