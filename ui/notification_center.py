"""Authenticated in-app notification center."""

from __future__ import annotations

import streamlit as st
from sqlalchemy.orm import Session

from core.auth import AuthenticatedUser, require_permission
from core.error_handler import handle_exception
from core.notifications import list_notifications, mark_all_read, mark_notification_read
from ui.components import page_header
from ui.navigation import navigate
from ui.notifications import show_error_from_dict, show_info, show_success


def render_notification_center(session: Session, user: AuthenticatedUser) -> None:
    require_permission(user, "read")
    page_header(
        "Notifications",
        "Review operational alerts, failed jobs, integration problems, and account notices.",
    )
    unread_only = st.checkbox("Unread only", value=False)
    try:
        notifications = list_notifications(
            session,
            user=user,
            unread_only=unread_only,
            limit=250,
        )
    except Exception as exc:
        session.rollback()
        show_error_from_dict(handle_exception(exc, context="notifications.list"))
        return

    unread_count = sum(1 for item in notifications if not item.is_read)
    c1, c2 = st.columns([0.7, 0.3])
    with c1:
        st.metric("Unread Notifications", unread_count)
    with c2:
        if st.button("Mark All Read", use_container_width=True, disabled=unread_count == 0):
            try:
                changed = mark_all_read(session, user=user)
                show_success(f"Marked {changed} notification(s) as read.")
                st.rerun()
            except Exception as exc:
                session.rollback()
                show_error_from_dict(handle_exception(exc, context="notifications.mark_all"))

    if not notifications:
        show_info("No notifications match this filter.")
        return

    for item in notifications:
        icon = {
            "critical": "🚨",
            "error": "❌",
            "warning": "⚠️",
            "success": "✅",
            "info": "ℹ️",
        }.get(item.severity, "ℹ️")
        with st.container(border=True):
            st.markdown(f"### {icon} {item.title}")
            st.write(item.message)
            st.caption(
                f"Severity: {item.severity.title()} | Created: {item.created_at}"
            )
            action_col, read_col = st.columns(2)
            with action_col:
                if item.action_page and st.button(
                    item.action_label or "Open",
                    key=f"notification_action_{item.id}",
                    use_container_width=True,
                ):
                    try:
                        mark_notification_read(
                            session,
                            notification_id=item.id,
                            user=user,
                        )
                    except Exception:
                        session.rollback()
                    navigate(item.action_page)
            with read_col:
                if not item.is_read and st.button(
                    "Mark Read",
                    key=f"notification_read_{item.id}",
                    use_container_width=True,
                ):
                    try:
                        mark_notification_read(
                            session,
                            notification_id=item.id,
                            user=user,
                        )
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        show_error_from_dict(
                            handle_exception(exc, context="notifications.mark_read")
                        )


def render(session: Session, user: AuthenticatedUser) -> None:
    render_notification_center(session, user)
