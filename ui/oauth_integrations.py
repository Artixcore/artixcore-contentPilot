"""OAuth connection management and callback processing interface."""

from __future__ import annotations

import os

import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.auth import AuthenticatedUser
from core.error_handler import handle_exception
from core.oauth_service import (
    begin_authorization,
    complete_authorization,
    configured_provider_status,
    get_provider_config,
    refresh_connection_token,
    revoke_pending_authorizations,
)
from core.operations_models import IntegrationConnection
from core.tenancy import WorkspaceContext
from ui.notifications import show_error_from_dict, show_success

_PROVIDERS = ["linkedin", "x", "meta"]


def _handle(exc: Exception, context: str) -> None:
    show_error_from_dict(handle_exception(exc, context=context))


def process_oauth_callback(
    session: Session,
    user: AuthenticatedUser,
    workspace: WorkspaceContext,
) -> None:
    """Consume OAuth callback query values exactly once after tenant resolution."""
    query = st.query_params
    state = query.get("state")
    code = query.get("code")
    error = query.get("error")
    if not state and not code and not error:
        return
    try:
        if error:
            raise ValueError("The OAuth provider rejected or cancelled the authorization request.")
        if not state or not code:
            raise ValueError("OAuth callback is missing the required state or authorization code.")
        connection = complete_authorization(
            session,
            context=workspace,
            actor=user,
            raw_state=str(state),
            authorization_code=str(code),
        )
        st.session_state["oauth_flash_success"] = (
            f"Connected {connection.display_name} through {connection.platform.title()}."
        )
    except Exception as exc:
        session.rollback()
        st.session_state["oauth_flash_error"] = handle_exception(
            exc, context="oauth.callback"
        )
    finally:
        st.query_params.clear()
        st.rerun()


def render_oauth_integrations(
    session: Session,
    user: AuthenticatedUser,
    workspace: WorkspaceContext,
) -> None:
    st.header("OAuth Integrations")
    st.caption(
        "Authorization uses PKCE, hashed one-time state, encrypted tokens, exact redirect URIs, and provider host allowlists."
    )

    success = st.session_state.pop("oauth_flash_success", None)
    if success:
        show_success(success)
    error = st.session_state.pop("oauth_flash_error", None)
    if error:
        show_error_from_dict(error)

    status = configured_provider_status()
    status_columns = st.columns(len(_PROVIDERS))
    for index, provider in enumerate(_PROVIDERS):
        status_columns[index].metric(
            provider.title(), "Configured" if status.get(provider) else "Not configured"
        )

    connect_tab, connections_tab, security_tab = st.tabs(
        ["Connect Account", "Connections", "Security"]
    )

    with connect_tab:
        if not workspace.can("integrations:write"):
            st.info("Your workspace role cannot create integrations.")
        else:
            provider = st.selectbox("Provider", _PROVIDERS, key="oauth_provider")
            prefix = f"OAUTH_{provider.upper()}"
            redirect_default = os.getenv(f"{prefix}_REDIRECT_URI", "")
            scopes_default = os.getenv(f"{prefix}_SCOPES", "")
            with st.form("oauth_begin_form"):
                account_key = st.text_input(
                    "Account key",
                    max_chars=100,
                    help="Stable internal identifier such as artixcore-company.",
                )
                display_name = st.text_input("Display name", max_chars=255)
                redirect_uri = st.text_input(
                    "Exact redirect URI",
                    value=redirect_default,
                    max_chars=1024,
                    help="This must exactly match the URI registered with the provider.",
                )
                scopes_text = st.text_input(
                    "Scopes",
                    value=scopes_default,
                    max_chars=5_000,
                    help="Space or comma separated provider scopes.",
                )
                begin = st.form_submit_button("Create authorization link", type="primary")
            if begin:
                try:
                    scopes = [
                        value.strip()
                        for value in scopes_text.replace(",", " ").split()
                        if value.strip()
                    ]
                    result = begin_authorization(
                        session,
                        context=workspace,
                        actor=user,
                        provider=provider,
                        redirect_uri=redirect_uri,
                        account_key=account_key,
                        display_name=display_name,
                        scopes=scopes,
                    )
                    st.session_state["oauth_authorization_url"] = result.authorization_url
                    st.session_state["oauth_authorization_expiry"] = result.expires_at.isoformat()
                    show_success("Authorization link created.")
                except Exception as exc:
                    session.rollback()
                    _handle(exc, "oauth.begin")

            authorization_url = st.session_state.get("oauth_authorization_url")
            if authorization_url:
                st.warning(
                    "The authorization link contains a one-time state value. Open it only in your own browser and do not share it."
                )
                st.link_button("Open provider authorization", authorization_url, type="primary")
                st.caption(
                    f"Expires at: {st.session_state.get('oauth_authorization_expiry', 'unknown')}"
                )

    with connections_tab:
        connections = list(
            session.scalars(
                select(IntegrationConnection).order_by(
                    IntegrationConnection.platform.asc(),
                    IntegrationConnection.display_name.asc(),
                )
            ).all()
        )
        if not connections:
            st.info("No integration connections are configured in this workspace.")
        for connection in connections:
            with st.container(border=True):
                columns = st.columns([3, 2, 2, 1])
                columns[0].markdown(f"**{connection.display_name}**")
                columns[0].caption(
                    f"{connection.platform.title()} · {connection.account_key}"
                )
                columns[1].write(connection.status.title())
                columns[2].write(connection.token_expires_at or "No expiry reported")
                if (
                    workspace.can("integrations:write")
                    and connection.refresh_credential_name
                    and columns[3].button("Refresh", key=f"oauth_refresh_{connection.id}")
                ):
                    try:
                        refresh_connection_token(
                            session,
                            context=workspace,
                            actor=user,
                            connection_id=connection.id,
                        )
                        show_success("OAuth access token refreshed.")
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        _handle(exc, "oauth.refresh")

    with security_tab:
        st.markdown(
            "Provider authorization and token endpoints are supplied through deployment configuration. "
            "ContentPilot refuses unallowlisted hosts, private DNS results, redirects, oversized responses, "
            "replayed state values, mismatched users, expired state, and unencrypted token storage."
        )
        if workspace.can("integrations:write"):
            provider_filter = st.selectbox(
                "Pending-state provider", ["all", *_PROVIDERS], key="oauth_revoke_provider"
            )
            if st.button("Revoke pending authorization states"):
                try:
                    count = revoke_pending_authorizations(
                        session,
                        context=workspace,
                        actor=user,
                        provider=None if provider_filter == "all" else provider_filter,
                    )
                    show_success(f"Revoked {count} pending authorization state(s).")
                    st.session_state.pop("oauth_authorization_url", None)
                    st.session_state.pop("oauth_authorization_expiry", None)
                except Exception as exc:
                    session.rollback()
                    _handle(exc, "oauth.revoke_pending")

        with st.expander("Configuration check"):
            for provider in _PROVIDERS:
                try:
                    config = get_provider_config(provider)
                    st.success(
                        f"{provider.title()}: endpoints and host allowlist validated. "
                        f"Token auth method: {config.token_auth_method}."
                    )
                except Exception as exc:
                    st.warning(f"{provider.title()}: {type(exc).__name__}")
