"""Centralized local/provider cleanup for chat and file deletion."""

from __future__ import annotations

import os
import logging

from . import mapping_store as store
from .gemini_files import delete_gemini_file_cache
from .gemini_interactions import delete_interaction
from .logging_utils import log_event

logger = logging.getLogger(__name__)


class ProviderCleanupPending(RuntimeError):
    """Deletion is incomplete because remote provider cleanup did not finish."""


def delete_file(chat_id: str, file_id: str) -> bool:
    """Delete local file state and associated provider/cache references."""
    if not store.get_chat_file(chat_id, file_id):
        return False

    api_key = os.getenv("GEMINI_API_KEY", "").strip() or None
    if not delete_gemini_file_cache(file_id, api_key):
        log_event(
            logger,
            "file_deletion_pending",
            level=logging.WARNING,
            chat_id=chat_id,
            file_id=file_id,
            reason="provider_cleanup_pending",
        )
        raise ProviderCleanupPending("Provider cleanup is pending; retry file deletion.")

    deleted = store.delete_chat_file(chat_id, file_id)
    return deleted


def delete_chat(chat_id: str, user_id: str) -> bool:
    """Delete a chat only after ownership validation and provider cleanup."""
    chat = store.get_chat(chat_id, user_id)
    if not chat:
        return False

    files = store.get_chat_files(chat_id)
    api_key = os.getenv("GEMINI_API_KEY", "").strip() or None
    for chat_file in files:
        if not delete_gemini_file_cache(chat_file["file_id"], api_key):
            raise ProviderCleanupPending("Provider cleanup is pending; retry chat deletion.")
    if not delete_interaction(chat_id, api_key):
        raise ProviderCleanupPending("Provider cleanup is pending; retry chat deletion.")

    store.delete_chat(chat_id, user_id)
    log_event(logger, "chat_deleted", chat_id=chat_id)
    return True
