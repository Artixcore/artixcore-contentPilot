"""Dedicated durable-job worker process."""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import time
from dataclasses import dataclass

from core.config_validation import validate_startup_configuration
from core.database import get_session, init_db
from core.job_handlers import execute_registered_job
from core.jobs import JobError, claim_next_job, complete_job, fail_job
from core.logging_config import get_logger, setup_logging
from core.tenancy import set_session_workspace

logger = get_logger(__name__)


@dataclass
class WorkerState:
    stopping: bool = False


def _bounded_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def _worker_id() -> str:
    configured = os.getenv("WORKER_ID", "").strip()
    if configured:
        return configured[:128]
    return f"{socket.gethostname()}-{os.getpid()}"[:128]


def _payload(raw: str) -> dict:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        raise JobError("Job payload is invalid JSON.", retryable=False) from exc
    if not isinstance(parsed, dict):
        raise JobError("Job payload must be a JSON object.", retryable=False)
    return parsed


def run_once(worker_id: str) -> bool:
    # Queue claiming is the only cross-workspace worker operation. Execution is
    # rebound to the claimed job workspace before any handler is called.
    session = get_session(tenant_bypass=True)
    job = None
    try:
        job = claim_next_job(session, worker_id=worker_id)
        if job is None:
            return False
        if not job.workspace_id:
            raise JobError("Job has no workspace assignment.", retryable=False)
        set_session_workspace(session, int(job.workspace_id), tenant_bypass=False)
        result = execute_registered_job(
            session,
            job_type=job.job_type,
            payload=_payload(job.payload_json),
        )
        complete_job(
            session,
            job_id=job.id,
            worker_id=worker_id,
            result=result,
        )
        logger.info(
            "Completed job id=%s workspace=%s type=%s",
            job.id,
            job.workspace_id,
            job.job_type,
        )
        return True
    except Exception as exc:
        session.rollback()
        if job is not None:
            try:
                if job.workspace_id:
                    set_session_workspace(session, int(job.workspace_id), tenant_bypass=False)
                retryable = getattr(exc, "retryable", True)
                failed = fail_job(
                    session,
                    job_id=job.id,
                    worker_id=worker_id,
                    error=exc,
                    retryable=bool(retryable),
                )
                logger.warning(
                    "Job failed id=%s workspace=%s type=%s status=%s error=%s",
                    job.id,
                    job.workspace_id,
                    job.job_type,
                    failed.status,
                    type(exc).__name__,
                )
            except Exception as failure_exc:
                session.rollback()
                logger.error(
                    "Failed to record job failure id=%s error=%s",
                    getattr(job, "id", "unknown"),
                    type(failure_exc).__name__,
                )
        else:
            logger.error("Worker iteration failed before claiming a job: %s", type(exc).__name__)
        return False
    finally:
        session.close()


def run_forever(*, once: bool = False) -> int:
    setup_logging()
    validate_startup_configuration()
    init_db()

    worker_id = _worker_id()
    poll_seconds = _bounded_float("WORKER_POLL_SECONDS", 2.0, 0.25, 60.0)
    idle_seconds = _bounded_float("WORKER_IDLE_SECONDS", 5.0, poll_seconds, 300.0)
    state = WorkerState()

    def _stop(_signum, _frame) -> None:
        state.stopping = True
        logger.info("Worker shutdown requested")

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info("ContentPilot worker started id=%s", worker_id)

    while not state.stopping:
        processed = run_once(worker_id)
        if once:
            return 0
        time.sleep(poll_seconds if processed else idle_seconds)

    logger.info("ContentPilot worker stopped id=%s", worker_id)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the ContentPilot durable job worker.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one available job and exit.",
    )
    args = parser.parse_args()
    return run_forever(once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
