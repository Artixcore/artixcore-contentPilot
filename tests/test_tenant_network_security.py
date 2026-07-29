"""Regression tests for workspace-bound encryption and outbound network controls."""

from __future__ import annotations

import json

import pytest

import core.job_handlers as job_handlers
import core.outbound_http as outbound_http
from core.auth import ROLE_OWNER, create_user
from core.credential_store import get_credential_value_internal, store_credential
from core.errors import ValidationAppError
from core.job_handlers import handle_webhook_delivery
from core.jobs import JobError
from core.operations_models import IntegrationConnection
from core.security_models import EncryptedCredential
from core.tenant_migration import backfill_legacy_workspace
from core.tenant_runtime import bind_workspace
from core.tenancy import bootstrap_default_tenant, create_organization


def _tenant(db_session):
    owner = create_user(
        db_session,
        email="network-owner@example.com",
        display_name="Network Owner",
        password="NetworkOwnerSecure!8327",
        role=ROLE_OWNER,
    )
    context = bootstrap_default_tenant(db_session, owner)
    backfill_legacy_workspace(db_session, context.workspace_id)
    bind_workspace(db_session, context)
    return owner, context


def test_ciphertext_cannot_be_reused_in_another_workspace(db_session):
    owner, first = _tenant(db_session)
    original = store_credential(
        db_session,
        name="oauth.linkedin.company.access_token",
        secret_value="workspace-one-secret-token-123456",
        credential_type="oauth_access_token",
        actor=owner,
    )
    second = create_organization(
        db_session,
        actor=owner,
        name="Second Security Organization",
        slug="second-security-organization",
        workspace_name="Second Security Workspace",
        workspace_slug="second-security-workspace",
    )
    bind_workspace(db_session, second)
    assert db_session.get(EncryptedCredential, original.id) is None

    copied = EncryptedCredential(
        credential_name="oauth.linkedin.company.access_token",
        ciphertext=original.ciphertext,
        key_id=original.key_id,
        credential_type="oauth_access_token",
        is_active=True,
        version=1,
        created_by_user_id=owner.id,
    )
    db_session.add(copied)
    db_session.commit()
    with pytest.raises(ValidationAppError, match="workspace encryption context"):
        get_credential_value_internal(
            db_session, name="oauth.linkedin.company.access_token"
        )


def test_outbound_https_rejects_private_dns(monkeypatch):
    monkeypatch.setattr(
        outbound_http.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [(2, 1, 6, "", ("127.0.0.1", 443))],
    )
    with pytest.raises(ValidationAppError, match="private or reserved"):
        outbound_http.validate_outbound_https(
            "https://hooks.example.com/receive",
            field="Webhook endpoint",
            allowed_hosts={"hooks.example.com"},
        )


def test_webhook_delivery_requires_allowlist_and_signs_payload(db_session, monkeypatch):
    owner, context = _tenant(db_session)
    store_credential(
        db_session,
        name="website.webhook.signing_secret",
        secret_value="webhook-signing-secret-value-123456",
        credential_type="webhook_signing_secret",
        actor=owner,
    )
    connection = IntegrationConnection(
        platform="website",
        account_key="crm-webhook",
        display_name="CRM Webhook",
        external_account_id="https://hooks.example.com/receive",
        status="connected",
        access_credential_name="website.webhook.signing_secret",
        created_by_user_id=owner.id,
    )
    db_session.add(connection)
    db_session.commit()
    payload = {
        "connection_id": connection.id,
        "endpoint": "https://hooks.example.com/receive",
        "event": {"lead_id": 10, "status": "qualified"},
        "rule_id": 5,
    }

    monkeypatch.delenv("AUTOMATION_WEBHOOK_ALLOWED_HOSTS", raising=False)
    with pytest.raises(JobError, match="allowlist"):
        handle_webhook_delivery(db_session, payload)

    monkeypatch.setenv("AUTOMATION_WEBHOOK_ALLOWED_HOSTS", "hooks.example.com")
    monkeypatch.setattr(job_handlers, "_require_public_resolutions", lambda *_args: None)
    captured: dict = {}

    class FakeResponse:
        status_code = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_bytes(self):
            yield b"ok"

    class FakeClient:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def stream(self, method, url, *, content, headers):
            captured.update(
                {
                    "method": method,
                    "url": url,
                    "content": content,
                    "headers": headers,
                }
            )
            return FakeResponse()

    monkeypatch.setattr(job_handlers.httpx, "Client", FakeClient)
    result = handle_webhook_delivery(db_session, payload)
    assert result["status_code"] == 204
    assert captured["method"] == "POST"
    assert captured["url"] == "https://hooks.example.com/receive"
    assert json.loads(captured["content"].decode("utf-8"))["lead_id"] == 10
    assert captured["headers"]["X-ContentPilot-Signature"].startswith("sha256=")
    assert "webhook-signing-secret-value" not in str(captured)
