"""Explicit background-job handler registry. No dynamic imports or arbitrary code execution."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import socket
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.encryption import decrypt_text
from core.errors import ValidationAppError
from core.jobs import JobError
from core.models import Post
from core.operations_models import IntegrationConnection, SystemNotification
from core.publishing import PUBLISHABLE_STATUSES, publish_post
from core.security_models import AuthSession, EncryptedCredential
from core.validation import validate_http_url, validate_positive_id

JobHandler = Callable[[Session, dict[str, Any]], dict[str, Any]]


def handle_integration_health_check(
    session: Session,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Check local integration configuration without exposing or decrypting credentials."""
    connection_id = validate_positive_id(payload.get("connection_id"), field="Connection ID")
    connection = session.get(IntegrationConnection, connection_id)
    if connection is None:
        raise JobError("Integration connection was not found.", retryable=False)

    now = datetime.now(timezone.utc)
    connection.last_health_check_at = now
    if connection.status == "disabled":
        return {"connection_id": connection.id, "status": "disabled", "checked": False}

    if connection.token_expires_at:
        expiry = connection.token_expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= now:
            connection.status = "expired"
            connection.last_error_code = "TOKEN_EXPIRED"
            connection.last_error_message = "The stored token metadata indicates that the token has expired."
            session.flush()
            return {"connection_id": connection.id, "status": "expired", "checked": True}

    credential_names = [
        name
        for name in (
            connection.access_credential_name,
            connection.refresh_credential_name,
        )
        if name
    ]
    if not connection.access_credential_name:
        connection.status = "degraded"
        connection.last_error_code = "ACCESS_CREDENTIAL_MISSING"
        connection.last_error_message = "No active access credential is assigned."
        session.flush()
        return {"connection_id": connection.id, "status": "degraded", "checked": True}

    active_credentials = set(
        session.scalars(
            select(EncryptedCredential.credential_name).where(
                EncryptedCredential.credential_name.in_(credential_names),
                EncryptedCredential.is_active.is_(True),
            )
        ).all()
    )
    missing = sorted(set(credential_names) - active_credentials)
    if missing:
        connection.status = "degraded"
        connection.last_error_code = "CREDENTIAL_INACTIVE"
        connection.last_error_message = "One or more assigned credentials are missing or inactive."
        session.flush()
        return {
            "connection_id": connection.id,
            "status": "degraded",
            "checked": True,
            "missing_credentials": missing,
        }

    connection.status = "connected"
    connection.last_success_at = now
    connection.last_error_code = None
    connection.last_error_message = None
    session.flush()
    return {
        "connection_id": connection.id,
        "platform": connection.platform,
        "status": "connected",
        "checked": True,
        "remote_api_checked": False,
    }


def handle_expired_session_cleanup(
    session: Session,
    payload: dict[str, Any],
) -> dict[str, Any]:
    del payload
    now = datetime.now(timezone.utc)
    sessions = list(
        session.scalars(
            select(AuthSession).where(
                AuthSession.expires_at < now,
                AuthSession.revoked_at.is_(None),
            )
        ).all()
    )
    for auth_session in sessions:
        auth_session.revoked_at = now
        auth_session.revoke_reason = "expired_session_cleanup"
    session.flush()
    return {"revoked_sessions": len(sessions)}


def handle_expired_notification_cleanup(
    session: Session,
    payload: dict[str, Any],
) -> dict[str, Any]:
    del payload
    now = datetime.now(timezone.utc)
    notifications = list(
        session.scalars(
            select(SystemNotification).where(
                SystemNotification.expires_at.is_not(None),
                SystemNotification.expires_at < now,
            )
        ).all()
    )
    for notification in notifications:
        session.delete(notification)
    session.flush()
    return {"deleted_notifications": len(notifications)}


def handle_publish_delivery(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    post_id = validate_positive_id(payload.get("post_id"), field="Post ID")
    post = session.get(Post, post_id)
    if post is None:
        raise JobError("Post was not found in this workspace.", retryable=False)
    if post.status not in PUBLISHABLE_STATUSES:
        raise JobError(
            f"Post must be approved or scheduled before publishing. Current status: {post.status}.",
            retryable=False,
        )
    result = publish_post(
        session,
        post_id=post.id,
        platform_name=post.platform,
        image_url=payload.get("image_url"),
    )
    if not result.get("success"):
        raise JobError("Publishing provider did not accept the post.", retryable=True)
    return {
        "post_id": post.id,
        "platform": post.platform,
        "external_post_id": result.get("external_post_id"),
        "external_post_url": result.get("external_post_url"),
    }


def _allowed_webhook_hosts() -> set[str]:
    return {
        value.strip().lower().rstrip(".")
        for value in os.getenv("AUTOMATION_WEBHOOK_ALLOWED_HOSTS", "").split(",")
        if value.strip()
    }


def _require_public_resolutions(hostname: str, port: int) -> None:
    try:
        results = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise JobError("Webhook hostname could not be resolved.", retryable=True) from exc
    addresses = {entry[4][0] for entry in results}
    if not addresses:
        raise JobError("Webhook hostname has no usable address.", retryable=True)
    for raw_address in addresses:
        address = ipaddress.ip_address(raw_address)
        if any(
            (
                address.is_private,
                address.is_loopback,
                address.is_link_local,
                address.is_multicast,
                address.is_reserved,
                address.is_unspecified,
            )
        ):
            raise JobError("Webhook hostname resolved to a private or reserved address.", retryable=False)


def _credential_value(session: Session, name: str) -> str:
    model = session.scalar(
        select(EncryptedCredential).where(
            EncryptedCredential.credential_name == name,
            EncryptedCredential.is_active.is_(True),
        )
    )
    if model is None:
        raise JobError("Webhook signing credential is missing or inactive.", retryable=False)
    workspace_id = session.info.get("workspace_id")
    contexts = [
        f"credential:{workspace_id}:{name}" if workspace_id is not None else "",
        f"credential:{name}",
    ]
    for context in contexts:
        if not context:
            continue
        try:
            return decrypt_text(model.ciphertext, associated_context=context)
        except Exception:
            continue
    raise JobError("Webhook signing credential could not be decrypted.", retryable=False)


def handle_webhook_delivery(session: Session, payload: dict[str, Any]) -> dict[str, Any]:
    connection_id = validate_positive_id(payload.get("connection_id"), field="Connection ID")
    connection = session.get(IntegrationConnection, connection_id)
    if connection is None or connection.platform != "website":
        raise JobError("Webhook integration was not found.", retryable=False)
    if connection.status not in {"connected", "degraded"}:
        raise JobError("Webhook integration is not active.", retryable=False)

    endpoint = validate_http_url(
        payload.get("endpoint"), field="Webhook endpoint", required=True, allow_private=False
    )
    configured_endpoint = validate_http_url(
        connection.external_account_id,
        field="Configured webhook endpoint",
        required=True,
        allow_private=False,
    )
    if endpoint != configured_endpoint:
        raise JobError("Webhook endpoint does not match the integration configuration.", retryable=False)
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https":
        raise JobError("Webhook endpoint must use HTTPS.", retryable=False)
    allowed_hosts = _allowed_webhook_hosts()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not allowed_hosts or hostname not in allowed_hosts:
        raise JobError("Webhook hostname is not present in the deployment allowlist.", retryable=False)
    _require_public_resolutions(hostname, parsed.port or 443)

    event = payload.get("event")
    if not isinstance(event, dict):
        raise JobError("Webhook event must be a JSON object.", retryable=False)
    body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > 256 * 1024:
        raise JobError("Webhook event exceeds the 256 KB delivery limit.", retryable=False)
    if not connection.access_credential_name:
        raise JobError("Webhook signing credential is not configured.", retryable=False)
    secret = _credential_value(session, connection.access_credential_name)
    timestamp = str(int(datetime.now(timezone.utc).timestamp()))
    signature = hmac.new(
        secret.encode("utf-8"), timestamp.encode("ascii") + b"." + body, hashlib.sha256
    ).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Artixcore-ContentPilot-Webhook/1.0",
        "X-ContentPilot-Timestamp": timestamp,
        "X-ContentPilot-Signature": f"sha256={signature}",
        "X-ContentPilot-Rule-ID": str(payload.get("rule_id", ""))[:64],
    }
    timeout = httpx.Timeout(10.0, connect=5.0)
    received = 0
    status_code = 0
    with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False) as client:
        with client.stream("POST", endpoint, content=body, headers=headers) as response:
            status_code = response.status_code
            for chunk in response.iter_bytes():
                received += len(chunk)
                if received > 256 * 1024:
                    raise JobError("Webhook response exceeded the 256 KB limit.", retryable=False)
    if not 200 <= status_code < 300:
        retryable = status_code == 429 or status_code >= 500
        raise JobError(
            f"Webhook endpoint returned HTTP {status_code}.",
            retryable=retryable,
        )
    return {
        "connection_id": connection.id,
        "status_code": status_code,
        "response_bytes": received,
    }


JOB_HANDLERS: dict[str, JobHandler] = {
    "integration.health_check": handle_integration_health_check,
    "auth.sessions_cleanup": handle_expired_session_cleanup,
    "notifications.cleanup": handle_expired_notification_cleanup,
    "publishing.deliver": handle_publish_delivery,
    "automation.webhook_delivery": handle_webhook_delivery,
}


def execute_registered_job(
    session: Session,
    *,
    job_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    handler = JOB_HANDLERS.get(job_type)
    if handler is None:
        raise JobError(f"No registered handler exists for job type '{job_type}'.", retryable=False)
    if not isinstance(payload, dict):
        raise ValidationAppError("Job payload must be a JSON object.")
    return handler(session, payload)
