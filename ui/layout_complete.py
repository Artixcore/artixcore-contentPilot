"""Complete authenticated workspace shell for ContentPilot."""

from __future__ import annotations

import html

import streamlit as st
from sqlalchemy.orm import Session

from core.auth import AuthenticatedUser
from core.tenancy import WorkspaceContext
from ui.authentication import render_logout
from ui.navigation_complete import available_labels


def render_sidebar(
    session: Session,
    user: AuthenticatedUser,
    workspace: WorkspaceContext,
    available_workspaces: list[WorkspaceContext],
) -> str:
    pages = available_labels(user, workspace) or ["Dashboard"]
    with st.sidebar:
        st.title("Artixcore Pilot")
        st.caption("ContentPilot")

        labels = [
            f"{item.organization_name} / {item.workspace_name}" for item in available_workspaces
        ]
        current_index = next(
            (
                index
                for index, item in enumerate(available_workspaces)
                if item.workspace_id == workspace.workspace_id
            ),
            0,
        )
        selected_label = st.selectbox(
            "Workspace",
            labels,
            index=current_index,
            key="complete_workspace_selector",
        )
        selected_workspace = available_workspaces[labels.index(selected_label)]
        if selected_workspace.workspace_id != workspace.workspace_id:
            st.session_state["active_workspace_id"] = selected_workspace.workspace_id
            st.session_state.pop("complete_page_radio", None)
            st.rerun()

        st.caption(f"Workspace role: {workspace.role.replace('_', ' ').title()}")
        if "Create Post" in pages and st.button(
            "+ New Content", use_container_width=True, key="complete_new_content"
        ):
            st.session_state["complete_forced_page"] = "Create Post"
            st.rerun()

        st.text_input(
            "Search",
            placeholder="Search pages...",
            label_visibility="collapsed",
            max_chars=200,
            key="complete_sidebar_search",
        )

        current = st.session_state.get("complete_page_radio")
        default_index = pages.index(current) if current in pages else 0
        forced = st.session_state.pop("complete_forced_page", None)
        if forced in pages:
            default_index = pages.index(forced)
            st.session_state["complete_page_radio"] = forced
        elif forced:
            st.warning("You do not have permission to open that page.")

        selected = st.radio(
            "Navigation",
            pages,
            index=default_index,
            key="complete_page_radio",
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
