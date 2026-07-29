"""Webhook signature verification, replay protection, and payload-minimizing receipts."""

from __future__ import annotations

import hashlib
import hmac
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.audit import log_audit_event
from core.errors import ValidationAppError
from core.operations_models import WebhookReceipt
from core.validation import normalize_text


def _safe_provider(value: object) -> str:
    provider = normalize_text(
        value,
        field="Webhook provider",
        min_length=2,
        max_length=50,
        allow_newlines=False,
    ).lower()
    if not all(char.isalnum() or char in {"_", "-"} for char in provider):
        raise ValidationAppError("Webhook provider contains unsupported characters.")
    return provider


def verify_hmac_sha256(
    *,
    body: bytes,
    secret: str,
    signature_header: str,
    timestamp: str | int | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify an HMAC-SHA256 signature using constant-time comparison."""
    if not isinstance(body, bytes):
        raise ValidationAppError("Webhook body must be raw bytes.")
    if not secret or len(secret) < 16:
        raise ValidationAppError("Webhook signing secret is missing or too short.")
    if len(body) > 10 * 1024 * 1024:
        raise ValidationAppError("Webhook payload exceeds the 10 MB limit.")

    safe_tolerance = min(max(int(tolerance_seconds), 30), 3_600)
    timestamp_text = ""
    if timestamp is not None:
        try:
            timestamp_value = int(timestamp)
        except (TypeError, ValueError) as exc:
            raise ValidationAppError("Webhook timestamp is invalid.") from exc
        if abs(int(time.time()) - timestamp_value) > safe_tolerance:
            return False
        timestamp_text = str(timestamp_value)

    signed_payload = body if not timestamp_text else timestamp_text.encode("ascii") + b"." + body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    supplied = str(signature_header or "").strip()
    if supplied.lower().startswith("sha256="):
        supplied = supplied.split("=", 1)[1]
    if len(supplied) != 64 or any(char not in "0123456789abcdefABCDEF" for char in supplied):
        return False
    return hmac.compare_digest(expected.lower(), supplied.lower())


def _validate_replay(existing: WebhookReceipt, payload_digest: str) -> None:
    if not hmac.compare_digest(existing.payload_digest, payload_digest):
        raise ValidationAppError(
            "Webhook event ID was reused with a different payload. The event was rejected."
        )


def record_webhook_receipt(
    session: Session,
    *,
    provider: str,
    event_id: str,
    body: bytes,
    signature_valid: bool,
    event_type: str | None = None,
) -> tuple[WebhookReceipt, bool]:
    """Record only the payload digest. Returns (receipt, created_new)."""
    safe_provider = _safe_provider(provider)
    safe_event_id = normalize_text(
        event_id,
        field="Webhook event ID",
        min_length=1,
        max_length=255,
        allow_newlines=False,
    )
    safe_event_type = (
        normalize_text(
            event_type,
            field="Webhook event type",
            max_length=255,
            allow_newlines=False,
        )
        if event_type
        else None
    )
    if not isinstance(body, bytes) or len(body) > 10 * 1024 * 1024:
        raise ValidationAppError("Webhook payload is invalid or too large.")

    payload_digest = hashlib.sha256(body).hexdigest()
    existing = session.scalar(
        select(WebhookReceipt).where(
            WebhookReceipt.provider == safe_provider,
            WebhookReceipt.event_id == safe_event_id,
        )
    )
    if existing:
        _validate_replay(existing, payload_digest)
        return existing, False

    receipt = WebhookReceipt(
        provider=safe_provider,
        event_id=safe_event_id,
        event_type=safe_event_type,
        payload_digest=payload_digest,
        signature_valid=bool(signature_valid),
        status="received" if signature_valid else "rejected",
        error_code=None if signature_valid else "INVALID_SIGNATURE",
    )
    try:
        session.add(receipt)
        session.flush()
        log_audit_event(
            session,
            action="webhook.received" if signature_valid else "webhook.rejected",
            outcome="success" if signature_valid else "blocked",
            resource_type="webhook_receipt",
            resource_id=receipt.id,
            event_data={
                "provider": safe_provider,
                "event_id": safe_event_id,
                "event_type": safe_event_type,
                "signature_valid": bool(signature_valid),
            },
        )
        session.commit()
        session.refresh(receipt)
        return receipt, True
    except IntegrityError:
        session.rollback()
        existing = session.scalar(
            select(WebhookReceipt).where(
                WebhookReceipt.provider == safe_provider,
                WebhookReceipt.event_id == safe_event_id,
            )
        )
        if existing:
            _validate_replay(existing, payload_digest)
            return existing, False
        raise


def mark_webhook_processed(
    session: Session,
    *,
    receipt_id: int,
    success: bool,
    error_code: str | None = None,
) -> WebhookReceipt:
    receipt = session.get(WebhookReceipt, int(receipt_id))
    if receipt is None:
        raise ValidationAppError("Webhook receipt was not found.")
    if not receipt.signature_valid:
        raise ValidationAppError("A webhook with an invalid signature cannot be processed.")
    receipt.status = "processed" if success else "failed"
    receipt.error_code = (
        normalize_text(error_code, field="Error code", max_length=100, allow_newlines=False)
        if error_code
        else None
    )
    receipt.processed_at = datetime.now(timezone.utc)
    session.commit()
    session.refresh(receipt)
    return receipt
