"""Persistent local state for Gemini Interactions API conversations.

Privy keeps only non-sensitive provider identifiers locally: chat id, model,
File Search store fingerprints, interaction id, and message count.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

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
_CACHE_VERSION = 3
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
    fingerprint = conversation_fingerprint(model, file_refs)
    with _LOCK:
        entry = _load().get(chat_id)
        if not isinstance(entry, dict):
            return None
        if entry.get("fingerprint") != fingerprint:
            return None
        if entry.get("model") != model:
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
        interaction_id = entry.get("interaction_id")
        return str(interaction_id) if interaction_id else None


def save_interaction_id(
    chat_id: str,
    model: str,
    file_refs: list[dict],
    interaction_id: str,
    message_count: int,
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
        }
        try:
            _save(entries)
        except OSError:
            pass


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
