"""Regression tests for allowlisted automation and secure OAuth flows."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

import core.oauth_service as oauth_service
from core.auth import ROLE_OWNER, create_user
from core.automation_service import create_rule, process_event
from core.credential_store import get_credential_value_internal
from core.errors import ValidationAppError
from core.models import Post
from core.operations_models import BackgroundJob, IntegrationConnection, SystemNotification
from core.product_models import AutomationRun
from core.security_models import EncryptedCredential
from core.tenant_migration import backfill_legacy_workspace
from core.tenant_runtime import bind_workspace
from core.tenancy import bootstrap_default_tenant


def _tenant(db_session):
    owner = create_user(
        db_session,
        email="automation-owner@example.com",
        display_name="Automation Owner",
        password="AutomationOwnerSecure!4719",
        role=ROLE_OWNER,
    )
    context = bootstrap_default_tenant(db_session, owner)
    backfill_legacy_workspace(db_session, context.workspace_id)
    bind_workspace(db_session, context)
    return owner, context


def test_allowlisted_automation_conditions_dedup_and_notification(db_session):
    owner, context = _tenant(db_session)
    rule = create_rule(
        db_session,
        context=context,
        actor=owner,
        name="Notify high value leads",
        trigger_type="lead_created",
        conditions={"score": {"operator": "gte", "value": 70}},
        action_type="create_notification",
        action_config={
            "title": "High value lead",
            "message": "A high value lead requires review.",
            "severity": "warning",
        },
        cooldown_seconds=0,
    )
    runs = process_event(
        db_session,
        context=context,
        trigger_type="lead_created",
        event_key="lead-created-100",
        payload={"lead_id": 100, "score": 80, "status": "qualified"},
    )
    assert len(runs) == 1
    assert runs[0].rule_id == rule.id
    assert runs[0].status == "succeeded"
    notification = db_session.scalar(
        select(SystemNotification).where(SystemNotification.title == "High value lead")
    )
    assert notification is not None
    assert process_event(
        db_session,
        context=context,
        trigger_type="lead_created",
        event_key="lead-created-100",
        payload={"lead_id": 100, "score": 80},
    ) == []

    with pytest.raises(ValidationAppError, match="operator"):
        create_rule(
            db_session,
            context=context,
            actor=owner,
            name="Unsafe operator",
            trigger_type="lead_created",
            conditions={"score": {"operator": "eval", "value": "__import__('os')"}},
            action_type="create_notification",
            action_config={"title": "x", "message": "y", "severity": "info"},
        )


def test_publish_automation_requires_approved_or_scheduled_post(db_session):
    owner, context = _tenant(db_session)
    post = Post(
        platform="linkedin",
        topic="Controlled publishing",
        content="Human review is required.",
        status="draft",
    )
    db_session.add(post)
    db_session.commit()
    create_rule(
        db_session,
        context=context,
        actor=owner,
        name="Publish approved post",
        trigger_type="post_status_changed",
        conditions={"status": {"operator": "equals", "value": "approved"}},
        action_type="enqueue_publish",
        action_config={"post_id": post.id},
        cooldown_seconds=0,
    )
    skipped = process_event(
        db_session,
        context=context,
        trigger_type="post_status_changed",
        event_key="post-draft-event",
        payload={"post_id": post.id, "status": "draft"},
    )
    assert skipped[0].status == "skipped"
    assert db_session.scalar(select(BackgroundJob.id)) is None

    post.status = "approved"
    db_session.commit()
    successful = process_event(
        db_session,
        context=context,
        trigger_type="post_status_changed",
        event_key="post-approved-event",
        payload={"post_id": post.id, "status": "approved"},
    )
    assert successful[0].status == "succeeded"
    job = db_session.scalar(
        select(BackgroundJob).where(BackgroundJob.job_type == "publishing.deliver")
    )
    assert job is not None
    assert job.workspace_id == context.workspace_id


def test_oauth_pkce_state_is_one_time_and_tokens_are_encrypted(db_session, monkeypatch):
    owner, context = _tenant(db_session)
    monkeypatch.setenv(
        "OAUTH_LINKEDIN_AUTHORIZATION_URL", "https://auth.example.com/oauth/authorize"
    )
    monkeypatch.setenv(
        "OAUTH_LINKEDIN_TOKEN_URL", "https://token.example.com/oauth/token"
    )
    monkeypatch.setenv("OAUTH_LINKEDIN_CLIENT_ID", "client-id")
    monkeypatch.setenv("OAUTH_LINKEDIN_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setenv("OAUTH_LINKEDIN_SCOPES", "content.read content.write")
    monkeypatch.setenv(
        "OAUTH_LINKEDIN_ALLOWED_HOSTS", "auth.example.com,token.example.com"
    )
    monkeypatch.setattr(
        oauth_service,
        "validate_outbound_https",
        lambda value, **_kwargs: str(value),
    )
    monkeypatch.setattr(
        oauth_service,
        "request_json_limited",
        lambda *_args, **_kwargs: (
            200,
            {
                "access_token": "access-token-secret-123456",
                "refresh_token": "refresh-token-secret-123456",
                "expires_in": 3600,
                "account_id": "account-42",
            },
        ),
    )

    started = oauth_service.begin_authorization(
        db_session,
        context=context,
        actor=owner,
        provider="linkedin",
        redirect_uri="http://localhost:8501",
        account_key="artixcore-company",
        display_name="Artixcore LinkedIn",
        scopes=["content.read", "content.write"],
    )
    query = parse_qs(urlsplit(started.authorization_url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert "code_challenge" in query
    raw_state = query["state"][0]

    connection = oauth_service.complete_authorization(
        db_session,
        context=context,
        actor=owner,
        raw_state=raw_state,
        authorization_code="authorization-code-value",
    )
    assert connection.status == "connected"
    assert connection.account_key == "artixcore-company"
    credentials = list(db_session.scalars(select(EncryptedCredential)).all())
    assert len(credentials) == 2
    serialized = " ".join(item.ciphertext for item in credentials)
    assert "access-token-secret" not in serialized
    assert "refresh-token-secret" not in serialized
    assert get_credential_value_internal(
        db_session, name=connection.access_credential_name
    ) == "access-token-secret-123456"

    with pytest.raises(ValidationAppError, match="already consumed"):
        oauth_service.complete_authorization(
            db_session,
            context=context,
            actor=owner,
            raw_state=raw_state,
            authorization_code="authorization-code-value",
        )

    monkeypatch.setattr(
        oauth_service,
        "request_json_limited",
        lambda *_args, **_kwargs: (
            200,
            {
                "access_token": "rotated-access-token-654321",
                "refresh_token": "rotated-refresh-token-654321",
                "expires_in": 7200,
            },
        ),
    )
    refreshed = oauth_service.refresh_connection_token(
        db_session,
        context=context,
        actor=owner,
        connection_id=connection.id,
    )
    assert refreshed.status == "connected"
    assert get_credential_value_internal(
        db_session, name=refreshed.access_credential_name
    ) == "rotated-access-token-654321"
    assert db_session.query(IntegrationConnection).count() == 1
    assert db_session.query(AutomationRun).count() >= 0
