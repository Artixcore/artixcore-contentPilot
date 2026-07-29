"""Complete tenant-aware navigation for the expanded ContentPilot platform."""

from __future__ import annotations

from core.auth import AuthenticatedUser
from core.tenancy import WorkspaceContext
from core.workspace_permissions import can_access

NAV_OPTIONS: tuple[tuple[str, str, str], ...] = (
    ("Dashboard", "dashboard", "read"),
    ("Workspaces", "workspaces", "read"),
    ("Notifications", "notifications", "read"),
    ("Campaigns", "campaigns", "create_content"),
    ("Analytics", "analytics", "read"),
    ("Leads", "leads", "read"),
    ("Automations", "automations", "manage_integrations"),
    ("OAuth Integrations", "oauth_integrations", "manage_integrations"),
    ("Brand Brain", "brand_brain", "read"),
    ("AI Workspace", "ai_workspace", "create_content"),
    ("Create Post", "create_post", "create_content"),
    ("Approvals", "approvals", "approve_content"),
    ("Chat Inbox", "chat_inbox", "manage_chatbot"),
    ("Chat Control", "chat_control", "manage_chatbot"),
    ("Publish Center", "publish_center", "publish_content"),
    ("Training Data", "training_data", "edit_content"),
    ("Provider Settings", "provider_settings", "manage_integrations"),
    ("Publishing Settings", "publishing_settings", "manage_integrations"),
    ("Brand Settings", "brand_settings", "manage_brand"),
    ("Exports", "exports", "export_data"),
    ("Operations", "operations", "manage_integrations"),
    ("User Management", "user_management", "manage_users"),
    ("Security", "security", "read"),
)

PAGE_PERMISSIONS = {label: permission for label, _key, permission in NAV_OPTIONS}
PAGE_KEYS = {label: key for label, key, _permission in NAV_OPTIONS}


def available_labels(
    user: AuthenticatedUser,
    workspace: WorkspaceContext,
) -> list[str]:
    return [
        label
        for label, _key, permission in NAV_OPTIONS
        if can_access(user, workspace, permission)
    ]


def permission_for_label(label: str) -> str:
    return PAGE_PERMISSIONS.get(label, "read")
