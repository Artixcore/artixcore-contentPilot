"""Authenticated Streamlit sidebar and topbar layout."""

from __future__ import annotations

import html

import streamlit as st
from sqlalchemy.orm import Session

from core.auth import AuthenticatedUser
from core.tenancy import WorkspaceContext
from ui.authentication import render_logout
from ui.navigation import available_labels


def render_sidebar(
    session: Session,
    user: AuthenticatedUser,
    workspace: WorkspaceContext,
    available_workspaces: list[WorkspaceContext],
) -> str:
    """Render workspace switcher, role-filtered navigation, and account actions."""
    nav_pages = available_labels(user, workspace) or ["Dashboard"]

    with st.sidebar:
        st.title("Artixcore Pilot")
        st.caption("ContentPilot")

        workspace_labels = [
            f"{item.organization_name} / {item.workspace_name}" for item in available_workspaces
        ]
        selected_index = next(
            (
                index
                for index, item in enumerate(available_workspaces)
                if item.workspace_id == workspace.workspace_id
            ),
            0,
        )
        selected_label = st.selectbox(
            "Workspace",
            workspace_labels,
            index=selected_index,
            key="workspace_selector",
        )
        selected_workspace = available_workspaces[workspace_labels.index(selected_label)]
        if selected_workspace.workspace_id != workspace.workspace_id:
            st.session_state["active_workspace_id"] = selected_workspace.workspace_id
            st.session_state.pop("page_radio", None)
            st.rerun()

        st.caption(f"Workspace role: {workspace.role.replace('_', ' ').title()}")

        if "Create Post" in nav_pages:
            if st.button("+ New Content", use_container_width=True, key="nav_new_content"):
                st.session_state["page"] = "Create Post"
                st.rerun()

        st.text_input(
            "Search",
            placeholder="Search...",
            label_visibility="collapsed",
            key="sidebar_search",
            max_chars=200,
        )

        current = st.session_state.get("page_radio")
        default_index = nav_pages.index(current) if current in nav_pages else 0
        forced = st.session_state.pop("page", None)
        if forced in nav_pages:
            default_index = nav_pages.index(forced)
            st.session_state["page_radio"] = forced
        elif forced:
            st.warning("You do not have permission to open that page.")

        selected = st.radio(
            "Navigation",
            nav_pages,
            index=default_index,
            key="page_radio",
            label_visibility="collapsed",
        )

        st.markdown("---")
        render_logout(session, user)

    return selected


def render_topbar(user: AuthenticatedUser, workspace: WorkspaceContext) -> None:
    safe_name = html.escape(user.display_name)
    safe_role = html.escape(user.role.replace("_", " ").title())
    safe_workspace = html.escape(workspace.workspace_name)
    safe_workspace_role = html.escape(workspace.role.replace("_", " ").title())
    st.markdown(
        f"""
    <div class="cp-topbar">
      <div class="cp-topbar-title">Artixcore ContentPilot · {safe_workspace}</div>
      <div class="cp-topbar-actions">
        <span class="cp-badge cp-badge-success">{safe_name}</span>
        <span class="cp-badge cp-badge-warning">{safe_role}</span>
        <span class="cp-badge">{safe_workspace_role}</span>
      </div>
    </div>
    """,
        unsafe_allow_html=True,
    )
