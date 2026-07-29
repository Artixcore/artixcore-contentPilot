"""Allowlisted, tenant-bound automation rules with no dynamic execution."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.audit import log_audit_event
from core.auth import AuthenticatedUser
from core.errors import AppError, ValidationAppError
from core.jobs import enqueue_job
from core.logging_config import sanitize_log_message
from core.models import Post
from core.notifications import create_notification
from core.operations_models import IntegrationConnection
from core.product_models import AutomationRule, AutomationRun, LeadRecord
from core.publishing import PUBLISHABLE_STATUSES
from core.tenant_models import WorkspaceMembership
from core.tenancy import WorkspaceContext, require_workspace_permission
from core.validation import normalize_text, validate_http_url, validate_positive_id

TRIGGER_TYPES = frozenset(
    {
        "schedule",
        "post_status_changed",
        "lead_created",
        "lead_score_changed",
        "integration_health_changed",
        "webhook_received",
    }
)
ACTION_TYPES = frozenset(
    {
        "enqueue_publish",
        "create_notification",
        "assign_lead",
        "change_lead_status",
        "queue_integration_health_check",
        "invoke_webhook",
    }
)
OPERATORS = frozenset({"equals", "not_equals", "in", "not_in", "gte", "lte", "contains"})
LEAD_STATUSES = frozenset(
    {"new", "qualified", "contacted", "proposal", "won", "lost", "spam", "archived"}
)


class AutomationError(AppError):
    default_error_code = "AUTOMATION_ERROR"
    default_user_action = "Review the automation rule and correct its configuration."
    retryable_default = False


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _safe_json(value: Any, *, field: str, maximum: int = 50_000) -> str:
    try:
        serialized = json.dumps(
            value if value is not None else {}, ensure_ascii=False, separators=(",", ":")
        )
    except (TypeError, ValueError) as exc:
        raise ValidationAppError(f"{field} must be JSON serializable.") from exc
    if len(serialized) > maximum:
        raise ValidationAppError(f"{field} is too large.")
    return serialized


def _load_object(value: str | None, *, field: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise AutomationError(f"{field} contains invalid JSON.") from exc
    if not isinstance(parsed, dict):
        raise AutomationError(f"{field} must be a JSON object.")
    return parsed


def _validate_conditions(conditions: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(conditions, dict):
        raise ValidationAppError("Automation conditions must be an object.")
    if len(conditions) > 20:
        raise ValidationAppError("An automation rule cannot contain more than 20 conditions.")
    result: dict[str, Any] = {}
    for field_name, specification in conditions.items():
        clean_field = normalize_text(
            field_name,
            field="Condition field",
            min_length=1,
            max_length=100,
            allow_newlines=False,
        )
        if not all(character.isalnum() or character in {"_", "."} for character in clean_field):
            raise ValidationAppError("Condition fields contain unsupported characters.")
        if not isinstance(specification, dict):
            raise ValidationAppError("Each condition must contain an operator and value.")
        operator = str(specification.get("operator", "equals")).strip().lower()
        if operator not in OPERATORS:
            raise ValidationAppError("Automation condition operator is unsupported.")
        expected = specification.get("value")
        if isinstance(expected, (dict, tuple, set)):
            raise ValidationAppError("Condition values must be scalar values or lists.")
        if isinstance(expected, list) and len(expected) > 100:
            raise ValidationAppError("Automation condition list is too large.")
        result[clean_field] = {"operator": operator, "value": expected}
    _safe_json(result, field="Conditions")
    return result


def _field_value(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _matches_condition(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "equals":
        return actual == expected
    if operator == "not_equals":
        return actual != expected
    if operator == "in":
        return isinstance(expected, list) and actual in expected
    if operator == "not_in":
        return isinstance(expected, list) and actual not in expected
    if operator == "contains":
        if isinstance(actual, str):
            return str(expected).casefold() in actual.casefold()
        if isinstance(actual, list):
            return expected in actual
        return False
    if operator in {"gte", "lte"}:
        try:
            actual_number = float(actual)
            expected_number = float(expected)
        except (TypeError, ValueError):
            return False
        return actual_number >= expected_number if operator == "gte" else actual_number <= expected_number
    return False


def matches_rule(rule: AutomationRule, payload: dict[str, Any]) -> bool:
    conditions = _load_object(rule.conditions_json, field="Rule conditions")
    return all(
        _matches_condition(
            _field_value(payload, field_name),
            specification["operator"],
            specification.get("value"),
        )
        for field_name, specification in conditions.items()
    )


def _validate_action_config(action_type: str, config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValidationAppError("Action configuration must be an object.")
    safe = dict(config)
    if action_type == "create_notification":
        safe["title"] = normalize_text(
            safe.get("title"), field="Notification title", min_length=1, max_length=255
        )
        safe["message"] = normalize_text(
            safe.get("message"), field="Notification message", min_length=1, max_length=10_000
        )
        severity = str(safe.get("severity", "info")).strip().lower()
        if severity not in {"info", "success", "warning", "error", "critical"}:
            raise ValidationAppError("Notification severity is invalid.")
        safe["severity"] = severity
        if safe.get("recipient_user_id") is not None:
            safe["recipient_user_id"] = validate_positive_id(
                safe["recipient_user_id"], field="Recipient user ID"
            )
        if safe.get("action_label") is not None:
            safe["action_label"] = normalize_text(
                safe["action_label"], field="Action label", max_length=100, allow_newlines=False
            )
        if safe.get("action_page") is not None:
            safe["action_page"] = normalize_text(
                safe["action_page"], field="Action page", max_length=100, allow_newlines=False
            )
    elif action_type in {"assign_lead", "change_lead_status"}:
        if safe.get("lead_id") is not None:
            safe["lead_id"] = validate_positive_id(safe["lead_id"], field="Lead ID")
        if action_type == "assign_lead":
            safe["user_id"] = validate_positive_id(safe.get("user_id"), field="User ID")
        else:
            status = str(safe.get("status", "")).strip().lower()
            if status not in LEAD_STATUSES:
                raise ValidationAppError("Lead status is invalid.")
            safe["status"] = status
    elif action_type in {"queue_integration_health_check", "invoke_webhook"}:
        safe["connection_id"] = validate_positive_id(
            safe.get("connection_id"), field="Connection ID"
        )
    elif action_type == "enqueue_publish":
        if safe.get("post_id") is not None:
            safe["post_id"] = validate_positive_id(safe["post_id"], field="Post ID")
    else:
        raise ValidationAppError("Automation action is unsupported.")
    _safe_json(safe, field="Action configuration")
    return safe


def create_rule(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    name: str,
    trigger_type: str,
    conditions: dict[str, Any],
    action_type: str,
    action_config: dict[str, Any],
    cooldown_seconds: int = 60,
    is_active: bool = True,
) -> AutomationRule:
    require_workspace_permission(context, "workspace:admin")
    clean_name = normalize_text(
        name, field="Automation name", min_length=2, max_length=255, allow_newlines=False
    )
    trigger = str(trigger_type or "").strip().lower()
    action = str(action_type or "").strip().lower()
    if trigger not in TRIGGER_TYPES:
        raise ValidationAppError("Automation trigger is unsupported.")
    if action not in ACTION_TYPES:
        raise ValidationAppError("Automation action is unsupported.")
    if session.scalar(select(AutomationRule.id).where(AutomationRule.name == clean_name)):
        raise ValidationAppError("An automation rule with this name already exists.")
    cooldown = int(cooldown_seconds)
    if not 0 <= cooldown <= 86_400:
        raise ValidationAppError("Automation cooldown must be between 0 and 86,400 seconds.")
    model = AutomationRule(
        name=clean_name,
        trigger_type=trigger,
        conditions_json=_safe_json(_validate_conditions(conditions), field="Conditions"),
        action_type=action,
        action_config_json=_safe_json(
            _validate_action_config(action, action_config), field="Action configuration"
        ),
        is_active=bool(is_active),
        cooldown_seconds=cooldown,
        created_by_user_id=actor.id,
    )
    session.add(model)
    session.flush()
    log_audit_event(
        session,
        action="automation.rule_created",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="automation_rule",
        resource_id=model.id,
        event_data={"trigger_type": trigger, "action_type": action},
    )
    session.commit()
    session.refresh(model)
    return model


def set_rule_active(
    session: Session,
    *,
    context: WorkspaceContext,
    actor: AuthenticatedUser,
    rule_id: int,
    active: bool,
) -> AutomationRule:
    require_workspace_permission(context, "workspace:admin")
    model = session.get(AutomationRule, int(rule_id))
    if model is None:
        raise ValidationAppError("Automation rule was not found.")
    model.is_active = bool(active)
    log_audit_event(
        session,
        action="automation.rule_status_changed",
        actor_user_id=actor.id,
        actor_email=actor.email,
        resource_type="automation_rule",
        resource_id=model.id,
        event_data={"active": model.is_active},
    )
    session.commit()
    session.refresh(model)
    return model


def list_rules(session: Session, *, context: WorkspaceContext) -> list[AutomationRule]:
    require_workspace_permission(context, "workspace:read")
    return list(session.scalars(select(AutomationRule).order_by(AutomationRule.created_at.desc())).all())


def list_runs(
    session: Session, *, context: WorkspaceContext, limit: int = 200
) -> list[AutomationRun]:
    require_workspace_permission(context, "workspace:read")
    return list(
        session.scalars(
            select(AutomationRun)
            .order_by(AutomationRun.created_at.desc())
            .limit(min(max(int(limit), 1), 500))
        ).all()
    )


def _active_workspace_member(session: Session, workspace_id: int, user_id: int) -> bool:
    return bool(
        session.scalar(
            select(WorkspaceMembership.id).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
                WorkspaceMembership.status == "active",
            )
        )
    )


def _execute_action(
    session: Session,
    *,
    context: WorkspaceContext,
    rule: AutomationRule,
    payload: dict[str, Any],
) -> dict[str, Any]:
    config = _load_object(rule.action_config_json, field="Action configuration")
    action = rule.action_type

    if action == "create_notification":
        recipient_id = config.get("recipient_user_id")
        if recipient_id is not None and not _active_workspace_member(
            session, context.workspace_id, int(recipient_id)
        ):
            raise AutomationError("Notification recipient is not an active workspace member.")
        notification = create_notification(
            session,
            title=config["title"],
            message=config["message"],
            severity=config.get("severity", "info"),
            recipient_user_id=recipient_id,
            action_label=config.get("action_label"),
            action_page=config.get("action_page"),
            deduplication_key=f"automation:{rule.id}:{payload.get('event_key', 'event')}",
            commit=False,
        )
        return {"notification_id": notification.id}

    if action in {"assign_lead", "change_lead_status"}:
        lead_id = config.get("lead_id") or payload.get("lead_id")
        lead = session.get(LeadRecord, validate_positive_id(lead_id, field="Lead ID"))
        if lead is None:
            raise AutomationError("Automation target lead was not found.")
        if action == "assign_lead":
            user_id = int(config["user_id"])
            if not _active_workspace_member(session, context.workspace_id, user_id):
                raise AutomationError("Automation assignee is not an active workspace member.")
            lead.assigned_user_id = user_id
            return {"lead_id": lead.id, "assigned_user_id": user_id}
        lead.status = config["status"]
        return {"lead_id": lead.id, "status": lead.status}

    if action == "queue_integration_health_check":
        connection = session.get(IntegrationConnection, int(config["connection_id"]))
        if connection is None:
            raise AutomationError("Integration connection was not found.")
        job = enqueue_job(
            session,
            job_type="integration.health_check",
            payload={"connection_id": connection.id, "platform": connection.platform},
            priority=70,
            max_attempts=3,
            idempotency_key=f"automation-health:{rule.id}:{connection.id}:{_utc_now():%Y%m%d%H}",
            commit=False,
        )
        return {"job_id": job.id, "connection_id": connection.id}

    if action == "enqueue_publish":
        post_id = validate_positive_id(
            config.get("post_id") or payload.get("post_id"), field="Post ID"
        )
        post = session.get(Post, post_id)
        if post is None:
            raise AutomationError("Automation target post was not found.")
        if post.status not in PUBLISHABLE_STATUSES:
            raise AutomationError("Automation can publish only approved or scheduled posts.")
        job = enqueue_job(
            session,
            job_type="publishing.deliver",
            payload={"post_id": post.id},
            priority=80,
            max_attempts=3,
            idempotency_key=f"automation-publish:{rule.id}:{post.id}",
            commit=False,
        )
        return {"job_id": job.id, "post_id": post.id}

    if action == "invoke_webhook":
        connection = session.get(IntegrationConnection, int(config["connection_id"]))
        if connection is None or connection.platform != "website":
            raise AutomationError("Webhook automation requires a website integration connection.")
        endpoint = validate_http_url(
            connection.external_account_id,
            field="Webhook endpoint",
            required=True,
            allow_private=False,
        )
        if not endpoint.lower().startswith("https://"):
            raise AutomationError("Webhook endpoint must use HTTPS.")
        job = enqueue_job(
            session,
            job_type="automation.webhook_delivery",
            payload={
                "connection_id": connection.id,
                "endpoint": endpoint,
                "event": payload,
                "rule_id": rule.id,
            },
            priority=60,
            max_attempts=4,
            idempotency_key=f"automation-webhook:{rule.id}:{payload.get('event_key', '')}",
            commit=False,
        )
        return {"job_id": job.id, "connection_id": connection.id}

    raise AutomationError("Automation action is not implemented.")


def process_event(
    session: Session,
    *,
    context: WorkspaceContext,
    trigger_type: str,
    event_key: str,
    payload: dict[str, Any],
) -> list[AutomationRun]:
    trigger = str(trigger_type or "").strip().lower()
    if trigger not in TRIGGER_TYPES:
        raise ValidationAppError("Automation trigger is unsupported.")
    key = normalize_text(
        event_key, field="Event key", min_length=1, max_length=255, allow_newlines=False
    )
    if not isinstance(payload, dict):
        raise ValidationAppError("Automation payload must be an object.")
    safe_payload = json.loads(_safe_json(payload, field="Automation event"))
    safe_payload.setdefault("event_key", key)
    now = _utc_now()
    rules = list(
        session.scalars(
            select(AutomationRule).where(
                AutomationRule.trigger_type == trigger,
                AutomationRule.is_active.is_(True),
            )
        ).all()
    )
    runs: list[AutomationRun] = []

    for rule in rules:
        if session.scalar(
            select(AutomationRun.id).where(
                AutomationRun.rule_id == rule.id,
                AutomationRun.event_key == key,
            )
        ):
            continue
        run = AutomationRun(
            rule_id=rule.id,
            event_key=key,
            status="running",
            input_json=_safe_json(safe_payload, field="Automation event"),
        )
        session.add(run)
        session.flush()
        runs.append(run)
        try:
            if rule.last_triggered_at:
                last = rule.last_triggered_at
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
                if last + timedelta(seconds=rule.cooldown_seconds) > now:
                    run.status = "skipped"
                    run.output_json = '{"reason":"cooldown"}'
                    run.finished_at = now
                    continue
            if not matches_rule(rule, safe_payload):
                run.status = "skipped"
                run.output_json = '{"reason":"conditions_not_met"}'
                run.finished_at = now
                continue
            with session.begin_nested():
                output = _execute_action(
                    session, context=context, rule=rule, payload=safe_payload
                )
            run.status = "succeeded"
            run.output_json = _safe_json(output, field="Automation output")
            run.finished_at = _utc_now()
            rule.last_triggered_at = run.finished_at
        except Exception as exc:
            run.status = "failed"
            run.error_code = getattr(exc, "error_code", type(exc).__name__.upper())[:100]
            run.error_message = sanitize_log_message(
                getattr(exc, "message", str(exc))
            )[:2_000]
            run.finished_at = _utc_now()
        finally:
            log_audit_event(
                session,
                action="automation.rule_executed",
                outcome=(
                    "success"
                    if run.status == "succeeded"
                    else "warning"
                    if run.status == "skipped"
                    else "failure"
                ),
                resource_type="automation_run",
                resource_id=run.id,
                event_data={"rule_id": rule.id, "status": run.status, "event_key": key},
            )

    session.commit()
    for run in runs:
        session.refresh(run)
    return runs
