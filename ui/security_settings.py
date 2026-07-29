"""Account security, encrypted credential vault, and audit review UI."""

from __future__ import annotations

import json

import streamlit as st
from sqlalchemy.orm import Session

from core.account_security import (
    change_own_password,
    disable_mfa,
    list_audit_events,
    purge_expired_sessions,
)
from core.auth import (
    AuthenticatedUser,
    begin_mfa_enrollment,
    confirm_mfa_enrollment,
)
from core.credential_store import (
    list_credential_metadata,
    rotate_credential_key,
    set_credential_active,
    store_credential,
)
from core.error_handler import handle_exception
from ui.authentication import clear_auth_state
from ui.components import page_header, section_title
from ui.notifications import show_error_from_dict, show_info, show_success, show_warning

_MFA_SECRET_KEY = "cp_pending_mfa_secret"
_MFA_URI_KEY = "cp_pending_mfa_uri"


def _handle_error(exc: Exception, context: str) -> None:
    show_error_from_dict(handle_exception(exc, context=context))


def _render_account_security(session: Session, user: AuthenticatedUser) -> None:
    section_title("Account Security")
    st.write(f"**Account:** {user.display_name} ({user.email})")
    st.write(f"**Role:** {user.role.replace('_', ' ').title()}")
    st.write(f"**MFA:** {'Enabled' if user.mfa_enabled else 'Disabled'}")

    with st.expander("Change password"):
        with st.form("change_own_password", clear_on_submit=True):
            current_password = st.text_input("Current Password", type="password", max_chars=256)
            new_password = st.text_input("New Password", type="password", max_chars=256)
            confirm_password = st.text_input("Confirm New Password", type="password", max_chars=256)
            submitted = st.form_submit_button(
                "Change Password and Revoke Sessions",
                use_container_width=True,
            )
        if submitted:
            if new_password != confirm_password:
                show_warning("New password confirmation does not match.")
            else:
                try:
                    change_own_password(
                        session,
                        user=user,
                        current_password=current_password,
                        new_password=new_password,
                    )
                    clear_auth_state()
                    show_success("Password changed. Sign in again with the new password.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    _handle_error(exc, "auth.password_change")

    if not user.mfa_enabled:
        with st.expander("Enable authenticator MFA"):
            show_info(
                "MFA secrets are encrypted at rest. Keep the deployment encryption keys available during rotation."
            )
            if st.button("Start MFA Enrollment", use_container_width=True):
                try:
                    secret, uri = begin_mfa_enrollment(session, user)
                    st.session_state[_MFA_SECRET_KEY] = secret
                    st.session_state[_MFA_URI_KEY] = uri
                except Exception as exc:
                    session.rollback()
                    _handle_error(exc, "auth.mfa_begin")

            secret = st.session_state.get(_MFA_SECRET_KEY)
            uri = st.session_state.get(_MFA_URI_KEY)
            if secret and uri:
                st.warning("Add this account to your authenticator before leaving this page.")
                st.code(secret)
                st.text_area("Authenticator URI", value=uri, disabled=True, height=100)
                with st.form("confirm_mfa_enrollment"):
                    code = st.text_input("Six-digit code", max_chars=8)
                    confirm = st.form_submit_button("Confirm MFA", type="primary")
                if confirm:
                    try:
                        confirm_mfa_enrollment(session, user, code)
                        st.session_state.pop(_MFA_SECRET_KEY, None)
                        st.session_state.pop(_MFA_URI_KEY, None)
                        show_success("MFA enabled successfully.")
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        _handle_error(exc, "auth.mfa_confirm")
    else:
        with st.expander("Disable authenticator MFA"):
            with st.form("disable_mfa", clear_on_submit=True):
                password = st.text_input("Password", type="password", max_chars=256)
                code = st.text_input("Current authentication code", max_chars=8)
                disable = st.form_submit_button("Disable MFA")
            if disable:
                try:
                    disable_mfa(session, user=user, password=password, totp_code=code)
                    show_success("MFA disabled.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    _handle_error(exc, "auth.mfa_disable")


def _render_credential_vault(session: Session, user: AuthenticatedUser) -> None:
    if not user.can("manage_security"):
        return

    section_title("Encrypted Credential Vault")
    show_info(
        "Credential plaintext is encrypted before database storage and is never included in logs or tables shown here."
    )

    with st.form("credential_store", clear_on_submit=True):
        c1, c2 = st.columns(2)
        with c1:
            name = st.text_input(
                "Credential Name",
                placeholder="linkedin.access_token",
                max_chars=255,
            )
            credential_type = st.selectbox(
                "Type",
                ["api_token", "oauth_access_token", "oauth_refresh_token", "client_secret", "webhook_secret", "secret"],
            )
        with c2:
            secret_value = st.text_area("Secret Value", height=120, max_chars=100_000)
        save = st.form_submit_button("Encrypt and Store", type="primary", use_container_width=True)

    if save:
        try:
            model = store_credential(
                session,
                name=name,
                secret_value=secret_value,
                credential_type=credential_type,
                actor=user,
            )
            show_success(
                f"Credential stored as version {model.version} with encryption key {model.key_id}."
            )
            st.rerun()
        except Exception as exc:
            session.rollback()
            _handle_error(exc, "credential.store")

    try:
        credentials = list_credential_metadata(session, actor=user)
    except Exception as exc:
        session.rollback()
        _handle_error(exc, "credential.list")
        return

    if not credentials:
        st.caption("No encrypted credentials stored.")
        return

    st.dataframe(
        [
            {
                "Name": item.credential_name,
                "Type": item.credential_type,
                "Version": item.version,
                "Key ID": item.key_id,
                "Active": item.is_active,
                "Rotated": item.rotated_at,
            }
            for item in credentials
        ],
        use_container_width=True,
        hide_index=True,
    )

    selected_name = st.selectbox(
        "Select credential",
        [item.credential_name for item in credentials],
    )
    selected = next(item for item in credentials if item.credential_name == selected_name)
    c1, c2 = st.columns(2)
    with c1:
        if st.button("Re-encrypt with Active Key", use_container_width=True):
            try:
                rotate_credential_key(session, name=selected_name, actor=user)
                show_success("Credential re-encrypted with the active key.")
                st.rerun()
            except Exception as exc:
                session.rollback()
                _handle_error(exc, "credential.key_rotate")
    with c2:
        desired_active = not selected.is_active
        label = "Reactivate" if desired_active else "Deactivate"
        if st.button(label, use_container_width=True):
            try:
                set_credential_active(
                    session,
                    name=selected_name,
                    active=desired_active,
                    actor=user,
                )
                show_success(f"Credential {label.lower()}d.")
                st.rerun()
            except Exception as exc:
                session.rollback()
                _handle_error(exc, "credential.status")


def _render_audit(session: Session, user: AuthenticatedUser) -> None:
    if not (user.can("view_audit") or user.can("manage_security")):
        return

    section_title("Security Audit Trail")
    c1, c2 = st.columns([0.7, 0.3])
    with c1:
        limit = st.selectbox("Events", [50, 100, 250, 500], index=1)
    with c2:
        if user.can("manage_security") and st.button("Purge Expired Sessions", use_container_width=True):
            try:
                count = purge_expired_sessions(session, actor=user)
                show_success(f"Purged {count} expired sessions.")
            except Exception as exc:
                session.rollback()
                _handle_error(exc, "auth.session_cleanup")

    try:
        events = list_audit_events(session, actor=user, limit=limit)
    except Exception as exc:
        session.rollback()
        _handle_error(exc, "audit.list")
        return

    rows = []
    for event in events:
        try:
            data = json.loads(event.event_data or "{}")
        except json.JSONDecodeError:
            data = {}
        rows.append(
            {
                "Time": event.created_at,
                "Action": event.action,
                "Outcome": event.outcome,
                "Actor": event.actor_email or event.actor_user_id,
                "Resource": f"{event.resource_type or ''}:{event.resource_id or ''}".strip(":"),
                "Request ID": event.request_id,
                "Details": json.dumps(data, ensure_ascii=False)[:500],
            }
        )
    st.dataframe(rows, use_container_width=True, hide_index=True)


def render_security_settings(session: Session, user: AuthenticatedUser) -> None:
    page_header(
        "Security",
        "Manage your password, MFA, encrypted credentials, sessions, and audit trail.",
    )
    _render_account_security(session, user)
    _render_credential_vault(session, user)
    _render_audit(session, user)


def render(session: Session, user: AuthenticatedUser) -> None:
    render_security_settings(session, user)
