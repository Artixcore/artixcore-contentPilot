"""Regression tests for durable jobs, notifications, integrations, and webhooks."""

from __future__ import annotations

import hashlib
import hmac
import time

import pytest
from sqlalchemy import select

from core.auth import ROLE_OWNER, create_user
from core.credential_store import store_credential
from core.integrations import list_connections, queue_health_check, upsert_connection
from core.jobs import JobError, claim_next_job, complete_job, enqueue_job, fail_job
from core.notifications import create_notification, list_notifications, mark_notification_read
from core.operations_models import BackgroundJob, SystemNotification, WebhookReceipt
from core.webhooks import record_webhook_receipt, verify_hmac_sha256


def _owner(db_session):
    return create_user(
        db_session,
        email="operations-owner@example.com",
        display_name="Operations Owner",
        password="OperationsSecure!5832",
        role=ROLE_OWNER,
    )


def test_job_idempotency_claim_and_completion(db_session):
    owner = _owner(db_session)
    first = enqueue_job(
        db_session,
        job_type="integration.health_check",
        payload={"connection_id": 10, "token": "must-redact"},
        actor=owner,
        idempotency_key="health-check-10",
    )
    second = enqueue_job(
        db_session,
        job_type="integration.health_check",
        payload={"connection_id": 10},
        actor=owner,
        idempotency_key="health-check-10",
    )
    assert first.id == second.id
    assert "must-redact" not in first.payload_json

    claimed = claim_next_job(db_session, worker_id="worker-1")
    assert claimed is not None
    assert claimed.status == "running"
    assert claimed.attempts == 1

    with pytest.raises(JobError, match="not owned"):
        complete_job(
            db_session,
            job_id=claimed.id,
            worker_id="other-worker",
            result={},
        )

    completed = complete_job(
        db_session,
        job_id=claimed.id,
        worker_id="worker-1",
        result={"healthy": True},
    )
    assert completed.status == "succeeded"
    assert "healthy" in (completed.result_json or "")


def test_failed_job_enters_dead_letter_and_notifies(db_session):
    owner = _owner(db_session)
    job = enqueue_job(
        db_session,
        job_type="publishing.deliver",
        payload={"post_id": 44},
        actor=owner,
        max_attempts=1,
    )
    claimed = claim_next_job(db_session, worker_id="publisher-1")
    assert claimed is not None
    failed = fail_job(
        db_session,
        job_id=job.id,
        worker_id="publisher-1",
        error=RuntimeError("Bearer secret-token-value"),
        retryable=True,
    )
    assert failed.status == "dead_letter"
    assert "secret-token-value" not in (failed.error_message or "")

    notification = db_session.scalar(
        select(SystemNotification).where(
            SystemNotification.recipient_user_id == owner.id
        )
    )
    assert notification is not None
    assert "publishing.deliver" in notification.message


def test_notifications_are_recipient_scoped(db_session):
    owner = _owner(db_session)
    viewer = create_user(
        db_session,
        email="operations-viewer@example.com",
        display_name="Operations Viewer",
        password="ViewerOperations!5832",
        role="viewer",
        actor=owner,
    )
    notification = create_notification(
        db_session,
        recipient_user_id=owner.id,
        severity="warning",
        title="Connector degraded",
        message="LinkedIn health checks are failing.",
        deduplication_key="linkedin-degraded",
    )
    duplicate = create_notification(
        db_session,
        recipient_user_id=owner.id,
        severity="error",
        title="Connector still degraded",
        message="LinkedIn health checks continue to fail.",
        deduplication_key="linkedin-degraded",
    )
    assert duplicate.id == notification.id
    assert len(list_notifications(db_session, user=owner)) == 1
    assert list_notifications(db_session, user=viewer) == []

    marked = mark_notification_read(
        db_session,
        notification_id=notification.id,
        user=owner,
    )
    assert marked.is_read is True


def test_integration_registry_uses_encrypted_credential_references(db_session):
    owner = _owner(db_session)
    store_credential(
        db_session,
        name="linkedin.access_token",
        secret_value="encrypted-linkedin-token-123456",
        credential_type="oauth_access_token",
        actor=owner,
    )
    connection = upsert_connection(
        db_session,
        platform="linkedin",
        account_key="artixcore-company",
        display_name="Artixcore LinkedIn",
        access_credential_name="linkedin.access_token",
        actor=owner,
    )
    assert connection.access_credential_name == "linkedin.access_token"
    assert "encrypted-linkedin-token" not in str(connection.__dict__)
    assert len(list_connections(db_session, actor=owner)) == 1

    job_id = queue_health_check(db_session, connection_id=connection.id, actor=owner)
    job = db_session.get(BackgroundJob, job_id)
    assert job is not None
    assert job.job_type == "integration.health_check"


def test_webhook_signature_timestamp_and_replay_receipt(db_session):
    body = b'{"event":"message.created"}'
    secret = "webhook-signing-secret-123456"
    timestamp = int(time.time())
    digest = hmac.new(
        secret.encode("utf-8"),
        str(timestamp).encode("ascii") + b"." + body,
        hashlib.sha256,
    ).hexdigest()

    assert verify_hmac_sha256(
        body=body,
        secret=secret,
        signature_header=f"sha256={digest}",
        timestamp=timestamp,
    ) is True
    assert verify_hmac_sha256(
        body=body,
        secret=secret,
        signature_header=f"sha256={digest}",
        timestamp=timestamp - 10_000,
    ) is False

    receipt, created = record_webhook_receipt(
        db_session,
        provider="linkedin",
        event_id="evt-123",
        event_type="message.created",
        body=body,
        signature_valid=True,
    )
    replay, replay_created = record_webhook_receipt(
        db_session,
        provider="linkedin",
        event_id="evt-123",
        event_type="message.created",
        body=body,
        signature_valid=True,
    )
    assert created is True
    assert replay_created is False
    assert replay.id == receipt.id
    assert receipt.payload_digest == hashlib.sha256(body).hexdigest()
    assert db_session.query(WebhookReceipt).count() == 1
