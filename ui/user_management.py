"""Role-based user administration interface."""

from __future__ import annotations

import streamlit as st
from sqlalchemy.orm import Session

from core.auth import ROLES, AuthenticatedUser, create_user, require_permission
from core.error_handler import handle_exception
from core.user_admin import list_users, reset_user_password, set_user_active, update_user_role
from ui.components import page_header, section_title
from ui.notifications import show_error_from_dict, show_success, show_warning


def _role_label(role: str) -> str:
    return role.replace("_", " ").title()


def render_user_management(session: Session, actor: AuthenticatedUser) -> None:
    require_permission(actor, "manage_users")
    page_header("User Management", "Create accounts and manage roles, status, and session revocation.")

    section_title("Create User")
    with st.form("create_user_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            email = st.text_input("Email", max_chars=320)
            display_name = st.text_input("Display Name", max_chars=255)
        with c2:
            allowed_roles = sorted(ROLES)
            role = st.selectbox("Role", allowed_roles, format_func=_role_label)
            password = st.text_input("Temporary Password", type="password", max_chars=256)
        submitted = st.form_submit_button("Create Account", type="primary", use_container_width=True)

    if submitted:
        try:
            created = create_user(
                session,
                email=email,
                display_name=display_name,
                password=password,
                role=role,
                actor=actor,
            )
            show_success(f"Created {created.display_name} with the {_role_label(created.role)} role.")
            st.rerun()
        except Exception as exc:
            session.rollback()
            show_error_from_dict(handle_exception(exc, context="user.create"))

    section_title("Existing Users")
    try:
        users = list_users(session, actor)
    except Exception as exc:
        show_error_from_dict(handle_exception(exc, context="user.list"))
        return

    if not users:
        show_warning("No users were found.")
        return

    rows = [
        {
            "ID": user.id,
            "Name": user.display_name,
            "Email": user.email,
            "Role": _role_label(user.role),
            "Active": user.is_active,
            "MFA": user.mfa_enabled,
            "Last Login": user.last_login_at,
        }
        for user in users
    ]
    st.dataframe(rows, use_container_width=True, hide_index=True)

    selected_id = st.selectbox(
        "Select account",
        [user.id for user in users],
        format_func=lambda value: next(
            f"{user.display_name} ({user.email})" for user in users if user.id == value
        ),
    )
    selected = next(user for user in users if user.id == selected_id)

    with st.container(border=True):
        st.markdown(f"### Manage {selected.display_name}")
        c1, c2 = st.columns(2)
        with c1:
            new_role = st.selectbox(
                "Role",
                sorted(ROLES),
                index=sorted(ROLES).index(selected.role),
                format_func=_role_label,
                key=f"role_{selected.id}",
            )
            if st.button("Update Role", use_container_width=True, key=f"update_role_{selected.id}"):
                try:
                    update_user_role(
                        session,
                        user_id=selected.id,
                        role=new_role,
                        actor=actor,
                    )
                    show_success("Role updated. Existing permissions will be re-evaluated immediately.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    show_error_from_dict(handle_exception(exc, context="user.role_update"))

        with c2:
            desired_active = st.checkbox(
                "Account active",
                value=selected.is_active,
                key=f"active_{selected.id}",
            )
            if st.button("Update Status", use_container_width=True, key=f"update_status_{selected.id}"):
                try:
                    set_user_active(
                        session,
                        user_id=selected.id,
                        active=desired_active,
                        actor=actor,
                    )
                    show_success("Account status updated. Sessions were revoked when deactivated.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    show_error_from_dict(handle_exception(exc, context="user.status_update"))

        with st.form(f"password_reset_{selected.id}", clear_on_submit=True):
            new_password = st.text_input(
                "New Password",
                type="password",
                max_chars=256,
                key=f"password_{selected.id}",
            )
            reset = st.form_submit_button("Reset Password and Revoke Sessions", use_container_width=True)
        if reset:
            try:
                reset_user_password(
                    session,
                    user_id=selected.id,
                    new_password=new_password,
                    actor=actor,
                )
                show_success("Password reset and all active sessions revoked.")
            except Exception as exc:
                session.rollback()
                show_error_from_dict(handle_exception(exc, context="user.password_reset"))


def render(session: Session, actor: AuthenticatedUser) -> None:
    render_user_management(session, actor)
