"""Run one due ContentPilot content-agent cycle for an explicit workspace."""

from __future__ import annotations

import argparse
import json
import os
import sys

from core.content_agent_dashboard import is_cycle_due
from core.content_agent_team import (
    get_content_agent_settings,
    run_full_content_cycle,
)
from core.content_intelligence_models import ContentAgentSettings  # noqa: F401
from core.database import get_session, init_db
from core.error_handler import handle_exception
from core.errors import ConfigurationError


def _workspace_id(value: str | None) -> int:
    raw = (value or os.getenv("CONTENTPILOT_WORKSPACE_ID", "")).strip()
    if not raw:
        raise ConfigurationError(
            "CONTENTPILOT_WORKSPACE_ID or --workspace-id is required.",
            user_action=(
                "Schedule one runner per workspace and provide its numeric workspace ID."
            ),
        )
    try:
        workspace_id = int(raw)
    except ValueError as exc:
        raise ConfigurationError(
            "The content agent workspace ID must be numeric."
        ) from exc
    if workspace_id <= 0:
        raise ConfigurationError(
            "The content agent workspace ID must be greater than zero."
        )
    return workspace_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run a due ContentPilot content-agent cycle."
    )
    parser.add_argument(
        "--workspace-id",
        help="Numeric workspace ID. Defaults to CONTENTPILOT_WORKSPACE_ID.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when the configured schedule is not due.",
    )
    parser.add_argument(
        "--skip-sync",
        action="store_true",
        help="Use the latest cached Instagram intelligence data.",
    )
    args = parser.parse_args()

    try:
        workspace_id = _workspace_id(args.workspace_id)
        init_db()
        session = get_session(workspace_id)
        try:
            settings = get_content_agent_settings(session)
            if settings is None:
                raise ConfigurationError(
                    "Content Agent Team settings are not configured for this workspace."
                )
            if not args.force and not is_cycle_due(settings):
                print(
                    json.dumps(
                        {
                            "status": "skipped",
                            "workspace_id": workspace_id,
                            "reason": "cycle_not_due",
                        },
                        sort_keys=True,
                    )
                )
                return 0
            result = run_full_content_cycle(
                session,
                sync_data=not args.skip_sync,
                send_telegram=None,
            )
            print(
                json.dumps(
                    {
                        "status": "completed",
                        "workspace_id": workspace_id,
                        "cycle_id": result["cycle_id"],
                        "agents_succeeded": result["succeeded"],
                        "agents_failed": result["failed"],
                        "telegram_delivered": result[
                            "telegram_delivered"
                        ],
                    },
                    sort_keys=True,
                )
            )
            return 0
        finally:
            session.close()
    except Exception as exc:
        error = handle_exception(
            exc,
            context="scheduled_content_agent_cycle",
        )
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_code": error["error_code"],
                    "message": error["message"],
                    "retryable": bool(error.get("retryable")),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
