"""Navigation state helpers with role-aware page permissions."""

from __future__ import annotations

from typing import TYPE_CHECKING

import streamlit as st

if TYPE_CHECKING:
    from core.auth import AuthenticatedUser

NAV_OPTIONS: list[tuple[str, str]] = [
    ("Dashboard", "dashboard"),
    ("Workspaces", "workspaces"),
    ("Notifications", "notifications"),
    ("AI Workspace", "ai_workspace"),
    ("Create Post", "create_post"),
    ("Approvals", "approvals"),
    ("Chat Inbox", "chat_inbox"),
    ("Chat Control", "chat_control"),
    ("Publish Center", "publish_center"),
    ("Training Data", "training_data"),
    ("Provider Settings", "provider_settings"),
    ("Publishing Settings", "publishing_settings"),
    ("Brand Settings", "brand_settings"),
    ("Exports", "exports"),
    ("Operations", "operations"),
    ("User Management", "user_management"),
    ("Security", "security"),
]

NAV_LABELS = [label for label, _ in NAV_OPTIONS]
NAV_KEYS = [key for _, key in NAV_OPTIONS]

PAGE_PERMISSIONS: dict[str, str] = {
    "Dashboard": "read",
    "Workspaces": "read",
    "Notifications": "read",
    "AI Workspace": "create_content",
    "Create Post": "create_content",
    "Approvals": "approve_content",
    "Chat Inbox": "manage_chatbot",
    "Chat Control": "manage_chatbot",
    "Publish Center": "publish_content",
    "Training Data": "edit_content",
    "Provider Settings": "manage_integrations",
    "Publishing Settings": "manage_integrations",
    "Brand Settings": "manage_brand",
    "Exports": "export_data",
    "Operations": "manage_integrations",
    "User Management": "manage_users",
    "Security": "read",
}

PAGE_LABELS: dict[str, str] = {key: label for label, key in NAV_OPTIONS}

PAGE_SUBTITLES: dict[str, str] = {
    "dashboard": "Overview of the active workspace content pipeline, publishing, and system health.",
    "workspaces": "Manage organizations, workspace settings, members, invitations, and API keys.",
    "notifications": "Review operational alerts, failed jobs, and integration notices.",
    "ai_workspace": "Ask ContentPilot to create, reply, plan, or publish.",
    "create_post": "Generate content for a selected platform.",
    "approvals": "Review, edit, approve, or reject pending content.",
    "chat_inbox": "Review conversations, approve replies, and simulate incoming messages.",
    "chat_control": "Configure and monitor the workspace chatbot.",
    "publish_center": "Publish approved or scheduled posts with confirmation.",
    "training_data": "Manage training examples and brand-learning data.",
    "provider_settings": "Provider status and configuration.",
    "publishing_settings": "Social platform connector status and encrypted credentials.",
    "brand_settings": "Configure the workspace brand profile used for content generation.",
    "exports": "Download workspace posts, training data, and activity logs.",
    "operations": "Monitor workspace jobs, integrations, webhook receipts, and failures.",
    "user_management": "Manage global accounts, roles, and access status.",
    "security": "Manage password, MFA, encrypted credentials, sessions, and audit logs.",
}


def init_navigation() -> None:
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []


def available_labels(user: "AuthenticatedUser") -> list[str]:
    return [
        label
        for label, _ in NAV_OPTIONS
        if user.can(PAGE_PERMISSIONS.get(label, "read"))
    ]


def permission_for_label(label: str) -> str:
    return PAGE_PERMISSIONS.get(label, "read")


def navigate(page_label: str) -> None:
    """Switch to a page by display label."""
    st.session_state["page"] = page_label
    st.session_state["page_radio"] = page_label
    st.rerun()


def label_for_key(page_key: str) -> str:
    return PAGE_LABELS.get(page_key, "Dashboard")


def key_for_label(label: str) -> str:
    for nav_label, key in NAV_OPTIONS:
        if nav_label == label:
            return key
    return "dashboard"
