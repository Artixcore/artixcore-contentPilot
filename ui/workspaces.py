"""Organization and workspace administration interface."""

from __future__ import annotations

import streamlit as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from core.auth import AuthenticatedUser
from core.error_handler import handle_exception
from core.tenant_models import Organization, Workspace
from core.tenancy import (
    WorkspaceContext,
    create_organization,
    create_workspace,
    invite_member,
    remove_member,
    set_membership_role,
)
from core.workspace_admin import (
    list_pending_invitations,
    list_workspace_api_keys,
    list_workspace_members,
    revoke_invitation,
    update_workspace_settings,
)
from core.workspace_api_keys import create_workspace_api_key, revoke_workspace_api_key
from ui.notifications import show_error_from_dict, show_success

_API_SCOPE_OPTIONS = [
    "content:read",
    "content:write",
    "content:approve",
    "content:publish",
    "analytics:read",
    "integrations:read",
    "integrations:write",
    "webhooks:write",
    "workspace:read",
    "workspace:admin",
]
_WORKSPACE_ROLES = ["owner", "admin", "editor", "reviewer", "viewer"]


def _handle(exc: Exception, context: str) -> None:
    show_error_from_dict(handle_exception(exc, context=context))


def render_workspaces(
    session: Session,
    user: AuthenticatedUser,
    context: WorkspaceContext,
) -> None:
    st.header("Organizations and Workspaces")
    st.caption(
        f"Active workspace: {context.organization_name} / {context.workspace_name}. "
        "All content, jobs, credentials, analytics, and integrations are isolated to this workspace."
    )

    settings_tab, members_tab, api_tab, create_tab = st.tabs(
        ["Workspace Settings", "Members", "API Keys", "Create"]
    )

    with settings_tab:
        workspace = session.get(Workspace, context.workspace_id)
        if workspace is None:
            st.error("Workspace is unavailable.")
        elif context.can("workspace:admin"):
            with st.form("workspace_settings_form"):
                name = st.text_input("Workspace name", value=workspace.name, max_chars=255)
                timezone_name = st.text_input(
                    "Timezone", value=workspace.timezone, max_chars=64, help="Use an IANA timezone."
                )
                locale = st.text_input("Locale", value=workspace.locale, max_chars=32)
                language = st.text_input(
                    "Default language", value=workspace.default_language, max_chars=64
                )
                usage_limit = st.number_input(
                    "Monthly usage limit",
                    min_value=0,
                    max_value=100_000_000,
                    value=int(workspace.usage_limit_monthly or 0),
                    step=100,
                    help="Use 0 for no application-level limit.",
                )
                submitted = st.form_submit_button("Save workspace settings", type="primary")
            if submitted:
                try:
                    update_workspace_settings(
                        session,
                        context=context,
                        actor=user,
                        name=name,
                        timezone_name=timezone_name,
                        locale=locale,
                        default_language=language,
                        usage_limit_monthly=int(usage_limit),
                    )
                    show_success("Workspace settings updated.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    _handle(exc, "workspace.settings")
        else:
            st.info("Your workspace role has read-only access to these settings.")
            st.json(
                {
                    "name": workspace.name,
                    "timezone": workspace.timezone,
                    "locale": workspace.locale,
                    "default_language": workspace.default_language,
                    "usage_limit_monthly": workspace.usage_limit_monthly,
                }
            )

    with members_tab:
        try:
            members = list_workspace_members(session, context=context)
        except Exception as exc:
            _handle(exc, "workspace.members.list")
            members = []

        for membership, account in members:
            with st.container(border=True):
                columns = st.columns([3, 2, 2, 1])
                columns[0].markdown(f"**{account.display_name}**\n\n{account.email}")
                columns[1].write(membership.role.replace("_", " ").title())
                columns[2].write(membership.status.title())
                if context.can("members:manage") and membership.user_id != user.id:
                    selected_role = columns[2].selectbox(
                        "Role",
                        _WORKSPACE_ROLES,
                        index=_WORKSPACE_ROLES.index(membership.role),
                        key=f"member_role_{membership.id}",
                        label_visibility="collapsed",
                    )
                    if columns[3].button("Save", key=f"member_save_{membership.id}"):
                        try:
                            set_membership_role(
                                session,
                                context=context,
                                actor=user,
                                membership_id=membership.id,
                                role=selected_role,
                            )
                            show_success("Membership role updated.")
                            st.rerun()
                        except Exception as exc:
                            session.rollback()
                            _handle(exc, "workspace.member.role")
                    if st.button("Suspend member", key=f"member_remove_{membership.id}"):
                        try:
                            remove_member(
                                session,
                                context=context,
                                actor=user,
                                membership_id=membership.id,
                            )
                            show_success("Workspace membership suspended.")
                            st.rerun()
                        except Exception as exc:
                            session.rollback()
                            _handle(exc, "workspace.member.remove")

        if context.can("members:manage"):
            st.subheader("Invite member")
            with st.form("workspace_invite_form"):
                invite_email = st.text_input("Email", max_chars=320)
                invite_role = st.selectbox(
                    "Workspace role", ["admin", "editor", "reviewer", "viewer"]
                )
                send_invite = st.form_submit_button("Create invitation", type="primary")
            if send_invite:
                try:
                    raw_token = invite_member(
                        session,
                        context=context,
                        actor=user,
                        email=invite_email,
                        role=invite_role,
                    )
                    st.session_state["workspace_invitation_token"] = raw_token
                    show_success("Invitation created. Share the token through a secure channel.")
                except Exception as exc:
                    session.rollback()
                    _handle(exc, "workspace.invite")

            invitation_token = st.session_state.pop("workspace_invitation_token", None)
            if invitation_token:
                st.warning("This invitation token is displayed once. Do not send it publicly.")
                st.code(invitation_token, language=None)

            pending = list_pending_invitations(session, context=context)
            if pending:
                st.subheader("Pending invitations")
            for invitation in pending:
                cols = st.columns([3, 2, 2, 1])
                cols[0].write(invitation.email)
                cols[1].write(invitation.role.title())
                cols[2].write(invitation.expires_at)
                if cols[3].button("Revoke", key=f"invite_revoke_{invitation.id}"):
                    try:
                        revoke_invitation(
                            session,
                            context=context,
                            actor=user,
                            invitation_id=invitation.id,
                        )
                        show_success("Invitation revoked.")
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        _handle(exc, "workspace.invite.revoke")

    with api_tab:
        if not context.can("api_keys:manage"):
            st.info("Only workspace owners and administrators can manage API keys.")
        else:
            with st.form("workspace_api_key_form"):
                key_name = st.text_input("Key name", max_chars=100)
                scopes = st.multiselect("Scopes", _API_SCOPE_OPTIONS)
                expires_days = st.number_input(
                    "Expires after days",
                    min_value=0,
                    max_value=3650,
                    value=90,
                    help="Use 0 for no expiration.",
                )
                create_key = st.form_submit_button("Create API key", type="primary")
            if create_key:
                try:
                    result = create_workspace_api_key(
                        session,
                        context=context,
                        actor=user,
                        name=key_name,
                        scopes=scopes,
                        expires_days=int(expires_days) or None,
                    )
                    st.session_state["workspace_api_key_once"] = result.api_key
                    show_success("API key created.")
                except Exception as exc:
                    session.rollback()
                    _handle(exc, "workspace.api_key.create")

            raw_key = st.session_state.pop("workspace_api_key_once", None)
            if raw_key:
                st.warning("Copy this API key now. Only its hash is stored.")
                st.code(raw_key, language=None)

            for api_key in list_workspace_api_keys(session, context=context):
                cols = st.columns([3, 2, 2, 1])
                cols[0].write(api_key.name)
                cols[1].code(api_key.key_prefix, language=None)
                cols[2].write("Active" if api_key.is_active else "Revoked")
                if api_key.is_active and cols[3].button(
                    "Revoke", key=f"api_revoke_{api_key.id}"
                ):
                    try:
                        revoke_workspace_api_key(
                            session,
                            context=context,
                            actor=user,
                            key_id=api_key.id,
                        )
                        show_success("API key revoked.")
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        _handle(exc, "workspace.api_key.revoke")

    with create_tab:
        if user.role in {"owner", "super_admin"}:
            st.subheader("Create organization")
            with st.form("create_organization_form"):
                organization_name = st.text_input("Organization name", max_chars=255)
                organization_slug = st.text_input("Organization slug", max_chars=100)
                first_workspace_name = st.text_input("First workspace name", max_chars=255)
                first_workspace_slug = st.text_input("First workspace slug", max_chars=100)
                create_org = st.form_submit_button("Create organization", type="primary")
            if create_org:
                try:
                    created = create_organization(
                        session,
                        actor=user,
                        name=organization_name,
                        slug=organization_slug,
                        workspace_name=first_workspace_name or organization_name,
                        workspace_slug=first_workspace_slug or organization_slug,
                    )
                    st.session_state["active_workspace_id"] = created.workspace_id
                    show_success("Organization and workspace created.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    _handle(exc, "organization.create")

        st.subheader("Create another workspace")
        organization_query = select(Organization).where(Organization.status == "active")
        if user.role not in {"owner", "super_admin"}:
            organization_query = organization_query.where(Organization.owner_user_id == user.id)
        organizations = session.scalars(organization_query.order_by(Organization.name)).all()
        if not organizations:
            st.info("You do not own an organization where a new workspace can be created.")
        else:
            org_map = {f"{item.name} ({item.slug})": item.id for item in organizations}
            with st.form("create_workspace_form"):
                organization_label = st.selectbox("Organization", list(org_map))
                new_workspace_name = st.text_input("Workspace name", max_chars=255)
                new_workspace_slug = st.text_input("Workspace slug", max_chars=100)
                new_timezone = st.text_input("Timezone", value="Asia/Dhaka", max_chars=64)
                new_locale = st.text_input("Locale", value="en-BD", max_chars=32)
                create_ws = st.form_submit_button("Create workspace", type="primary")
            if create_ws:
                try:
                    created = create_workspace(
                        session,
                        actor=user,
                        organization_id=org_map[organization_label],
                        name=new_workspace_name,
                        slug=new_workspace_slug,
                        timezone_name=new_timezone,
                        locale=new_locale,
                    )
                    st.session_state["active_workspace_id"] = created.workspace_id
                    show_success("Workspace created.")
                    st.rerun()
                except Exception as exc:
                    session.rollback()
                    _handle(exc, "workspace.create")
