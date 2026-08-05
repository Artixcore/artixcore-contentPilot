"""Streamlit control panel for Instagram intelligence and the five-agent team."""

from __future__ import annotations

import json

import pandas as pd
import streamlit as st
from sqlalchemy.orm import Session

from core.apify_instagram import parse_competitor_text
from core.content_agent_team import (
    content_agent_security_findings,
    get_content_agent_dashboard,
    get_content_agent_settings,
    run_content_agent_team,
    run_full_content_cycle,
    save_content_agent_settings,
    sync_instagram_intelligence,
)
from core.error_handler import handle_exception
from core.router import ProviderRouter
from core.tenancy import WorkspaceContext
from ui.components import page_header, section_title

_AGENT_ORDER = (
    "ideator",
    "hook_script",
    "planner",
    "analyst",
    "dm_manager",
)


def _render_error(exc: BaseException, *, context: str) -> None:
    error = handle_exception(exc, context=context)
    st.error(error["message"])
    st.caption(f"Error code: {error['error_code']}")
    if error.get("user_action"):
        st.info(error["user_action"])
    if error.get("retryable"):
        st.warning(
            "This failure may be temporary. Retry after checking the connector status."
        )


def _settings_defaults(session: Session) -> dict:
    settings = get_content_agent_settings(session)
    competitors: list[str] = []
    if settings and settings.competitor_handles_json:
        try:
            value = json.loads(settings.competitor_handles_json)
            if isinstance(value, list):
                competitors = [str(item) for item in value]
        except json.JSONDecodeError:
            competitors = []
    return {
        "settings": settings,
        "own_handle": (
            settings.own_instagram_handle if settings else ""
        ),
        "competitors": "\n".join(competitors),
        "actor_id": (
            settings.apify_actor_id
            if settings
            else "apify/instagram-scraper"
        ),
        "posts_per_profile": (
            settings.posts_per_profile if settings else 30
        ),
        "schedule_enabled": (
            settings.schedule_enabled if settings else False
        ),
        "minimum_interval_minutes": (
            settings.minimum_interval_minutes if settings else 1440
        ),
        "telegram_reports_enabled": (
            settings.telegram_reports_enabled if settings else False
        ),
    }


def _render_configuration(session: Session) -> None:
    defaults = _settings_defaults(session)
    with st.expander(
        "Agent team configuration",
        expanded=defaults["settings"] is None,
    ):
        st.caption(
            "API tokens stay in environment variables. This form stores handles, "
            "bounded limits, and scheduling preferences only."
        )
        with st.form("content_agent_settings_form"):
            c1, c2 = st.columns(2)
            with c1:
                own_handle = st.text_input(
                    "Your Instagram handle",
                    value=defaults["own_handle"],
                    placeholder="artixcore",
                    max_chars=31,
                )
                competitors = st.text_area(
                    "Competitor handles (3 to 5 recommended)",
                    value=defaults["competitors"],
                    placeholder="competitor_one\ncompetitor_two",
                    height=150,
                    max_chars=1_000,
                )
                actor_id = st.text_input(
                    "Apify actor ID",
                    value=defaults["actor_id"],
                    max_chars=255,
                    help=(
                        "Must be listed in APIFY_ALLOWED_ACTOR_IDS. "
                        "Default: apify/instagram-scraper"
                    ),
                )
            with c2:
                posts_per_profile = st.number_input(
                    "Posts per profile",
                    min_value=1,
                    max_value=100,
                    value=int(defaults["posts_per_profile"]),
                    step=1,
                )
                schedule_enabled = st.checkbox(
                    "Enable scheduled cycles",
                    value=bool(defaults["schedule_enabled"]),
                )
                interval = st.number_input(
                    "Minimum minutes between cycles",
                    min_value=15,
                    max_value=1_440,
                    value=int(defaults["minimum_interval_minutes"]),
                    step=15,
                    disabled=not schedule_enabled,
                )
                telegram_enabled = st.checkbox(
                    "Send Telegram report after scheduled cycles",
                    value=bool(
                        defaults["telegram_reports_enabled"]
                    ),
                )
            submitted = st.form_submit_button(
                "Save configuration",
                type="primary",
                use_container_width=True,
            )

        if submitted:
            try:
                parsed_competitors = parse_competitor_text(
                    competitors,
                    own_handle=own_handle,
                )
                save_content_agent_settings(
                    session,
                    own_handle=own_handle,
                    competitor_handles=parsed_competitors,
                    apify_actor_id=actor_id,
                    posts_per_profile=posts_per_profile,
                    schedule_enabled=schedule_enabled,
                    minimum_interval_minutes=interval,
                    telegram_reports_enabled=telegram_enabled,
                )
                st.success("Content Agent Team configuration saved.")
                st.rerun()
            except Exception as exc:
                _render_error(
                    exc,
                    context="content_agent_settings_save",
                )


def _render_security_posture(session: Session) -> None:
    settings = get_content_agent_settings(session)
    provider_available = ProviderRouter(
        session=session
    ).has_any_provider()
    findings = content_agent_security_findings(
        settings,
        provider_available=provider_available,
    )
    with st.expander(
        "Validation and vulnerability alerts",
        expanded=True,
    ):
        for finding in findings:
            message = (
                f"**{finding['title']}**\n\n"
                f"{finding['message']}\n\n"
                f"Action: {finding['action']}"
            )
            severity = finding["severity"]
            if severity == "critical":
                st.error(message)
            elif severity == "warning":
                st.warning(message)
            elif severity == "success":
                st.success(message)
            else:
                st.info(message)


def _render_actions(session: Session) -> None:
    settings = get_content_agent_settings(session)
    disabled = settings is None
    section_title("Run the workflow")
    c1, c2, c3 = st.columns(3)
    sync_clicked = c1.button(
        "1. Sync Instagram data",
        use_container_width=True,
        disabled=disabled,
    )
    agents_clicked = c2.button(
        "2. Run five agents",
        use_container_width=True,
        disabled=disabled,
    )
    cycle_clicked = c3.button(
        "Full cycle + Telegram",
        type="primary",
        use_container_width=True,
        disabled=disabled,
    )

    if sync_clicked:
        try:
            with st.spinner(
                "Syncing owned and competitor Instagram data through Apify..."
            ):
                summary = sync_instagram_intelligence(
                    session,
                    settings=settings,
                )
            st.success(
                f"Synced {summary['profiles']} profiles and upserted "
                f"{summary['posts_upserted']} post records."
            )
            st.rerun()
        except Exception as exc:
            _render_error(
                exc,
                context="content_agent_instagram_sync",
            )

    if agents_clicked:
        try:
            with st.spinner(
                "Running Ideator, Hook & Script, Planner, Analyst, and DM Manager..."
            ):
                result = run_content_agent_team(
                    session,
                    settings=settings,
                )
            st.success(
                f"Agent cycle {result['cycle_id']} completed: "
                f"{result['succeeded']} succeeded, "
                f"{result['failed']} failed."
            )
            st.rerun()
        except Exception as exc:
            _render_error(
                exc,
                context="content_agent_team_run",
            )

    if cycle_clicked:
        try:
            with st.spinner(
                "Running data sync, five agents, and Telegram reporting..."
            ):
                result = run_full_content_cycle(
                    session,
                    sync_data=True,
                    send_telegram=True,
                )
            if result["telegram_delivered"]:
                st.success(
                    "Full cycle completed and report delivered to "
                    f"{result['telegram_delivered']} Telegram recipient(s)."
                )
            else:
                st.warning(
                    "The agent cycle completed, but Telegram delivery did not succeed."
                )
                if result.get("telegram_error"):
                    st.caption(
                        result["telegram_error"].get(
                            "message",
                            "Telegram delivery failed.",
                        )
                    )
            st.rerun()
        except Exception as exc:
            _render_error(
                exc,
                context="content_agent_full_cycle",
            )


def _render_agent_cards(dashboard: dict) -> None:
    section_title("Five-agent workspace")
    columns = st.columns(2)
    for index, key in enumerate(_AGENT_ORDER):
        agent = dashboard["agents"][key]
        with columns[index % 2]:
            with st.container(border=True):
                st.subheader(agent["label"])
                status = agent["status"]
                if status == "succeeded":
                    st.success("Succeeded")
                elif status == "failed":
                    st.error(
                        f"Failed: {agent.get('error_code') or 'UNKNOWN'}"
                    )
                    if agent.get("error_message"):
                        st.caption(agent["error_message"])
                elif status == "running":
                    st.info("Running")
                else:
                    st.caption("Not run yet")
                if agent.get("provider"):
                    st.caption(
                        f"Provider: {agent['provider']} | "
                        f"Model: {agent.get('model') or 'unknown'}"
                    )
                payload = agent.get("payload") or {}
                if payload:
                    st.write(payload.get("summary", "Agent output"))
                    st.json(payload, expanded=False)


def _render_top_posts(dashboard: dict) -> None:
    section_title("Evidence from real post data")
    posts = dashboard.get("top_posts") or []
    if not posts:
        st.info("No Instagram post data has been imported yet.")
        return
    frame = pd.DataFrame(posts)
    visible_columns = [
        "handle",
        "owned",
        "type",
        "caption",
        "likes",
        "comments",
        "shares",
        "views",
        "engagement_rate",
        "published_at",
        "permalink",
    ]
    st.dataframe(
        frame[visible_columns],
        use_container_width=True,
        hide_index=True,
        column_config={
            "permalink": st.column_config.LinkColumn("Post"),
            "engagement_rate": st.column_config.NumberColumn(
                "Engagement",
                format="%.4f",
            ),
        },
    )


def render_content_agent_team(
    session: Session,
    workspace: WorkspaceContext,
) -> None:
    page_header(
        "Content Agent Team",
        "Competitor intelligence, five coordinated AI agents, draft planning, and Telegram reports.",
    )
    st.caption(
        f"Workspace: {workspace.workspace_name}. "
        "All imported and generated records are tenant scoped."
    )

    _render_configuration(session)
    _render_security_posture(session)
    _render_actions(session)

    dashboard = get_content_agent_dashboard(session)
    m1, m2, m3 = st.columns(3)
    m1.metric("Tracked profiles", dashboard["profile_count"])
    m2.metric("Imported posts", dashboard["post_count"])
    m3.metric(
        "Latest cycle",
        dashboard["latest_cycle_id"] or "Not run",
    )

    _render_agent_cards(dashboard)
    _render_top_posts(dashboard)

    with st.expander("Scheduled runner", expanded=False):
        st.code(
            "# Run every 15 minutes; ContentPilot enforces the configured interval\n"
            "*/15 * * * * cd /app && python scripts/run_content_agent_cycle.py",
            language="bash",
        )
        st.caption(
            "The runner processes only workspaces with scheduling enabled and "
            "does not publish content automatically."
        )


def render(
    session: Session,
    workspace: WorkspaceContext,
) -> None:
    render_content_agent_team(session, workspace)
