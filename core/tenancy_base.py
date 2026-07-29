"""Shared SQLAlchemy mixin for workspace-scoped records."""

from __future__ import annotations

from sqlalchemy import ForeignKey, Integer
from sqlalchemy.orm import Mapped, declarative_mixin, mapped_column


@declarative_mixin
class TenantScopedMixin:
    """Mark a model as belonging to exactly one workspace.

    The column stays nullable at the database layer during the compatibility
    migration. Application session hooks fail closed and assign or validate the
    active workspace before any scoped record is written.
    """

    workspace_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
