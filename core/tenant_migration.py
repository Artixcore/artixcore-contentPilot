"""Compatibility backfill and integrity checks for workspace isolation."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from core.errors import DatabaseError
from core.migrations import TENANT_SCOPED_TABLES


def _safe_table_name(table_name: str) -> str:
    if not table_name.replace("_", "").isalnum():
        raise DatabaseError("Unsafe tenant migration identifier detected.")
    return table_name


def backfill_legacy_workspace(session: Session, workspace_id: int) -> dict[str, int]:
    """Assign legacy unscoped records to the initial workspace.

    This operation is intentionally limited to records whose workspace_id is
    NULL. It never moves data that is already assigned to another workspace.
    """
    if int(workspace_id) <= 0:
        raise DatabaseError("A valid workspace ID is required for tenant backfill.")
    session.info["tenant_bypass"] = True
    inspector = inspect(session.get_bind())
    existing_tables = set(inspector.get_table_names())
    updated: dict[str, int] = {}
    try:
        for raw_name in TENANT_SCOPED_TABLES:
            table_name = _safe_table_name(raw_name)
            if table_name not in existing_tables:
                continue
            columns = {column["name"] for column in inspector.get_columns(table_name)}
            if "workspace_id" not in columns:
                raise DatabaseError(
                    f"Tenant migration is incomplete for table: {table_name}",
                    user_action="Run database migrations before starting ContentPilot.",
                )
            result = session.execute(
                text(f"UPDATE {table_name} SET workspace_id = :workspace_id WHERE workspace_id IS NULL"),
                {"workspace_id": int(workspace_id)},
            )
            updated[table_name] = int(result.rowcount or 0)
        session.commit()
        verify_tenant_integrity(session)
        return updated
    except Exception:
        session.rollback()
        raise
    finally:
        session.info["tenant_bypass"] = False


def verify_tenant_integrity(session: Session) -> None:
    """Fail when scoped rows are unassigned or child records cross workspaces."""
    session.info["tenant_bypass"] = True
    inspector = inspect(session.get_bind())
    existing_tables = set(inspector.get_table_names())
    problems: list[str] = []
    try:
        for raw_name in TENANT_SCOPED_TABLES:
            table_name = _safe_table_name(raw_name)
            if table_name not in existing_tables:
                continue
            columns = {column["name"] for column in inspector.get_columns(table_name)}
            if "workspace_id" not in columns:
                problems.append(f"{table_name}:missing_workspace_column")
                continue
            unassigned = session.scalar(
                text(f"SELECT COUNT(*) FROM {table_name} WHERE workspace_id IS NULL")
            )
            if int(unassigned or 0) > 0:
                problems.append(f"{table_name}:unassigned={int(unassigned or 0)}")

        relationship_checks = (
            (
                "publishing_logs",
                "posts",
                "post_id",
            ),
            (
                "training_examples",
                "posts",
                "post_id",
            ),
            (
                "content_events",
                "posts",
                "post_id",
            ),
            (
                "post_analytics",
                "posts",
                "post_id",
            ),
            (
                "chat_messages",
                "chat_conversations",
                "conversation_id",
            ),
            (
                "chat_events",
                "chat_conversations",
                "conversation_id",
            ),
            (
                "chat_training_examples",
                "chat_conversations",
                "conversation_id",
            ),
        )
        for child, parent, foreign_key in relationship_checks:
            if child not in existing_tables or parent not in existing_tables:
                continue
            child_columns = {column["name"] for column in inspector.get_columns(child)}
            parent_columns = {column["name"] for column in inspector.get_columns(parent)}
            if "workspace_id" not in child_columns or "workspace_id" not in parent_columns:
                continue
            mismatch = session.scalar(
                text(
                    f"SELECT COUNT(*) FROM {child} c JOIN {parent} p ON p.id = c.{foreign_key} "
                    "WHERE c.workspace_id <> p.workspace_id"
                )
            )
            if int(mismatch or 0) > 0:
                problems.append(f"{child}:cross_workspace={int(mismatch or 0)}")

        if problems:
            raise DatabaseError(
                "Workspace isolation integrity check failed.",
                reason=", ".join(problems),
                user_action="Restore from backup or correct the tenant migration before serving traffic.",
            )
    finally:
        session.info["tenant_bypass"] = False
