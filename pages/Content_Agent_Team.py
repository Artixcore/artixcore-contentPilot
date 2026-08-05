"""Authenticated Streamlit page for the Content Agent Team."""

from __future__ import annotations

import streamlit as st

from core.auth import AuthenticatedUser
from core.content_intelligence_models import ContentAgentSettings  # noqa: F401
from core.database import get_engine, get_session
from core.error_handler import handle_exception
from core.migrations import run_migrations
from core.tenancy import WorkspaceContext
from core.workspace_permissions import can_access
from ui.content_agent_team import render_content_agent_team


def _find_state_value(expected_type: type, *preferred_keys: str):
    for key in preferred_keys:
        value = st.session_state.get(key)
        if isinstance(value, expected_type):
            return value
    for value in st.session_state.values():
        if isinstance(value, expected_type):
            return value
    return None


user = _find_state_value(
    AuthenticatedUser,
    "authenticated_user",
    "current_user",
    "user",
)
workspace = _find_state_value(
    WorkspaceContext,
    "workspace_context",
    "current_workspace",
    "active_workspace",
)

if user is None or workspace is None:
    st.error(
        "Open this page from an authenticated ContentPilot workspace. "
        "Direct unauthenticated access is blocked."
    )
    st.stop()

if not can_access(user, workspace, "manage_integrations"):
    st.error(
        "You do not have permission to manage content intelligence integrations "
        "for this workspace."
    )
    st.stop()

try:
    run_migrations(get_engine())
except Exception as exc:
    error = handle_exception(
        exc,
        context="content_agent_page_migrations",
    )
    st.error(error["message"])
    st.caption(f"Error code: {error['error_code']}")
    st.stop()

session = get_session(workspace)
try:
    render_content_agent_team(session, workspace)
finally:
    session.close()
