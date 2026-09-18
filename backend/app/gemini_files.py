"""Gemini File Search helper for reusable masked datasets.

Privy uploads only the fully masked CSV to Google's File Search service. The
original Excel/CSV bytes never leave the Privy upload path.

Each Privy file gets one provider-side File Search store. The store contains
one indexed document for the current masked content. Subsequent questions
reuse only the store name; the masked file bytes are NOT uploaded again.

File Search stores persist until explicitly deleted, so Privy must delete the
provider store when its associated Privy file is permanently removed.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

GEMINI_API_ROOT = os.environ.get(
    "GEMINI_API_ROOT",
    "https://generativelanguage.googleapis.com/v1beta",
).rstrip("/")
GEMINI_FILE_SEARCH_UPLOAD_ROOT = os.environ.get(
    "GEMINI_FILE_SEARCH_UPLOAD_ROOT",
    "https://generativelanguage.googleapis.com/upload/v1beta",
).rstrip("/")

_CACHE_PATH = Path(
    os.environ.get(
        "PRIVY_GEMINI_FILE_CACHE_PATH",
        str(
            Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            / "Privy"
            / "gemini_file_search_cache.json"
        ),
    )
)

_CACHE_LOCK = threading.RLock()
_CACHE_VERSION = 2
_DEFAULT_OPERATION_TIMEOUT = float(
    os.environ.get("PRIVY_GEMINI_FILE_SEARCH_READY_TIMEOUT", "180")
)
_DEFAULT_POLL_SECONDS = float(
    os.environ.get("PRIVY_GEMINI_FILE_SEARCH_POLL_SECONDS", "1.5")
)
_DEFAULT_EMBEDDING_MODEL = os.environ.get(
    "GEMINI_FILE_SEARCH_EMBEDDING_MODEL",
    "models/gemini-embedding-2",
).strip()


def _content_hash(masked_csv: str) -> str:
    return hashlib.sha256(masked_csv.encode("utf-8")).hexdigest()


def _api_headers(api_key: str) -> dict[str, str]:
    return {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
        "Api-Revision": os.environ.get("GEMINI_API_REVISION", "2026-05-20"),
    }


def _load_cache() -> dict[str, Any]:
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


def _save_cache(entries: dict[str, Any]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _CACHE_PATH.with_suffix(".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(
            {"version": _CACHE_VERSION, "entries": entries},
            handle,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    os.replace(temp_path, _CACHE_PATH)


def _api_key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _cache_entry_valid(
    entry: dict[str, Any],
    content_hash: str,
    api_key: str,
) -> bool:
    if entry.get("content_sha256") != content_hash:
        return False
    if entry.get("api_key_fingerprint") != _api_key_fingerprint(api_key):
        return False
    return bool(entry.get("store_name"))


def _create_store(
    *,
    file_id: str,
    content_hash: str,
    api_key: str,
) -> dict[str, Any]:
    display_name = f"privy-file-{file_id[:8]}-{content_hash[:12]}"
    payload: dict[str, Any] = {
        "displayName": display_name,
    }
    if _DEFAULT_EMBEDDING_MODEL:
        payload["embeddingModel"] = _DEFAULT_EMBEDDING_MODEL

    response = requests.post(
        f"{GEMINI_API_ROOT}/fileSearchStores",
        headers=_api_headers(api_key),
        json=payload,
        timeout=(10, 30),
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Gemini File Search store creation failed ({response.status_code}): "
            f"{response.text[:500]}"
        )

    try:
        store = response.json()
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Gemini File Search returned an invalid store response") from exc

    store_name = str(store.get("name") or "")
    if not store_name:
        raise RuntimeError("Gemini File Search store response did not include a store name")
    return store


def _start_upload(
    *,
    store_name: str,
    masked_csv: str,
    display_name: str,
    api_key: str,
) -> tuple[str, int]:
    data = masked_csv.encode("utf-8")
    start_url = f"{GEMINI_FILE_SEARCH_UPLOAD_ROOT}/{store_name}:uploadToFileSearchStore"
    headers = {
        "x-goog-api-key": api_key,
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(len(data)),
        "X-Goog-Upload-Header-Content-Type": "text/csv",
        "Content-Type": "application/json",
    }

    response = requests.post(
        start_url,
        headers=headers,
        json={"displayName": display_name},
        timeout=(10, 30),
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Gemini File Search upload setup failed ({response.status_code}): "
            f"{response.text[:500]}"
        )

    upload_url = response.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        raise RuntimeError("Gemini File Search did not return an upload URL")
    return upload_url, len(data)


def _upload_to_store(
    *,
    store_name: str,
    masked_csv: str,
    display_name: str,
    api_key: str,
) -> dict[str, Any]:
    upload_url, byte_count = _start_upload(
        store_name=store_name,
        masked_csv=masked_csv,
        display_name=display_name,
        api_key=api_key,
    )
    data = masked_csv.encode("utf-8")
    response = requests.post(
        upload_url,
        headers={
            "Content-Length": str(byte_count),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
            "Content-Type": "text/csv",
        },
        data=data,
        timeout=(10, 180),
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Gemini File Search upload failed ({response.status_code}): "
            f"{response.text[:500]}"
        )

    try:
        operation = response.json()
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Gemini File Search returned an invalid upload operation") from exc
    return operation


def _operation_done(operation: dict[str, Any]) -> bool:
    return bool(operation.get("done"))


def _poll_operation(operation: dict[str, Any], api_key: str) -> dict[str, Any]:
    if _operation_done(operation):
        return operation

    operation_name = str(operation.get("name") or "")
    if not operation_name:
        raise RuntimeError("Gemini File Search upload did not return an operation name")

    deadline = time.time() + _DEFAULT_OPERATION_TIMEOUT
    while time.time() < deadline:
        time.sleep(_DEFAULT_POLL_SECONDS)
        response = requests.get(
            f"{GEMINI_API_ROOT}/{operation_name}",
            headers={
                "x-goog-api-key": api_key,
                "Api-Revision": os.environ.get("GEMINI_API_REVISION", "2026-05-20"),
            },
            timeout=(10, 30),
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"Gemini File Search operation lookup failed ({response.status_code}): "
                f"{response.text[:500]}"
            )

        try:
            operation = response.json()
        except (ValueError, TypeError) as exc:
            raise RuntimeError("Gemini File Search returned an invalid operation response") from exc

        if _operation_done(operation):
            error = operation.get("error")
            if error:
                message = error.get("message") if isinstance(error, dict) else str(error)
                raise RuntimeError(f"Gemini File Search indexing failed: {message}")
            return operation

    raise RuntimeError("Timed out while Gemini was indexing the masked file")


def _delete_store(store_name: str, api_key: str) -> None:
    try:
        response = requests.delete(
            f"{GEMINI_API_ROOT}/{store_name}",
            params={"force": "true"},
            headers={
                "x-goog-api-key": api_key,
                "Api-Revision": os.environ.get("GEMINI_API_REVISION", "2026-05-20"),
            },
            timeout=(10, 30),
        )
        if response.status_code not in {200, 204, 404}:
            logger.warning(
                "gemini_file_search_store_delete_failed status=%s",
                response.status_code,
            )
    except requests.RequestException:
        logger.warning("gemini_file_search_store_delete_request_failed")


def _store_exists(store_name: str, api_key: str) -> bool:
    try:
        response = requests.get(
            f"{GEMINI_API_ROOT}/{store_name}",
            headers={
                "x-goog-api-key": api_key,
                "Api-Revision": os.environ.get("GEMINI_API_REVISION", "2026-05-20"),
            },
            timeout=(10, 30),
        )
    except requests.RequestException:
        # Don't turn a transient metadata lookup failure into a forced re-upload.
        return True
    return response.status_code == 200


def get_or_upload_gemini_file(
    *,
    file_id: str,
    filename: str,
    masked_csv: str,
    api_key: str,
) -> dict[str, Any]:
    """Return a persistent File Search store for the current masked content.

    The function name stays backward-compatible with the previous helper, but
    the returned object now contains ``file_search_store_name`` instead of a
    raw Files API URI.
    """
    digest = _content_hash(masked_csv)

    with _CACHE_LOCK:
        entries = _load_cache()
        cached = entries.get(file_id)

        if (
            isinstance(cached, dict)
            and _cache_entry_valid(cached, digest, api_key)
            and _store_exists(str(cached["store_name"]), api_key)
        ):
            logger.info("gemini_file_search_cache_hit file_id=%s", file_id)
            return cached

        if isinstance(cached, dict) and cached.get("store_name"):
            _delete_store(str(cached["store_name"]), api_key)
        entries.pop(file_id, None)

        store = _create_store(
            file_id=file_id,
            content_hash=digest,
            api_key=api_key,
        )
        store_name = str(store["name"])

        display_name = f"privy-masked-{file_id[:8]}-{digest[:12]}.csv"
        upload_operation = _upload_to_store(
            store_name=store_name,
            masked_csv=masked_csv,
            display_name=display_name,
            api_key=api_key,
        )
        completed_operation = _poll_operation(upload_operation, api_key)

        response = completed_operation.get("response") or {}
        document = response.get("document") or {}
        document_name = str(document.get("name") or "")

        logger.info("gemini_file_search_indexed file_id=%s", file_id)
        entry = {
            "content_sha256": digest,
            "api_key_fingerprint": _api_key_fingerprint(api_key),
            "store_name": store_name,
            "document_name": document_name,
            "indexed_at": time.time(),
            "source_filename": filename,
        }
        entries[file_id] = entry
        try:
            _save_cache(entries)
        except OSError:
            logger.warning("gemini_file_search_cache_write_failed file_id=%s", file_id)
        return entry


def delete_gemini_file_cache(file_id: str, api_key: str | None = None) -> None:
    """Delete the provider File Search store associated with a Privy file."""
    with _CACHE_LOCK:
        entries = _load_cache()
        entry = entries.pop(file_id, None)
        try:
            _save_cache(entries)
        except OSError:
            pass

    if not entry or not api_key:
        return
    store_name = entry.get("store_name")
    if store_name:
        _delete_store(str(store_name), api_key)
