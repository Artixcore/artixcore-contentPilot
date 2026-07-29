"""Centralized logging with secret sanitization and rotating file handler."""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.utils import sanitize_text

_LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
_LOG_FILE = _LOG_DIR / "contentpilot.log"
_CONFIGURED = False

_SECRET_PATTERNS = (
    re.compile(r"sk-[a-zA-Z0-9_-]{10,}", re.IGNORECASE),
    re.compile(r"sk-ant-[a-zA-Z0-9_-]{10,}", re.IGNORECASE),
    re.compile(r"Bearer\s+[a-zA-Z0-9._~+/-]+=*", re.IGNORECASE),
    re.compile(r"Authorization\s*[:=]\s*[^\s,;]+", re.IGNORECASE),
    re.compile(
        r"(?:password|passwd|pwd|api[_-]?key|api[_-]?secret|client[_-]?secret|"
        r"access[_-]?token|refresh[_-]?token|verify[_-]?token|secret|token)"
        r"\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)",
        re.IGNORECASE,
    ),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s/]+@", re.IGNORECASE),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\beyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\b"),
)


def sanitize_log_message(message: str) -> str:
    """Sanitize a log message before writing or alerting."""
    if not message:
        return ""
    text = sanitize_text(str(message))
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("****", text)
    return text


class SanitizingFormatter(logging.Formatter):
    """Formatter that redacts secrets from log records."""

    def format(self, record: logging.LogRecord) -> str:
        original_msg = record.msg
        original_args = record.args
        try:
            if record.msg:
                record.msg = sanitize_log_message(str(record.msg))
            if record.args:
                record.args = tuple(
                    sanitize_log_message(str(arg)) if isinstance(arg, str) else arg
                    for arg in record.args
                )
            return sanitize_log_message(super().format(record))
        finally:
            record.msg = original_msg
            record.args = original_args


class ContextAdapter(logging.LoggerAdapter):
    """Logger adapter that injects action/request context."""

    def process(self, msg, kwargs):
        context = sanitize_log_message(str(self.extra.get("context", "")))
        if context:
            return f"[{context}] {msg}", kwargs
        return msg, kwargs


def setup_logging(level: str | None = None) -> None:
    """Configure console and rotating file logging once."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    log_level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, log_level_name, logging.INFO)
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    formatter = SanitizingFormatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(log_level)

    console = logging.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(formatter)
    console._contentpilot_handler = True  # type: ignore[attr-defined]

    file_handler = RotatingFileHandler(
        _LOG_FILE,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    file_handler._contentpilot_handler = True  # type: ignore[attr-defined]

    if not any(getattr(handler, "_contentpilot_handler", False) for handler in root.handlers):
        root.addHandler(console)
        root.addHandler(file_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    _CONFIGURED = True
    logging.getLogger(__name__).info(
        "Logging initialized (level=%s, file=%s)",
        log_level_name,
        _LOG_FILE,
    )


def get_logger(name: str, context: str | None = None) -> logging.Logger | ContextAdapter:
    """Return a logger, optionally wrapped with sanitized context."""
    logger = logging.getLogger(name)
    if context:
        return ContextAdapter(logger, {"context": context})
    return logger


def get_log_file_path() -> Path:
    return _LOG_FILE
