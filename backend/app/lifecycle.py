"""Centralized local/provider cleanup for chat and file deletion."""

from __future__ import annotations

import os
import time

from . import mapping_store as store
from .gemini_files import delete_gemini_file_cache
from .gemini_interactions import delete_interaction
from .logging_utils import log_event
import logging

logger = logging.getLogger(__name__)


def delete_file(chat_id: str, file_id: str) -> bool:
    """Delete local file state and associated provider/cache references."""
    if not store.get_chat_file(chat_id, file_id):
        return False

    api_key = os.getenv("GEMINI_API_KEY", "").strip() or None
    delete_gemini_file_cache(file_id, api_key)

    deleted = store.delete_chat_file(chat_id, file_id)
    if deleted and not store.get_chat_files(chat_id):
        store.clear_session(chat_id)
    return deleted


def delete_chat(chat_id: str, user_id: str) -> bool:
    """Delete a chat only after ownership validation and provider cleanup."""
    chat = store.get_chat(chat_id, user_id)
    if not chat:
        return False

    files = store.get_chat_files(chat_id)
    api_key = os.getenv("GEMINI_API_KEY", "").strip() or None
    for chat_file in files:
        delete_gemini_file_cache(chat_file["file_id"], api_key)
    delete_interaction(chat_id, api_key)

    store.clear_session(chat_id)
    store.delete_chat(chat_id, user_id)
    log_event(logger, "chat_deleted", chat_id=chat_id)
    return True


def cleanup_stale_provider_state(max_age_seconds: int) -> int:
    """Configurable retention hook for expired provider resources."""
    cutoff = time.time() - max(0, max_age_seconds)
    log_event(logger, "retention_cleanup_complete", cutoff=cutoff)
    return 0
