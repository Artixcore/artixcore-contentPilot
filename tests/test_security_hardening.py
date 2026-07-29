"""Regression tests for the first security-hardening phase."""

from pathlib import Path

from core.models import ProviderLog
from core.router import ProviderRouter


def test_provider_logging_does_not_commit_or_rollback_caller(db_session, monkeypatch):
    """Telemetry must not own the surrounding business transaction."""

    def forbidden(*args, **kwargs):
        raise AssertionError("provider logging must not commit or roll back the caller session")

    monkeypatch.setattr(db_session, "commit", forbidden)
    monkeypatch.setattr(db_session, "rollback", forbidden)

    router = ProviderRouter(session=db_session)
    router._log_provider(
        provider="openai",
        model="test-model",
        task_type="security_regression",
        success=True,
        latency_ms=5,
    )

    assert db_session.query(ProviderLog).filter_by(task_type="security_regression").count() == 1


def test_streamlit_security_defaults_are_enabled():
    config = Path(".streamlit/config.toml").read_text(encoding="utf-8")
    assert "enableCORS = true" in config
    assert "enableXsrfProtection = true" in config
    assert "showErrorDetails = false" in config


def test_reverse_proxy_headers_cover_core_browser_controls():
    headers = Path("deploy/nginx/security_headers.conf").read_text(encoding="utf-8")
    required = (
        "Strict-Transport-Security",
        "Content-Security-Policy",
        "X-Content-Type-Options",
        "X-Frame-Options",
        "Referrer-Policy",
        "Permissions-Policy",
    )
    for header in required:
        assert header in headers


def test_app_does_not_render_raw_exception_strings():
    source = Path("app.py").read_text(encoding="utf-8")
    assert "st.caption(str(exc))" not in source
    assert "handle_exception(exc" in source
