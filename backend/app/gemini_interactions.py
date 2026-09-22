"""Persistent local state for Gemini Interactions API conversations.

Privy keeps only non-sensitive provider identifiers locally: chat id, model,
File Search store fingerprints, interaction id, and message count.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import logging
import time
from pathlib import Path
from typing import Any

import requests

from .logging_utils import elapsed_ms, log_event

logger = logging.getLogger(__name__)

_CACHE_PATH = Path(
    os.environ.get(
        "PRIVY_GEMINI_INTERACTION_CACHE_PATH",
        str(
            Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            / "Privy"
            / "gemini_interaction_cache.json"
        ),
    )
)
_CACHE_VERSION = 5
_LOCK = threading.RLock()
_DEFAULT_MAX_AGE_SECONDS = int(
    os.environ.get("PRIVY_GEMINI_INTERACTION_MAX_AGE_SECONDS", str(20 * 60 * 60))
)


def _load() -> dict[str, Any]:
    try:
        if not _CACHE_PATH.exists():
            return {}
        with _CACHE_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict) or data.get("version") != _CACHE_VERSION:
            return {}
        entries = data.get("entries")
        return entries if isinstance(entries, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save(entries: dict[str, Any]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = _CACHE_PATH.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(
            {"version": _CACHE_VERSION, "entries": entries},
            handle,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    os.replace(temp, _CACHE_PATH)


def conversation_fingerprint(model: str, file_refs: list[dict]) -> str:
    parts = [model]
    for ref in sorted(
        file_refs,
        key=lambda item: str(item.get("file_search_store_name") or ""),
    ):
        parts.append(
            "|".join(
                [
                    str(ref.get("file_search_store_name") or ""),
                    str(ref.get("content_sha256") or ""),
                ]
            )
        )
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def get_interaction_id(
    chat_id: str,
    model: str,
    file_refs: list[dict],
    message_count: int,
) -> str | None:
    """Return the chain id only when the local chat state still matches it."""
    started = time.perf_counter()
    fingerprint = conversation_fingerprint(model, file_refs)
    with _LOCK:
        entry = _load().get(chat_id)
        if not isinstance(entry, dict):
            log_event(logger, "gemini_interaction_cache_miss", chat_id=chat_id, reason="missing_entry", duration_ms=elapsed_ms(started))
            return None
        if entry.get("fingerprint") != fingerprint:
            log_event(logger, "gemini_interaction_cache_miss", chat_id=chat_id, reason="fingerprint_mismatch", duration_ms=elapsed_ms(started))
            return None
        if entry.get("model") != model:
            log_event(logger, "gemini_interaction_cache_miss", chat_id=chat_id, reason="model_mismatch", duration_ms=elapsed_ms(started))
            return None
        try:
            saved_message_count = int(entry.get("message_count"))
            saved_at = float(entry.get("saved_at"))
        except (TypeError, ValueError):
            log_event(logger, "gemini_interaction_cache_miss", chat_id=chat_id, reason="invalid_entry", duration_ms=elapsed_ms(started))
            return None
        if saved_message_count != message_count:
            log_event(logger, "gemini_interaction_cache_miss", chat_id=chat_id, reason="message_count_mismatch", duration_ms=elapsed_ms(started))
            return None
        if time.time() - saved_at >= _DEFAULT_MAX_AGE_SECONDS:
            log_event(logger, "gemini_interaction_cache_miss", chat_id=chat_id, reason="expired", duration_ms=elapsed_ms(started))
            return None
        interaction_id = entry.get("interaction_id")
        result = str(interaction_id) if interaction_id else None
        log_event(logger, "gemini_interaction_cache_hit" if result else "gemini_interaction_cache_miss", chat_id=chat_id, duration_ms=elapsed_ms(started))
        return result


def get_interaction_tool_mode(
    chat_id: str,
    model: str,
    file_refs: list[dict],
    message_count: int,
) -> str | None:
    """Return the tool mode used by the currently reusable interaction."""
    started = time.perf_counter()
    fingerprint = conversation_fingerprint(model, file_refs)
    with _LOCK:
        entry = _load().get(chat_id)
        if not isinstance(entry, dict):
            return None
        if entry.get("fingerprint") != fingerprint or entry.get("model") != model:
            return None
        try:
            saved_message_count = int(entry.get("message_count"))
            saved_at = float(entry.get("saved_at"))
        except (TypeError, ValueError):
            return None
        if saved_message_count != message_count:
            return None
        if time.time() - saved_at >= _DEFAULT_MAX_AGE_SECONDS:
            return None
        mode = entry.get("tool_mode")
        result = str(mode) if mode else None
        log_event(logger, "gemini_interaction_tool_mode_lookup", chat_id=chat_id, tool_mode=result, duration_ms=elapsed_ms(started))
        return result


def save_interaction_id(
    chat_id: str,
    model: str,
    file_refs: list[dict],
    interaction_id: str,
    message_count: int,
    tool_mode: str | None = None,
) -> None:
    """Persist the completed Gemini interaction id for the next chat turn."""
    if not interaction_id:
        return
    with _LOCK:
        entries = _load()
        entries[chat_id] = {
            "model": model,
            "fingerprint": conversation_fingerprint(model, file_refs),
            "interaction_id": interaction_id,
            "message_count": int(message_count),
            "saved_at": time.time(),
            "tool_mode": tool_mode,
        }
        try:
            _save(entries)
        except OSError:
            log_event(logger, "gemini_interaction_cache_write_failed", level=logging.WARNING, chat_id=chat_id)
            pass
        log_event(logger, "gemini_interaction_saved", chat_id=chat_id, tool_mode=tool_mode, message_count=message_count)


def _clear_local(chat_id: str) -> dict[str, Any] | None:
    with _LOCK:
        entries = _load()
        entry = entries.pop(chat_id, None)
        try:
            _save(entries)
        except OSError:
            pass
        return entry if isinstance(entry, dict) else None


def delete_interaction(
    chat_id: str,
    api_key: str | None = None,
) -> None:
    """Delete the provider-side interaction when possible, then clear local state."""
    entry = _clear_local(chat_id)
    if not entry or not api_key:
        return

    interaction_id = entry.get("interaction_id")
    if not interaction_id:
        return

    base_url = os.environ.get(
        "GEMINI_API_ROOT",
        "https://generativelanguage.googleapis.com/v1beta",
    ).strip().rstrip("/")
    try:
        response = requests.delete(
            f"{base_url}/interactions/{interaction_id}",
            headers={
                "x-goog-api-key": api_key,
                "Api-Revision": os.environ.get("GEMINI_API_REVISION", "2026-05-20"),
            },
            timeout=(10, 30),
        )
        if response.status_code not in {200, 204, 404}:
            return
    except requests.RequestException:
        return


def clear_interaction_id(chat_id: str) -> None:
    """Backward-compatible local-only state clear."""
    _clear_local(chat_id)
