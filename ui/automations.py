"""Workspace automation rule builder, test runner, and execution history."""

from __future__ import annotations

import json
import secrets

import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.auth import AuthenticatedUser
from core.automation_service import (
    ACTION_TYPES,
    TRIGGER_TYPES,
    create_rule,
    list_rules,
    list_runs,
    process_event,
    set_rule_active,
)
from core.error_handler import handle_exception
from core.operations_models import IntegrationConnection
from core.security_models import UserAccount
from core.tenant_models import WorkspaceMembership
from core.tenancy import WorkspaceContext
from ui.notifications import show_error_from_dict, show_success


def _handle(exc: Exception, context: str) -> None:
    show_error_from_dict(handle_exception(exc, context=context))


def _parse_json(value: str, field: str) -> dict:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field} must contain valid JSON: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field} must be a JSON object.")
    return parsed


def _workspace_members(session: Session, workspace_id: int) -> dict[str, int]:
    rows = session.execute(
        select(UserAccount, WorkspaceMembership)
        .join(WorkspaceMembership, WorkspaceMembership.user_id == UserAccount.id)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.status == "active",
            UserAccount.is_active.is_(True),
        )
        .order_by(UserAccount.display_name)
    ).all()
    return {f"{account.display_name} ({account.email})": account.id for account, _ in rows}


def _connections(session: Session) -> dict[str, int]:
    models = session.scalars(
        select(IntegrationConnection)
        .where(IntegrationConnection.status != "disabled")
        .order_by(IntegrationConnection.platform, IntegrationConnection.display_name)
    ).all()
    return {
        f"{model.platform.title()} · {model.display_name} · {model.status.title()}": model.id
        for model in models
    }


def render_automations(
    session: Session,
    user: AuthenticatedUser,
    workspace: WorkspaceContext,
) -> None:
    st.header("Automation")
    st.caption(
        "Rules use fixed triggers, operators, and actions. ContentPilot does not execute arbitrary code or dynamic imports."
    )

    rules_tab, test_tab, history_tab = st.tabs(["Rules", "Test Event", "History"])

    with rules_tab:
        rules = list_rules(session, context=workspace)
        if workspace.can("workspace:admin"):
            with st.expander("Create automation rule", expanded=not rules):
                trigger = st.selectbox("Trigger", sorted(TRIGGER_TYPES), key="automation_trigger")
                action = st.selectbox("Action", sorted(ACTION_TYPES), key="automation_action")
                with st.form("automation_rule_form"):
                    name = st.text_input("Rule name", max_chars=255)
                    cooldown = st.number_input(
                        "Cooldown seconds", min_value=0, max_value=86_400, value=60
                    )
                    conditions_text = st.text_area(
                        "Conditions JSON",
                        value="{}",
                        max_chars=50_000,
                        help=(
                            'Example: {"score":{"operator":"gte","value":70},'
                            '"status":{"operator":"equals","value":"qualified"}}'
                        ),
                    )

                    action_config: dict = {}
                    if action == "create_notification":
                        action_config["title"] = st.text_input(
                            "Notification title", max_chars=255
                        )
                        action_config["message"] = st.text_area(
                            "Notification message", max_chars=10_000
                        )
                        action_config["severity"] = st.selectbox(
                            "Severity", ["info", "success", "warning", "error", "critical"]
                        )
                    elif action == "assign_lead":
                        members = _workspace_members(session, workspace.workspace_id)
                        if members:
                            member = st.selectbox("Assign to", list(members))
                            action_config["user_id"] = members[member]
                        action_config["lead_id"] = int(
                            st.number_input(
                                "Fixed lead ID, optional",
                                min_value=0,
                                value=0,
                                help="Use 0 to read lead_id from the event payload.",
                            )
                        ) or None
                    elif action == "change_lead_status":
                        action_config["status"] = st.selectbox(
                            "New lead status",
                            ["new", "qualified", "contacted", "proposal", "won", "lost", "spam", "archived"],
                        )
                        action_config["lead_id"] = int(
                            st.number_input("Fixed lead ID, optional", min_value=0, value=0)
                        ) or None
                    elif action in {"queue_integration_health_check", "invoke_webhook"}:
                        connections = _connections(session)
                        if connections:
                            connection = st.selectbox("Integration connection", list(connections))
                            action_config["connection_id"] = connections[connection]
                    elif action == "enqueue_publish":
                        action_config["post_id"] = int(
                            st.number_input(
                                "Fixed post ID, optional",
                                min_value=0,
                                value=0,
                                help="Use 0 to read post_id from the event payload.",
                            )
                        ) or None

                    advanced_config_text = st.text_area(
                        "Additional action configuration JSON",
                        value="{}",
                        max_chars=20_000,
                        help="Optional values are merged with the fields above. Fixed protected fields win.",
                    )
                    active = st.checkbox("Active", value=True)
                    submitted = st.form_submit_button("Create rule", type="primary")
                if submitted:
                    try:
                        conditions = _parse_json(conditions_text, "Conditions")
                        advanced = _parse_json(
                            advanced_config_text, "Additional action configuration"
                        )
                        final_config = {**advanced, **action_config}
                        final_config = {
                            key: value for key, value in final_config.items() if value is not None
                        }
                        create_rule(
                            session,
                            context=workspace,
                            actor=user,
                            name=name,
                            trigger_type=trigger,
                            conditions=conditions,
                            action_type=action,
                            action_config=final_config,
                            cooldown_seconds=int(cooldown),
                            is_active=active,
                        )
                        show_success("Automation rule created.")
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        _handle(exc, "automation.create")

        for rule in rules:
            with st.container(border=True):
                columns = st.columns([3, 2, 2, 1])
                columns[0].markdown(f"**{rule.name}**")
                columns[0].caption(f"{rule.trigger_type} → {rule.action_type}")
                columns[1].write("Active" if rule.is_active else "Disabled")
                columns[2].write(f"Cooldown: {rule.cooldown_seconds}s")
                if workspace.can("workspace:admin"):
                    if columns[3].button(
                        "Disable" if rule.is_active else "Enable",
                        key=f"automation_toggle_{rule.id}",
                    ):
                        try:
                            set_rule_active(
                                session,
                                context=workspace,
                                actor=user,
                                rule_id=rule.id,
                                active=not rule.is_active,
                            )
                            show_success("Automation rule updated.")
                            st.rerun()
                        except Exception as exc:
                            session.rollback()
                            _handle(exc, "automation.toggle")
                with st.expander("Configuration"):
                    st.code(rule.conditions_json, language="json")
                    st.code(rule.action_config_json, language="json")

    with test_tab:
        if not workspace.can("workspace:admin"):
            st.info("Only workspace owners and administrators can execute test events.")
        else:
            with st.form("automation_test_event"):
                trigger = st.selectbox("Event trigger", sorted(TRIGGER_TYPES))
                event_key = st.text_input(
                    "Unique event key",
                    value=f"manual-{secrets.token_hex(8)}",
                    max_chars=255,
                )
                payload_text = st.text_area(
                    "Event payload JSON", value='{"status":"qualified","score":80}', max_chars=50_000
                )
                execute = st.form_submit_button("Process event", type="primary")
            if execute:
                try:
                    runs = process_event(
                        session,
                        context=workspace,
                        trigger_type=trigger,
                        event_key=event_key,
                        payload=_parse_json(payload_text, "Event payload"),
                    )
                    show_success(f"Event processed across {len(runs)} matching rule candidate(s).")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    _handle(exc, "automation.test")

    with history_tab:
        runs = list_runs(session, context=workspace)
        if not runs:
            st.info("No automation runs have been recorded.")
        for run in runs:
            with st.container(border=True):
                columns = st.columns([2, 2, 2, 2])
                columns[0].write(f"Rule #{run.rule_id}")
                columns[1].write(run.event_key)
                columns[2].write(run.status.title())
                columns[3].write(run.created_at)
                if run.error_code:
                    st.error(f"{run.error_code}: {run.error_message or 'Automation failed.'}")
                elif run.output_json:
                    st.code(run.output_json, language="json")
