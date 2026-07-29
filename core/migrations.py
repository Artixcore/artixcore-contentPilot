"""Safe startup migrations that add missing tables and columns without deleting data."""

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from core.errors import DatabaseError
from core.models import Base

logger = logging.getLogger(__name__)

# (table_name, column_name, portable_column_definition)
COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("posts", "external_post_id", "VARCHAR(255)"),
    ("posts", "external_post_url", "VARCHAR(1024)"),
    ("posts", "published_by_platform", "VARCHAR(50)"),
    ("posts", "publish_error", "TEXT"),
    ("posts", "publish_raw_response", "TEXT"),
    ("posts", "input_prompt", "TEXT"),
    ("posts", "system_prompt", "TEXT"),
    ("posts", "raw_ai_response", "TEXT"),
    ("posts", "parsed_ai_response", "TEXT"),
    ("posts", "generation_temperature", "FLOAT"),
    ("posts", "generation_max_tokens", "INTEGER"),
    ("posts", "provider_latency_ms", "INTEGER"),
    ("posts", "provider_error", "TEXT"),
    ("posts", "token_input_estimate", "INTEGER"),
    ("posts", "token_output_estimate", "INTEGER"),
    ("posts", "cost_estimate", "FLOAT"),
    ("posts", "revision_count", "INTEGER DEFAULT 0"),
    ("posts", "parent_post_id", "INTEGER"),
    ("posts", "approved_by", "VARCHAR(255)"),
    ("posts", "approved_at", "DATETIME"),
    ("posts", "rejected_reason", "TEXT"),
    ("posts", "manual_feedback", "TEXT"),
    ("posts", "training_score", "INTEGER"),
    ("provider_logs", "request_payload_sanitized", "TEXT"),
    ("provider_logs", "response_payload_sanitized", "TEXT"),
]

TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "brand_profiles",
    "posts",
    "campaigns",
    "provider_logs",
    "publishing_logs",
    "training_examples",
    "content_events",
    "social_accounts",
    "post_analytics",
    "chatbot_settings",
    "chat_conversations",
    "chat_messages",
    "chat_events",
    "chat_training_examples",
    "telegram_admins",
    "telegram_commands",
    "encrypted_credentials",
    "background_jobs",
    "system_notifications",
    "integration_connections",
    "webhook_receipts",
)

for _table_name in TENANT_SCOPED_TABLES:
    COLUMN_MIGRATIONS.append((_table_name, "workspace_id", "INTEGER"))
COLUMN_MIGRATIONS.append(("audit_events", "workspace_id", "INTEGER"))


def run_migrations(engine: Engine) -> None:
    """Create missing tables and add missing columns safely."""
    try:
        Base.metadata.create_all(bind=engine)
        inspector = inspect(engine)
        existing_tables = set(inspector.get_table_names())

        for table_name, column_name, column_def in COLUMN_MIGRATIONS:
            if table_name not in existing_tables:
                continue
            existing_columns = {column["name"] for column in inspector.get_columns(table_name)}
            if column_name in existing_columns:
                continue
            if not table_name.replace("_", "").isalnum() or not column_name.replace("_", "").isalnum():
                raise DatabaseError("Unsafe migration identifier detected.")
            statement = f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}"
            try:
                with engine.begin() as connection:
                    connection.execute(text(statement))
                logger.info("Migration: added column %s.%s", table_name, column_name)
            except Exception as exc:
                logger.warning(
                    "Migration skipped %s.%s: %s",
                    table_name,
                    column_name,
                    type(exc).__name__,
                )

        inspector = inspect(engine)
        final_tables = set(inspector.get_table_names())
        for table_name in Base.metadata.tables:
            if table_name not in final_tables:
                logger.warning("Table still missing after migration: %s", table_name)
                raise DatabaseError(
                    f"Required table missing: {table_name}",
                    reason="Database migration could not create all required tables.",
                    user_action="Check database permissions and restart the app.",
                )
    except DatabaseError:
        raise
    except Exception as exc:
        logger.error("Migration failed: %s", type(exc).__name__, exc_info=True)
        raise DatabaseError(
            "Database migration failed.",
            reason=str(exc),
            user_action="Check database permissions, migration rights, and restart the app.",
            original_exception=exc,
        ) from exc
