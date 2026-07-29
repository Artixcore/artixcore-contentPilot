"""Database initialization, secure engine configuration, and tenant-aware sessions."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, event, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from core.errors import DatabaseError
from core.logging_config import get_logger
from core.migrations import run_migrations
from core.models import DEFAULT_BRAND, Base, BrandProfile
from core.retries import retry_on_sqlite_locked

# Importing registers security, tenancy, and operations tables on Base.metadata.
import core.operations_models  # noqa: F401,E402
import core.security_models  # noqa: F401,E402
import core.tenant_models  # noqa: F401,E402
from core.tenant_runtime import bind_workspace as set_session_workspace
from core.tenancy import WorkspaceContext, install_tenant_session_hooks

load_dotenv()
install_tenant_session_hooks()

logger = get_logger(__name__)

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///data/contentpilot.db")
DATABASE_TIMEOUT_SECONDS = int(os.getenv("DATABASE_TIMEOUT_SECONDS", "30"))

_engine = None
_SessionLocal = None


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return min(max(value, minimum), maximum)


def _ensure_data_dir() -> None:
    if DATABASE_URL.startswith("sqlite:///"):
        db_path = DATABASE_URL.replace("sqlite:///", "")
        if db_path != ":memory:":
            path = Path(db_path)
            path.parent.mkdir(parents=True, exist_ok=True)


def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA secure_delete=ON")
    finally:
        cursor.close()


def get_engine():
    global _engine
    if _engine is None:
        _ensure_data_dir()
        is_sqlite = DATABASE_URL.startswith("sqlite")
        connect_args: dict = {}
        engine_kwargs: dict = {
            "pool_pre_ping": True,
            "hide_parameters": True,
        }
        if is_sqlite:
            connect_args["check_same_thread"] = False
            connect_args["timeout"] = DATABASE_TIMEOUT_SECONDS
        else:
            connect_args["connect_timeout"] = DATABASE_TIMEOUT_SECONDS
            connect_args["application_name"] = "artixcore-contentpilot"
            engine_kwargs.update(
                {
                    "pool_size": _bounded_int("DATABASE_POOL_SIZE", 5, 1, 50),
                    "max_overflow": _bounded_int("DATABASE_MAX_OVERFLOW", 10, 0, 100),
                    "pool_recycle": _bounded_int(
                        "DATABASE_POOL_RECYCLE_SECONDS", 1_800, 60, 86_400
                    ),
                }
            )
        _engine = create_engine(
            DATABASE_URL,
            connect_args=connect_args,
            **engine_kwargs,
        )
        if is_sqlite:
            event.listen(_engine, "connect", _configure_sqlite_connection)
    return _engine


def get_session_factory():
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(
            bind=get_engine(),
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )
    return _SessionLocal


def get_session(
    workspace: WorkspaceContext | int | None = None,
    *,
    tenant_bypass: bool = False,
) -> Session:
    session = get_session_factory()()
    return set_session_workspace(session, workspace, tenant_bypass=tenant_bypass)


@contextmanager
def session_scope(
    workspace: WorkspaceContext | int | None = None,
    *,
    tenant_bypass: bool = False,
):
    session = get_session(workspace, tenant_bypass=tenant_bypass)
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@retry_on_sqlite_locked()
def init_db() -> None:
    """Initialize database with safe migrations."""
    try:
        engine = get_engine()
        run_migrations(engine)
        logger.info("Database initialized successfully")
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error("Database initialization failed: %s", type(exc).__name__)
        raise DatabaseError(
            "Database is currently unavailable.",
            reason=str(exc),
            user_action="Check database connectivity, credentials, permissions, and TLS settings.",
            original_exception=exc,
        ) from exc


def reset_engine(database_url: str | None = None) -> None:
    """Reset engine, primarily for isolated tests."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None
    if database_url is not None:
        import core.database as db_module

        db_module.DATABASE_URL = database_url


def check_database_health() -> dict:
    """Check database reachability and required schema without disclosing credentials."""
    try:
        engine = get_engine()
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        required = set(Base.metadata.tables.keys())
        missing = required - tables
        if missing:
            return {
                "healthy": False,
                "message": f"Missing tables: {', '.join(sorted(missing))}",
            }
        return {"healthy": True, "message": "Database is reachable and schema is valid."}
    except OperationalError as exc:
        return {"healthy": False, "message": f"Database unavailable: {type(exc).__name__}"}
    except Exception as exc:
        return {"healthy": False, "message": f"Database check failed: {type(exc).__name__}"}


@retry_on_sqlite_locked()
def seed_default_brand_profile(
    session: Session | None = None,
    *,
    workspace: WorkspaceContext | int | None = None,
) -> BrandProfile | None:
    own_session = session is None
    if own_session:
        session = get_session(workspace)
    elif workspace is not None:
        set_session_workspace(session, workspace)
    try:
        existing = session.execute(select(BrandProfile)).scalars().first()
        if existing:
            return existing
        profile = BrandProfile(**DEFAULT_BRAND)
        session.add(profile)
        if own_session:
            session.commit()
            session.refresh(profile)
        else:
            session.flush()
        return profile
    finally:
        if own_session:
            session.close()


def get_brand_profile(session: Session) -> BrandProfile | None:
    return session.execute(select(BrandProfile)).scalars().first()
