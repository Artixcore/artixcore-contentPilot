"""Regression checks for stable browser sessions and extension-neutral frontend code."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_streamlit_runtime_avoids_reload_and_reconnect_churn() -> None:
    config = (ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert 'fileWatcherType = "none"' in config
    assert "runOnSave = false" in config
    assert "fastReruns = false" in config
    assert "websocketPingInterval = 30" in config
    assert "disconnectedSessionTTL = 300" in config
    assert "enableWebsocketCompression = false" in config


def test_nginx_proxy_preserves_one_stable_websocket_session() -> None:
    proxy = (ROOT / "deploy" / "nginx" / "streamlit_proxy.conf").read_text(
        encoding="utf-8"
    )
    required = (
        "proxy_http_version 1.1;",
        "proxy_set_header Upgrade $http_upgrade;",
        'proxy_set_header Connection "upgrade";',
        "proxy_buffering off;",
        "proxy_request_buffering off;",
        "proxy_read_timeout 3600s;",
        "proxy_next_upstream off;",
    )
    for directive in required:
        assert directive in proxy


def test_application_does_not_patch_or_initialize_browser_wallet_extensions() -> None:
    """Wallet extensions own their injected providers and listener lifecycle.

    Application code must not mask extension leaks with setMaxListeners(), attach
    TronLink listeners, or inject wallet-provider scripts into the page.
    """
    forbidden = (
        "setMaxListeners(",
        "window.tronLink",
        "window.tronWeb",
        "TronLink initiated",
        "app-init-liveness",
        "background-liveness",
        "ObjectMultiplex",
        "Provider initialised",
    )
    extensions = {".py", ".js", ".jsx", ".ts", ".tsx", ".html"}
    excluded_parts = {".git", ".venv", "venv", "node_modules", "tests"}

    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in extensions:
            continue
        if any(part in excluded_parts for part in path.parts):
            continue
        content = path.read_text(encoding="utf-8", errors="ignore")
        for marker in forbidden:
            assert marker not in content, f"Forbidden browser-extension hook in {path}: {marker}"
