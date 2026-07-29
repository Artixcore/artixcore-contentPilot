"""Tests for log and alert secret redaction."""

from core.logging_config import sanitize_log_message


def test_redacts_database_url_credentials():
    raw = "Database failed: postgresql://admin:super-secret@db.internal/contentpilot"
    cleaned = sanitize_log_message(raw)
    assert "super-secret" not in cleaned
    assert "admin:" not in cleaned


def test_redacts_key_value_secrets():
    raw = "password=hunter2 access_token='token-value' client_secret=client-value"
    cleaned = sanitize_log_message(raw)
    assert "hunter2" not in cleaned
    assert "token-value" not in cleaned
    assert "client-value" not in cleaned


def test_redacts_bearer_tokens_and_jwts():
    raw = (
        "Authorization: Bearer secret-token "
        "eyJabcdefghijk.abcdefghijklmnop.qrstuvwxyzabcd"
    )
    cleaned = sanitize_log_message(raw)
    assert "secret-token" not in cleaned
    assert "eyJabcdefghijk" not in cleaned


def test_preserves_non_sensitive_context():
    raw = "Publishing failed for post 42 on linkedin"
    assert sanitize_log_message(raw) == raw
