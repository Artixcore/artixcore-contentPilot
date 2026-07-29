"""Artixcore ContentPilot — Streamlit application entry point."""

import logging

import streamlit as st
from dotenv import load_dotenv

from core.chat_database import seed_default_chatbot_settings
from core.database import get_session, init_db, seed_default_brand_profile
from core.error_handler import handle_exception
from ui.ai_workspace import render_ai_workspace
from ui.approvals import render_approvals
from ui.brand_settings import render_brand_settings
from ui.chat_control import render_chat_control
from ui.chat_inbox import render_chat_inbox
from ui.create_post import render_create_post
from ui.dashboard import render_dashboard
from ui.exports import render_exports
from ui.layout import render_sidebar, render_topbar
from ui.navigation import init_navigation
from ui.provider_settings import render_provider_settings
from ui.publish_center import render_publish_center
from ui.publishing_settings import render_publishing_settings
from ui.theme import init_theme
from ui.training_data import render_training_data

load_dotenv()

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


@st.cache_resource
def bootstrap_database():
    init_db()
    session = get_session()
    try:
        seed_default_brand_profile(session)
        seed_default_chatbot_settings(session)
        session.commit()
    finally:
        session.close()


@st.cache_resource
def start_telegram_controller():
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


def main() -> None:
    st.set_page_config(
        page_title="Artixcore ContentPilot",
        page_icon="🟠",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    init_theme()
    init_navigation()

    bootstrap_database()
    start_telegram_controller()

    page = render_sidebar()
    render_topbar()

    session = get_session()
    try:
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
        else:
            render_dashboard(session)
    except Exception as exc:
        session.rollback()
        _render_error(exc)
    finally:
        session.close()


if __name__ == "__main__":
    main()
