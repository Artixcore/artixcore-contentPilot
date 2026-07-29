"""Append-only, sanitized security and business audit logging."""

from __future__ import annotations

import json
import secrets
from typing import Any

from sqlalchemy.orm import Session

from core.logging_config import get_logger, sanitize_log_message
from core.security_models import AuditEvent
from core.utils import sanitize_payload

logger = get_logger(__name__)


def new_request_id() -> str:
    return secrets.token_hex(16)


def log_audit_event(
    session: Session,
    *,
    action: str,
    outcome: str = "success",
    actor_user_id: int | None = None,
    actor_email: str | None = None,
    resource_type: str | None = None,
    resource_id: str | int | None = None,
    event_data: dict[str, Any] | None = None,
    request_id: str | None = None,
    workspace_id: int | None = None,
) -> AuditEvent | None:
    """Stage an audit event without committing or rolling back caller work."""
    safe_action = sanitize_log_message(str(action or "unknown"))[:128]
    safe_outcome = sanitize_log_message(str(outcome or "unknown"))[:32]
    safe_email = sanitize_log_message(str(actor_email or ""))[:320] or None
    safe_resource_type = sanitize_log_message(str(resource_type or ""))[:100] or None
    safe_resource_id = sanitize_log_message(str(resource_id or ""))[:255] or None
    active_workspace_id = workspace_id
    if active_workspace_id is None:
        value = session.info.get("workspace_id")
        active_workspace_id = int(value) if value is not None else None

    try:
        payload = json.loads(sanitize_payload(event_data or {}))
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))[:20_000]
    except Exception:
        serialized = "{}"

    event = AuditEvent(
        workspace_id=active_workspace_id,
        request_id=(request_id or new_request_id())[:64],
        actor_user_id=actor_user_id,
        actor_email=safe_email,
        action=safe_action,
        resource_type=safe_resource_type,
        resource_id=safe_resource_id,
        outcome=safe_outcome,
        event_data=serialized,
    )
    try:
        with session.begin_nested():
            session.add(event)
            session.flush()
        return event
    except Exception as exc:
        logger.warning("Failed to stage audit event: %s", type(exc).__name__)
        return None
