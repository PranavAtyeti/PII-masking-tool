"""Privacy-safe file-only observability helpers for Privy.

Application logs are written to a rotating file only. Raw spreadsheet
contents, masking mappings, user prompts, provider secrets, authentication
credentials, and tool result bodies are intentionally excluded from log
fields even if a caller accidentally supplies a sensitive field name.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar, Token
from logging.handlers import RotatingFileHandler
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import uuid4

_REQUEST_ID: ContextVar[str | None] = ContextVar("privy_request_id", default=None)
_LOG_HANDLER_MARKER = "_privy_file_handler"
_MAX_FIELD_CHARS = int(os.environ.get("PRIVY_LOG_MAX_FIELD_CHARS", "500"))

_SENSITIVE_LOG_KEYS = {
    "authorization",
    "api_key",
    "access_token",
    "refresh_token",
    "guest_session",
    "jwt",
    "password",
    "password_hash",
    "secret",
    "cookie",
    "set_cookie",
    "prompt",
    "system_prompt",
    "user_prompt",
    "question",
    "answer",
    "content",
    "body",
    "response",
    "response_body",
    "result",
    "result_body",
    "tool_output",
    "masked_csv",
    "raw_bytes",
    "original",
    "value_norm",
    "known_values",
    "reverse_map",
}


def _log_path() -> Path:
    configured = os.environ.get("PRIVY_LOG_FILE", "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[1] / "logs" / "privy.log"


class _RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


def configure_file_logging() -> Path:
    """Configure the application logger to write only to a rotating file."""
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    app_logger = logging.getLogger("app")
    app_logger.setLevel(logging.INFO)
    app_logger.propagate = False

    existing = None
    for handler in app_logger.handlers:
        if getattr(handler, _LOG_HANDLER_MARKER, False):
            existing = handler
            break

    if existing is not None:
        return path

    handler = RotatingFileHandler(
        path,
        maxBytes=int(os.environ.get("PRIVY_LOG_MAX_BYTES", str(10 * 1024 * 1024))),
        backupCount=int(os.environ.get("PRIVY_LOG_BACKUP_COUNT", "5")),
        encoding="utf-8",
    )
    setattr(handler, _LOG_HANDLER_MARKER, True)
    handler.setLevel(logging.INFO)
    handler.addFilter(_RequestIdFilter())
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s | %(levelname)s | %(name)s | request_id=%(request_id)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    app_logger.addHandler(handler)
    return path


LOG_FILE_PATH = configure_file_logging()


def new_request_id() -> str:
    """Create a short correlation id suitable for backend logs."""
    return uuid4().hex[:12]


def set_request_id(request_id: str) -> Token:
    """Set the request id for the current execution context."""
    return _REQUEST_ID.set(request_id)


def reset_request_id(token: Token) -> None:
    """Restore the previous request-id context."""
    _REQUEST_ID.reset(token)


def get_request_id() -> str | None:
    return _REQUEST_ID.get()


def _is_sensitive_key(key: str) -> bool:
    normalized = str(key).strip().lower()
    return normalized in _SENSITIVE_LOG_KEYS or normalized.endswith(
        ("_prompt", "_question", "_content", "_response", "_result", "_token")
    )


def _safe_value(value: Any) -> str:
    """Format a log value without allowing multiline or oversized log injection."""
    text = str(value)
    text = " ".join(text.replace("\r", " ").replace("\n", " ").split())
    if len(text) > _MAX_FIELD_CHARS:
        text = text[:_MAX_FIELD_CHARS].rstrip() + "…"
    return text


def log_event(
    logger: logging.Logger,
    event: str,
    level: int = logging.INFO,
    **fields: Any,
) -> None:
    """Write a consistent, privacy-safe key/value event."""
    parts = ["event=" + _safe_value(event)]
    request_id = get_request_id()
    if request_id:
        parts.append("request_id=" + _safe_value(request_id))

    for key, value in fields.items():
        if value is None:
            continue
        if _is_sensitive_key(str(key)):
            parts.append(f"{key}=<redacted>")
            continue
        parts.append(f"{key}={_safe_value(value)}")

    logger.log(level, "privy %s", " ".join(parts))


def elapsed_ms(start: float) -> int:
    """Return elapsed monotonic time in milliseconds."""
    return int((perf_counter() - start) * 1000)
