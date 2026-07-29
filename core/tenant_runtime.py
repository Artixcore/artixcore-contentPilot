"""Runtime helpers for safely binding SQLAlchemy sessions to workspaces."""

from __future__ import annotations

from sqlalchemy.orm import Session

from core.errors import AppError


class WorkspaceSessionError(AppError):
    default_error_code = "WORKSPACE_SESSION_ERROR"
    default_user_action = "Reload the page and select the workspace again."
    retryable_default = False


def bind_workspace(
    session: Session,
    workspace: object | int | None,
    *,
    tenant_bypass: bool = False,
) -> Session:
    """Bind a session to a workspace and clear cached identities on change.

    A session cannot change workspaces while it has pending writes. Clearing the
    identity map prevents a model loaded in one workspace from being returned
    from SQLAlchemy's cache after switching to another workspace.
    """
    workspace_id = None
    if workspace is not None:
        workspace_id = int(getattr(workspace, "workspace_id", workspace))
        if workspace_id <= 0:
            raise WorkspaceSessionError("Workspace ID must be a positive integer.")

    previous = session.info.get("workspace_id")
    changing = previous is not None and workspace_id != int(previous)
    if changing:
        if session.new or session.dirty or session.deleted:
            raise WorkspaceSessionError(
                "Cannot switch workspaces while database changes are pending."
            )
        session.expunge_all()

    session.info["tenant_bypass"] = bool(tenant_bypass)
    if workspace_id is None:
        session.info.pop("workspace_id", None)
        session.info.pop("workspace_context", None)
    else:
        session.info["workspace_id"] = workspace_id
        if hasattr(workspace, "workspace_id"):
            session.info["workspace_context"] = workspace
        else:
            session.info.pop("workspace_context", None)
    return session
