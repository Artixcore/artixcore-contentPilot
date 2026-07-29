"""Authenticated Streamlit sidebar and topbar layout."""

from __future__ import annotations

import html

import streamlit as st
from sqlalchemy.orm import Session

from core.auth import AuthenticatedUser
from ui.authentication import render_logout
from ui.navigation import SIDEBAR_WORKSPACES, available_labels


def render_sidebar(session: Session, user: AuthenticatedUser) -> str:
    """Render role-filtered navigation and return the selected page label."""
    nav_pages = available_labels(user)
    if not nav_pages:
        nav_pages = ["Dashboard"]

    with st.sidebar:
        st.title("Artixcore Pilot")
        st.caption("ContentPilot")

        if user.can("create_content"):
            if st.button("+ New Content", use_container_width=True, key="nav_new_content"):
                st.session_state["page"] = "Create Post"
                st.rerun()

        if st.button("Import", use_container_width=True, key="nav_import", disabled=True):
            st.info("Import is not enabled yet.")

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
        st.caption("Works / Projects")
        for workspace in SIDEBAR_WORKSPACES[:3]:
            st.button(
                workspace,
                use_container_width=True,
                key=f"workspace_{workspace}",
                disabled=True,
            )

        st.markdown("---")
        render_logout(session, user)

    return selected


def render_topbar(user: AuthenticatedUser) -> None:
    safe_name = html.escape(user.display_name)
    safe_role = html.escape(user.role.replace("_", " ").title())
    st.markdown(
        f"""
    <div class="cp-topbar">
      <div class="cp-topbar-title">Artixcore ContentPilot</div>
      <div class="cp-topbar-actions">
        <span class="cp-badge cp-badge-success">{safe_name}</span>
        <span class="cp-badge cp-badge-warning">{safe_role}</span>
      </div>
    </div>
    """,
        unsafe_allow_html=True,
    )
