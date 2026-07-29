"""Complete authenticated, tenant-isolated Artixcore ContentPilot entrypoint."""

from __future__ import annotations

# ruff: noqa: E402

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from core.auth import bootstrap_owner
from core.chat_database import seed_default_chatbot_settings
from core.config_validation import validate_startup_configuration
from core.database import get_session, init_db, seed_default_brand_profile, set_session_workspace
from core.error_handler import handle_exception
from core.logging_config import setup_logging
from core.tenant_migration import backfill_legacy_workspace
from core.tenancy import (
    WorkspaceContext,
    bootstrap_default_tenant,
    list_accessible_workspaces,
    resolve_workspace,
)
from core.workspace_permissions import require_combined_permission
from ui.ai_workspace import render_ai_workspace
from ui.analytics import render_analytics
from ui.approvals import render_approvals
from ui.authentication import current_user, render_login
from ui.automations import render_automations
from ui.brand_brain import render_brand_brain
from ui.brand_settings import render_brand_settings
from ui.campaigns import render_campaigns
from ui.chat_control import render_chat_control
from ui.chat_inbox import render_chat_inbox
from ui.create_post import render_create_post
from ui.dashboard import render_dashboard
from ui.exports import render_exports
from ui.layout_complete import render_sidebar, render_topbar
from ui.leads import render_leads
from ui.navigation import init_navigation
from ui.navigation_complete import permission_for_label
from ui.notification_center import render_notification_center
from ui.oauth_integrations import process_oauth_callback, render_oauth_integrations
from ui.operations import render_operations
from ui.provider_settings import render_provider_settings
from ui.publish_center import render_publish_center
from ui.publishing_settings import render_publishing_settings
from ui.security_settings import render_security_settings
from ui.theme import init_theme
from ui.training_data import render_training_data
from ui.user_management import render_user_management
from ui.workspaces import render_workspaces

setup_logging()


@st.cache_resource
def bootstrap_database() -> bool:
    """Initialize schema, owner, tenant, legacy backfill, and scoped defaults."""
    init_db()
    session = get_session(tenant_bypass=True)
    try:
        owner = bootstrap_owner(session)
        if owner is None:
            return True
        workspace = bootstrap_default_tenant(session, owner)
        backfill_legacy_workspace(session, workspace.workspace_id)
        set_session_workspace(session, workspace)
        seed_default_brand_profile(session, workspace=workspace)
        seed_default_chatbot_settings(session)
        session.commit()
        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _render_error(exc: BaseException) -> None:
    error = handle_exception(exc, context="streamlit_page")
    st.error(error["message"])
    st.caption(f"Error code: {error['error_code']}")
    if error.get("user_action"):
        st.info(error["user_action"])
    if error.get("retryable"):
        st.warning("This failure may be temporary. Please retry the action.")


def _bootstrap_application() -> bool:
    try:
        validate_startup_configuration()
        bootstrap_database()
        return True
    except Exception as exc:
        _render_error(exc)
        st.warning(
            "ContentPilot stopped before serving the application because startup checks failed."
        )
        return False


def _authenticate_request():
    session = get_session(tenant_bypass=True)
    try:
        user = current_user(session)
        if user is None:
            render_login(session)
        return user
    except Exception as exc:
        session.rollback()
        _render_error(exc)
        return None
    finally:
        session.close()


def _resolve_workspace_request(user) -> tuple[WorkspaceContext, list[WorkspaceContext]] | None:
    session = get_session(tenant_bypass=True)
    try:
        requested = st.session_state.get("active_workspace_id")
        workspace = resolve_workspace(session, user, requested)
        available = list_accessible_workspaces(session, user)
        if not any(item.workspace_id == workspace.workspace_id for item in available):
            raise RuntimeError("Resolved workspace is not present in the user's membership list.")
        st.session_state["active_workspace_id"] = workspace.workspace_id
        return workspace, available
    except Exception as exc:
        session.rollback()
        _render_error(exc)
        return None
    finally:
        session.close()


def _render_page(
    session,
    page: str,
    user,
    workspace: WorkspaceContext,
) -> None:
    require_combined_permission(user, workspace, permission_for_label(page))

    routes = {
        "Dashboard": lambda: render_dashboard(session),
        "Workspaces": lambda: render_workspaces(session, user, workspace),
        "Notifications": lambda: render_notification_center(session, user),
        "Campaigns": lambda: render_campaigns(session, user, workspace),
        "Analytics": lambda: render_analytics(session, workspace),
        "Leads": lambda: render_leads(session, user, workspace),
        "Automations": lambda: render_automations(session, user, workspace),
        "OAuth Integrations": lambda: render_oauth_integrations(session, user, workspace),
        "Brand Brain": lambda: render_brand_brain(session, user, workspace),
        "AI Workspace": lambda: render_ai_workspace(session),
        "Create Post": lambda: render_create_post(session),
        "Approvals": lambda: render_approvals(session),
        "Chat Inbox": lambda: render_chat_inbox(session),
        "Chat Control": lambda: render_chat_control(session),
        "Publish Center": lambda: render_publish_center(session),
        "Training Data": lambda: render_training_data(session),
        "Provider Settings": lambda: render_provider_settings(session),
        "Publishing Settings": lambda: render_publishing_settings(session),
        "Brand Settings": lambda: render_brand_settings(session),
        "Exports": lambda: render_exports(session),
        "Operations": lambda: render_operations(session, user),
        "User Management": lambda: render_user_management(session, user),
        "Security": lambda: render_security_settings(session, user),
    }
    renderer = routes.get(page)
    if renderer is None:
        require_combined_permission(user, workspace, "read")
        render_dashboard(session)
        return
    renderer()


def main() -> None:
    st.set_page_config(
        page_title="Artixcore ContentPilot",
        page_icon="🟠",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    init_theme()
    init_navigation()

    if not _bootstrap_application():
        st.stop()
    user = _authenticate_request()
    if user is None:
        st.stop()
    resolved = _resolve_workspace_request(user)
    if resolved is None:
        st.stop()
    workspace, available_workspaces = resolved

    session = get_session(workspace)
    try:
        process_oauth_callback(session, user, workspace)
        page = render_sidebar(session, user, workspace, available_workspaces)
        render_topbar(user, workspace)
        _render_page(session, page, user, workspace)
    except Exception as exc:
        session.rollback()
        _render_error(exc)
    finally:
        session.close()


if __name__ == "__main__":
    main()
