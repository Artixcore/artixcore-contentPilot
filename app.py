"""Artixcore ContentPilot authenticated Streamlit application entry point."""

# ruff: noqa: E402

import streamlit as st
from dotenv import load_dotenv

# Load deployment secrets before importing modules that read configuration at import time.
load_dotenv()

from core.auth import bootstrap_owner, require_permission
from core.chat_database import seed_default_chatbot_settings
from core.config_validation import validate_startup_configuration
from core.database import get_session, init_db, seed_default_brand_profile
from core.error_handler import handle_exception
from core.logging_config import setup_logging
from ui.ai_workspace import render_ai_workspace
from ui.approvals import render_approvals
from ui.authentication import current_user, render_login
from ui.brand_settings import render_brand_settings
from ui.chat_control import render_chat_control
from ui.chat_inbox import render_chat_inbox
from ui.create_post import render_create_post
from ui.dashboard import render_dashboard
from ui.exports import render_exports
from ui.layout import render_sidebar, render_topbar
from ui.navigation import init_navigation, permission_for_label
from ui.provider_settings import render_provider_settings
from ui.publish_center import render_publish_center
from ui.publishing_settings import render_publishing_settings
from ui.security_settings import render_security_settings
from ui.theme import init_theme
from ui.training_data import render_training_data
from ui.user_management import render_user_management

setup_logging()


@st.cache_resource
def bootstrap_database() -> bool:
    init_db()
    session = get_session()
    try:
        seed_default_brand_profile(session)
        seed_default_chatbot_settings(session)
        session.commit()
        bootstrap_owner(session)
        return True
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@st.cache_resource
def start_telegram_controller() -> bool:
    from chatbot.telegram_controller import start_telegram_polling

    start_telegram_polling()
    return True


def _render_error(exc: BaseException) -> None:
    """Render a sanitized error alert without leaking internal exception details."""
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
        start_telegram_controller()
        return True
    except Exception as exc:
        _render_error(exc)
        st.warning(
            "ContentPilot stopped before serving the dashboard because startup checks failed."
        )
        return False


def _render_page(session, page: str, user) -> None:
    require_permission(user, permission_for_label(page))

    if page == "Dashboard":
        render_dashboard(session)
    elif page == "AI Workspace":
        render_ai_workspace(session)
    elif page == "Create Post":
        render_create_post(session)
    elif page == "Approvals":
        render_approvals(session)
    elif page == "Chat Inbox":
        render_chat_inbox(session)
    elif page == "Chat Control":
        render_chat_control(session)
    elif page == "Publish Center":
        render_publish_center(session)
    elif page == "Training Data":
        render_training_data(session)
    elif page == "Provider Settings":
        render_provider_settings(session)
    elif page == "Publishing Settings":
        render_publishing_settings(session)
    elif page == "Brand Settings":
        render_brand_settings(session)
    elif page == "Exports":
        render_exports(session)
    elif page == "User Management":
        render_user_management(session, user)
    elif page == "Security":
        render_security_settings(session, user)
    else:
        require_permission(user, "read")
        render_dashboard(session)


def _authenticate_request():
    auth_session = get_session()
    try:
        user = current_user(auth_session)
        if user is None:
            render_login(auth_session)
        return user
    except Exception as exc:
        auth_session.rollback()
        _render_error(exc)
        return None
    finally:
        auth_session.close()


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

    session = get_session()
    try:
        page = render_sidebar(session, user)
        render_topbar(user)
        _render_page(session, page, user)
    except Exception as exc:
        session.rollback()
        _render_error(exc)
    finally:
        session.close()


if __name__ == "__main__":
    main()
