"""Pytest fixtures for ContentPilot tests."""

import pytest
from sqlalchemy.orm import Session

import core.database as db_module
from core.chat_database import seed_default_chatbot_settings
from core.database import get_session, init_db, reset_engine, seed_default_brand_profile

_TEST_FERNET_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


@pytest.fixture(autouse=True)
def isolate_env(monkeypatch):
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ADMIN_IDS",
        "TELEGRAM_ALERT_CHAT_IDS",
        "META_PAGE_ACCESS_TOKEN",
        "X_ACCESS_TOKEN",
        "BOOTSTRAP_ADMIN_EMAIL",
        "BOOTSTRAP_ADMIN_PASSWORD",
        "BOOTSTRAP_ADMIN_PASSWORD_HASH",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("APP_DEBUG", "false")
    monkeypatch.setenv("ALERTS_ENABLED", "false")
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setenv("CONTENTPILOT_ENCRYPTION_KEYS", _TEST_FERNET_KEY)
    monkeypatch.setenv("AUTH_SESSION_HOURS", "8")
    monkeypatch.setenv("AUTH_MAX_FAILED_LOGINS", "5")
    monkeypatch.setenv("AUTH_LOCK_MINUTES", "15")


@pytest.fixture
def db_session() -> Session:
    reset_engine("sqlite:///:memory:")
    init_db()
    session = get_session()
    seed_default_brand_profile(session)
    seed_default_chatbot_settings(session)
    session.commit()
    yield session
    session.close()
    reset_engine(db_module.DATABASE_URL)


@pytest.fixture
def approved_post(db_session) -> "Post":
    from core.models import Post

    post = Post(
        platform="linkedin",
        topic="Test topic",
        content="Approved content ready to publish.",
        status="approved",
        provider_used="openai",
        model_used="gpt-4.1-mini",
        input_prompt="Generate post",
        system_prompt="Brand voice",
        raw_ai_response='{"content": "draft"}',
    )
    db_session.add(post)
    db_session.commit()
    db_session.refresh(post)
    return post
