"""Regression tests for the explicit worker handler boundary."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from core.auth import ROLE_OWNER, authenticate_user, create_user
from core.credential_store import store_credential
from core.integrations import upsert_connection
from core.job_handlers import execute_registered_job
from core.jobs import JobError
from core.notifications import create_notification
from core.operations_models import IntegrationConnection, SystemNotification
from core.security_models import AuthSession
from workers.runner import _payload


def _owner(db_session):
    return create_user(
        db_session,
        email="worker-owner@example.com",
        display_name="Worker Owner",
        password="WorkerSecure!6932",
        role=ROLE_OWNER,
    )


def test_registered_integration_health_handler(db_session):
    owner = _owner(db_session)
    store_credential(
        db_session,
        name="linkedin.worker_token",
        secret_value="worker-token-secret-123456",
        credential_type="oauth_access_token",
        actor=owner,
    )
    connection = upsert_connection(
        db_session,
        platform="linkedin",
        account_key="worker-account",
        display_name="Worker LinkedIn",
        access_credential_name="linkedin.worker_token",
        actor=owner,
    )

    result = execute_registered_job(
        db_session,
        job_type="integration.health_check",
        payload={"connection_id": connection.id},
    )
    db_session.commit()
    refreshed = db_session.get(IntegrationConnection, connection.id)
    assert result["status"] == "connected"
    assert result["remote_api_checked"] is False
    assert refreshed is not None
    assert refreshed.status == "connected"


def test_unknown_handler_is_non_retryable(db_session):
    with pytest.raises(JobError) as raised:
        execute_registered_job(
            db_session,
            job_type="unregistered.command",
            payload={},
        )
    assert raised.value.retryable is False


def test_session_cleanup_handler_revokes_expired_sessions(db_session):
    _owner(db_session)
    tokens = authenticate_user(
        db_session,
        email="worker-owner@example.com",
        password="WorkerSecure!6932",
    )
    auth_session = db_session.scalar(
        select(AuthSession).where(AuthSession.token_hash.is_not(None)).limit(1)
    )
    assert auth_session is not None
    auth_session.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()

    result = execute_registered_job(
        db_session,
        job_type="auth.sessions_cleanup",
        payload={},
    )
    db_session.commit()
    db_session.refresh(auth_session)
    assert result["revoked_sessions"] == 1
    assert auth_session.revoked_at is not None
    assert tokens.session_token


def test_notification_cleanup_handler_deletes_expired_records(db_session):
    owner = _owner(db_session)
    notification = create_notification(
        db_session,
        recipient_user_id=owner.id,
        title="Expired notice",
        message="This notice should be removed.",
        expires_in_hours=1,
    )
    notification.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()

    result = execute_registered_job(
        db_session,
        job_type="notifications.cleanup",
        payload={},
    )
    db_session.commit()
    assert result["deleted_notifications"] == 1
    assert db_session.get(SystemNotification, notification.id) is None


def test_worker_payload_parser_rejects_non_objects():
    assert _payload(json.dumps({"safe": True})) == {"safe": True}
    with pytest.raises(JobError):
        _payload("[]")
    with pytest.raises(JobError):
        _payload("not-json")
