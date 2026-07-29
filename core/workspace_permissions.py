"""Combined global-account and workspace-membership authorization."""

from __future__ import annotations

from core.auth import AuthenticatedUser, require_permission
from core.tenancy import WorkspaceContext, require_workspace_permission

GLOBAL_TO_WORKSPACE_PERMISSION: dict[str, str | None] = {
    "read": "workspace:read",
    "create_content": "content:write",
    "edit_content": "content:write",
    "approve_content": "content:approve",
    "publish_content": "content:publish",
    "manage_brand": "workspace:admin",
    "manage_chatbot": "integrations:write",
    "manage_integrations": "integrations:write",
    "view_audit": "workspace:admin",
    "export_data": "content:read",
    "manage_security": "workspace:admin",
    # Global account administration is intentionally not delegated to a workspace role.
    "manage_users": None,
}


def can_access(
    user: AuthenticatedUser,
    workspace: WorkspaceContext,
    global_permission: str,
) -> bool:
    if not user.can(global_permission):
        return False
    workspace_permission = GLOBAL_TO_WORKSPACE_PERMISSION.get(global_permission, "workspace:read")
    return workspace_permission is None or workspace.can(workspace_permission)


def require_combined_permission(
    user: AuthenticatedUser,
    workspace: WorkspaceContext,
    global_permission: str,
) -> None:
    require_permission(user, global_permission)
    workspace_permission = GLOBAL_TO_WORKSPACE_PERMISSION.get(global_permission, "workspace:read")
    if workspace_permission is not None:
        require_workspace_permission(workspace, workspace_permission)
