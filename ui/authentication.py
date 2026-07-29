"""Streamlit authentication boundary and session-state integration."""

from __future__ import annotations

import os
from typing import Mapping

import streamlit as st
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.auth import AuthenticatedUser, authenticate_user, logout, resolve_session
from core.error_handler import handle_exception
from core.security_models import UserAccount
from ui.notifications import show_error_from_dict, show_info, show_success

_SESSION_TOKEN_KEY = "cp_auth_session_token"
_CSRF_TOKEN_KEY = "cp_auth_csrf_token"
_USER_KEY = "cp_authenticated_user"


def _request_context() -> tuple[str | None, str | None]:
    """Return a conservative user-agent and trusted-proxy IP when available."""
    context = getattr(st, "context", None)
    headers: Mapping[str, str] = getattr(context, "headers", {}) or {}
    user_agent = headers.get("User-Agent") or headers.get("user-agent")
    ip_address = None
    if os.getenv("TRUST_PROXY_HEADERS", "false").strip().lower() in {"1", "true", "yes"}:
        forwarded = headers.get("X-Forwarded-For") or headers.get("x-forwarded-for")
        if forwarded:
            ip_address = forwarded.split(",", 1)[0].strip()[:64]
    return user_agent[:512] if user_agent else None, ip_address


def clear_auth_state() -> None:
    for key in (_SESSION_TOKEN_KEY, _CSRF_TOKEN_KEY, _USER_KEY):
        st.session_state.pop(key, None)


def current_user(session: Session) -> AuthenticatedUser | None:
    token = st.session_state.get(_SESSION_TOKEN_KEY)
    user_agent, ip_address = _request_context()
    try:
        user = resolve_session(
            session,
            token,
            user_agent=user_agent,
            ip_address=ip_address,
        )
    except Exception:
        session.rollback()
        clear_auth_state()
        return None
    if user is None:
        clear_auth_state()
        return None
    st.session_state[_USER_KEY] = user
    return user


def render_login(session: Session) -> None:
    st.markdown("## Sign in to ContentPilot")
    st.caption("Authentication is required before any dashboard or publishing feature is loaded.")

    user_count = int(session.scalar(select(func.count(UserAccount.id))) or 0)
    if user_count == 0:
        show_info(
            "No owner account exists. Configure BOOTSTRAP_ADMIN_EMAIL and a secure "
            "bootstrap password secret, then restart the application."
        )
        return

    with st.form("contentpilot_login", clear_on_submit=False):
        email = st.text_input("Email", max_chars=320)
        password = st.text_input("Password", type="password", max_chars=256)
        totp_code = st.text_input(
            "Authentication code",
            max_chars=8,
            help="Required only when MFA is enabled for your account.",
        )
        submitted = st.form_submit_button("Sign in", type="primary", use_container_width=True)

    if not submitted:
        return

    user_agent, ip_address = _request_context()
    try:
        tokens = authenticate_user(
            session,
            email=email,
            password=password,
            totp_code=totp_code,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        st.session_state[_SESSION_TOKEN_KEY] = tokens.session_token
        st.session_state[_CSRF_TOKEN_KEY] = tokens.csrf_token
        st.session_state[_USER_KEY] = tokens.user
        show_success("Signed in successfully.")
        st.rerun()
    except Exception as exc:
        session.rollback()
        show_error_from_dict(handle_exception(exc, context="auth.login"))


def render_logout(session: Session, user: AuthenticatedUser) -> None:
    st.caption(f"Signed in as {user.display_name}")
    st.caption(f"{user.role.replace('_', ' ').title()} • {user.email}")
    if st.button("Sign out", use_container_width=True, key="auth_logout"):
        try:
            logout(session, st.session_state.get(_SESSION_TOKEN_KEY, ""), actor=user)
        finally:
            clear_auth_state()
        st.rerun()
