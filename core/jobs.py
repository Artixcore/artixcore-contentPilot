"""Durable database-backed job queue with retries, locking, and audit events."""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from core.audit import log_audit_event
from core.auth import AuthenticatedUser, require_permission
from core.errors import AppError, ValidationAppError
from core.logging_config import sanitize_log_message
from core.operations_models import BackgroundJob, SystemNotification
from core.utils import sanitize_payload

_JOB_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,99}$")
_TERMINAL_STATUSES = frozenset({"succeeded", "cancelled", "dead_letter"})


class JobError(AppError):
    default_error_code = "JOB_ERROR"
    default_user_action = "Review the job details and retry when the underlying issue is resolved."
    retryable_default = True


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_json(data: Any, *, max_length: int = 100_000) -> str:
    serialized = sanitize_payload(data if data is not None else {})
    if len(serialized) > max_length:
        raise ValidationAppError(f"Serialized job data cannot exceed {max_length} characters.")
    try:
        parsed = json.loads(serialized)
    except json.JSONDecodeError as exc:
        raise ValidationAppError("Job data must be JSON serializable.") from exc
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def _validate_job_type(job_type: object) -> str:
    value = str(job_type or "").strip().lower()
    if not _JOB_TYPE_RE.fullmatch(value):
        raise ValidationAppError(
            "Job type must begin with a letter and contain only lowercase letters, numbers, dots, hyphens, or underscores."
        )
    return value


def _validate_idempotency_key(value: object | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    key = str(value).strip()
    if len(key) > 128 or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ValidationAppError("Idempotency key contains unsupported characters or is too long.")
    return key


def enqueue_job(
    session: Session,
    *,
    job_type: str,
    payload: dict[str, Any] | None = None,
    actor: AuthenticatedUser | None = None,
    priority: int = 50,
    max_attempts: int = 3,
    available_at: datetime | None = None,
    idempotency_key: str | None = None,
) -> BackgroundJob:
    if actor is not None and not (
        actor.can("create_content")
        or actor.can("publish_content")
        or actor.can("manage_integrations")
        or actor.can("manage_security")
    ):
        raise ValidationAppError("Your role cannot enqueue operational jobs.")

    safe_type = _validate_job_type(job_type)
    safe_priority = int(priority)
    safe_max_attempts = int(max_attempts)
    if not 0 <= safe_priority <= 100:
        raise ValidationAppError("Job priority must be between 0 and 100.")
    if not 1 <= safe_max_attempts <= 20:
        raise ValidationAppError("Job max attempts must be between 1 and 20.")
    key = _validate_idempotency_key(idempotency_key)

    if key:
        existing = session.scalar(
            select(BackgroundJob).where(BackgroundJob.idempotency_key == key).limit(1)
        )
        if existing:
            return existing

    job = BackgroundJob(
        job_type=safe_type,
        status="queued",
        priority=safe_priority,
        payload_json=_safe_json(payload or {}),
        max_attempts=safe_max_attempts,
        idempotency_key=key,
        requested_by_user_id=actor.id if actor else None,
        available_at=available_at or _utc_now(),
    )
    try:
        session.add(job)
        session.flush()
        log_audit_event(
            session,
            action="job.enqueued",
            actor_user_id=actor.id if actor else None,
            actor_email=actor.email if actor else None,
            resource_type="background_job",
            resource_id=job.id,
            event_data={
                "job_type": job.job_type,
                "priority": job.priority,
                "max_attempts": job.max_attempts,
            },
        )
        session.commit()
        session.refresh(job)
        return job
    except IntegrityError:
        session.rollback()
        if key:
            existing = session.scalar(
                select(BackgroundJob).where(BackgroundJob.idempotency_key == key).limit(1)
            )
            if existing:
                return existing
        raise
    except Exception:
        session.rollback()
        raise


def claim_next_job(
    session: Session,
    *,
    worker_id: str,
    job_types: set[str] | None = None,
) -> BackgroundJob | None:
    worker = str(worker_id or "").strip()
    if not worker or len(worker) > 128:
        raise ValidationAppError("Worker ID is required and cannot exceed 128 characters.")

    now = _utc_now()
    query = select(BackgroundJob).where(
        BackgroundJob.status == "queued",
        BackgroundJob.available_at <= now,
    )
    if job_types:
        safe_types = {_validate_job_type(value) for value in job_types}
        query = query.where(BackgroundJob.job_type.in_(safe_types))
    query = query.order_by(
        BackgroundJob.priority.desc(),
        BackgroundJob.available_at.asc(),
        BackgroundJob.created_at.asc(),
    ).limit(1)

    if session.get_bind().dialect.name == "postgresql":
        query = query.with_for_update(skip_locked=True)

    job = session.scalar(query)
    if job is None:
        return None

    job.status = "running"
    job.locked_by = worker
    job.locked_at = now
    job.started_at = now
    job.attempts = int(job.attempts or 0) + 1
    session.commit()
    session.refresh(job)
    return job


def complete_job(
    session: Session,
    *,
    job_id: int,
    worker_id: str,
    result: dict[str, Any] | None = None,
) -> BackgroundJob:
    job = session.get(BackgroundJob, int(job_id))
    if job is None:
        raise JobError("Job was not found.", retryable=False)
    if job.status != "running" or job.locked_by != worker_id:
        raise JobError("Job is not owned by this worker.", retryable=False)

    now = _utc_now()
    job.status = "succeeded"
    job.result_json = _safe_json(result or {}, max_length=200_000)
    job.error_code = None
    job.error_message = None
    job.finished_at = now
    job.locked_by = None
    job.locked_at = None
    log_audit_event(
        session,
        action="job.succeeded",
        actor_user_id=job.requested_by_user_id,
        resource_type="background_job",
        resource_id=job.id,
        event_data={"job_type": job.job_type, "attempts": job.attempts},
    )
    session.commit()
    session.refresh(job)
    return job


def fail_job(
    session: Session,
    *,
    job_id: int,
    worker_id: str,
    error: BaseException,
    retryable: bool = True,
) -> BackgroundJob:
    job = session.get(BackgroundJob, int(job_id))
    if job is None:
        raise JobError("Job was not found.", retryable=False)
    if job.status != "running" or job.locked_by != worker_id:
        raise JobError("Job is not owned by this worker.", retryable=False)

    now = _utc_now()
    error_code = getattr(error, "error_code", type(error).__name__.upper())[:100]
    safe_message = sanitize_log_message(getattr(error, "message", str(error)))[:2_000]
    can_retry = retryable and int(job.attempts or 0) < int(job.max_attempts or 1)

    if can_retry:
        backoff_seconds = min(3_600, 30 * (2 ** max(int(job.attempts or 1) - 1, 0)))
        jitter = secrets.randbelow(15)
        job.status = "queued"
        job.available_at = now + timedelta(seconds=backoff_seconds + jitter)
    else:
        job.status = "dead_letter"
        job.finished_at = now

    job.error_code = error_code
    job.error_message = safe_message
    job.locked_by = None
    job.locked_at = None

    log_audit_event(
        session,
        action="job.retry_scheduled" if can_retry else "job.dead_lettered",
        actor_user_id=job.requested_by_user_id,
        resource_type="background_job",
        resource_id=job.id,
        outcome="warning" if can_retry else "failure",
        event_data={
            "job_type": job.job_type,
            "attempts": job.attempts,
            "error_code": error_code,
        },
    )

    if not can_retry:
        session.add(
            SystemNotification(
                recipient_user_id=job.requested_by_user_id,
                severity="error",
                title="Background job requires attention",
                message=f"{job.job_type} failed after {job.attempts} attempt(s). Error code: {error_code}",
                action_label="Open Operations",
                action_page="Operations",
                deduplication_key=f"job-dead-letter:{job.id}",
            )
        )

    session.commit()
    session.refresh(job)
    return job


def cancel_job(
    session: Session,
    *,
    job_id: int,
    actor: AuthenticatedUser,
) -> BackgroundJob:
    require_permission(actor, "manage_security")
    job = session.get(BackgroundJob, int(job_id))
    if job is None:
        raise JobError("Job was not found.", retryable=False)
    if job.status in _TERMINAL_STATUSES:
        return job
    job.status = "cancelled"
    job.finished_at = _utc_now()
    job.locked_by = None
    job.locked_at = None
    log_audit_event(
        session,
        action="job.cancelled",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="background_job",
        resource_id=job.id,
        event_data={"job_type": job.job_type},
    )
    session.commit()
    session.refresh(job)
    return job


def retry_dead_letter_job(
    session: Session,
    *,
    job_id: int,
    actor: AuthenticatedUser,
) -> BackgroundJob:
    require_permission(actor, "manage_security")
    job = session.get(BackgroundJob, int(job_id))
    if job is None or job.status not in {"dead_letter", "failed"}:
        raise JobError("Only failed or dead-letter jobs can be retried.", retryable=False)
    job.status = "queued"
    job.attempts = 0
    job.error_code = None
    job.error_message = None
    job.finished_at = None
    job.available_at = _utc_now()
    log_audit_event(
        session,
        action="job.manual_retry",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="background_job",
        resource_id=job.id,
        event_data={"job_type": job.job_type},
    )
    session.commit()
    session.refresh(job)
    return job


def list_jobs(
    session: Session,
    *,
    actor: AuthenticatedUser,
    status: str | None = None,
    limit: int = 200,
) -> list[BackgroundJob]:
    if not (actor.can("manage_security") or actor.can("manage_integrations")):
        raise ValidationAppError("Your role cannot view operational jobs.")
    query = select(BackgroundJob)
    if status:
        query = query.where(BackgroundJob.status == str(status).strip().lower())
    query = query.order_by(BackgroundJob.created_at.desc()).limit(min(max(int(limit), 1), 500))
    return list(session.scalars(query).all())
