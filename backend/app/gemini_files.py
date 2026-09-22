"""Gemini provider-file manager for Privy's masked datasets.

Privy masks the user's spreadsheet locally and sends only the masked CSV to
Gemini.  The same masked CSV is registered in two Gemini-side resources:

1. Gemini Files API: a temporary reusable File URI used when Code Execution
   needs the actual dataset as a file input.  Files API objects expire after
   48 hours.
2. Gemini File Search Store: a persistent indexed representation used for
   semantic retrieval.  File Search stores remain until explicitly deleted.

The bytes are uploaded only when the masked content changes or the temporary
Files API object expires.  Normal chat turns reuse provider identifiers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from .logging_utils import elapsed_ms, log_event
from .masking import looks_unmasked

logger = logging.getLogger(__name__)

GEMINI_API_ROOT = os.environ.get(
    "GEMINI_API_ROOT",
    "https://generativelanguage.googleapis.com/v1beta",
).strip().rstrip("/")
GEMINI_UPLOAD_ROOT = os.environ.get(
    "GEMINI_UPLOAD_ROOT",
    "https://generativelanguage.googleapis.com/upload/v1beta",
).strip().rstrip("/")

_CACHE_PATH = Path(
    os.environ.get(
        "PRIVY_GEMINI_FILE_CACHE_PATH",
        str(
            Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
            / "Privy"
            / "gemini_file_cache.json"
        ),
    )
)

_CACHE_LOCK = threading.RLock()
_CACHE_VERSION = 6
_CACHE_SAFETY_SECONDS = 60
_DEFAULT_FILE_EXPIRY_SECONDS = 48 * 60 * 60
_DEFAULT_OPERATION_TIMEOUT = float(
    os.environ.get("PRIVY_GEMINI_FILE_SEARCH_READY_TIMEOUT", "180")
)
_DEFAULT_POLL_SECONDS = float(
    os.environ.get("PRIVY_GEMINI_FILE_SEARCH_POLL_SECONDS", "1.5")
)
_DEFAULT_STORE_VERIFY_SECONDS = float(
    os.environ.get("PRIVY_GEMINI_STORE_VERIFY_SECONDS", str(10 * 60))
)
_DEFAULT_EMBEDDING_MODEL = os.environ.get(
    "GEMINI_FILE_SEARCH_EMBEDDING_MODEL",
    "models/gemini-embedding-2",
).strip()


def _content_hash(masked_csv: str) -> str:
    return hashlib.sha256(masked_csv.encode("utf-8")).hexdigest()


def _api_key_fingerprint(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


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


def _headers(api_key: str) -> dict[str, str]:
    return {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
        "Api-Revision": os.environ.get("GEMINI_API_REVISION", "2026-05-20"),
    }


def _parse_expiration(value: Any) -> float:
    if not value:
        return time.time() + _DEFAULT_FILE_EXPIRY_SECONDS
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return time.time() + _DEFAULT_FILE_EXPIRY_SECONDS


def _raw_file_valid(entry: dict[str, Any], content_hash: str, api_key: str) -> bool:
    if entry.get("content_sha256") != content_hash:
        return False
    if entry.get("api_key_fingerprint") != _api_key_fingerprint(api_key):
        return False
    if not entry.get("file_name") or not entry.get("file_uri"):
        return False
    try:
        expires_at = float(entry.get("expires_at", 0))
    except (TypeError, ValueError):
        return False
    return time.time() < expires_at - _CACHE_SAFETY_SECONDS


def _store_cache_key_valid(entry: dict[str, Any], content_hash: str, api_key: str) -> bool:
    return (
        entry.get("content_sha256") == content_hash
        and entry.get("api_key_fingerprint") == _api_key_fingerprint(api_key)
        and bool(entry.get("store_name"))
    )


def _store_verification_fresh(entry: dict[str, Any]) -> bool:
    try:
        verified_at = float(entry.get("store_verified_at", 0))
    except (TypeError, ValueError):
        return False
    return (time.time() - verified_at) < _DEFAULT_STORE_VERIFY_SECONDS


def _get_json(response: requests.Response, message: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except (ValueError, TypeError) as exc:
        raise RuntimeError(message) from exc
    if not isinstance(payload, dict):
        raise RuntimeError(message)
    return payload


def _upload_raw_file(
    *,
    masked_csv: str,
    display_name: str,
    api_key: str,
) -> dict[str, Any]:
    """Upload masked CSV once to the Gemini Files API."""
    started = time.perf_counter()
    data = masked_csv.encode("utf-8")
    log_event(logger, "gemini_raw_file_upload_start", bytes=len(data))
    start_url = f"{GEMINI_UPLOAD_ROOT}/files"
    start_headers = {
        "x-goog-api-key": api_key,
        "X-Goog-Upload-Protocol": "resumable",
        "X-Goog-Upload-Command": "start",
        "X-Goog-Upload-Header-Content-Length": str(len(data)),
        "X-Goog-Upload-Header-Content-Type": "text/csv",
        "Content-Type": "application/json",
    }

    response = requests.post(
        start_url,
        headers=start_headers,
        json={"file": {"display_name": display_name}},
        timeout=(10, 30),
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Gemini Files API setup failed (HTTP {response.status_code})"
        )

    upload_url = response.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        raise RuntimeError("Gemini Files API did not return an upload URL")

    upload_response = requests.post(
        upload_url,
        headers={
            "Content-Length": str(len(data)),
            "X-Goog-Upload-Offset": "0",
            "X-Goog-Upload-Command": "upload, finalize",
            "Content-Type": "text/csv",
        },
        data=data,
        timeout=(10, 180),
    )
    if upload_response.status_code >= 400:
        raise RuntimeError(
            f"Gemini file upload failed (HTTP {upload_response.status_code})"
        )

    payload = _get_json(
        upload_response,
        "Gemini Files API returned an invalid upload response",
    )
    file_obj = payload.get("file") or payload
    if not isinstance(file_obj, dict):
        raise RuntimeError("Gemini Files API returned an invalid file object")

    active_file = _wait_until_file_active(file_obj, api_key)
    log_event(
        logger,
        "gemini_raw_file_upload_complete",
        duration_ms=elapsed_ms(started),
        file_name=active_file.get("name"),
        state=active_file.get("state"),
    )
    return active_file


def _get_file(file_name: str, api_key: str) -> dict[str, Any]:
    response = requests.get(
        f"{GEMINI_API_ROOT}/{file_name}",
        headers=_headers(api_key),
        timeout=(10, 30),
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Gemini Files API metadata lookup failed (HTTP {response.status_code})"
        )
    payload = _get_json(
        response,
        "Gemini Files API returned invalid file metadata",
    )
    file_obj = payload.get("file") or payload
    if not isinstance(file_obj, dict):
        raise RuntimeError("Gemini Files API returned invalid file metadata")
    return file_obj


def _wait_until_file_active(file_obj: dict[str, Any], api_key: str) -> dict[str, Any]:
    started = time.perf_counter()
    state = str(file_obj.get("state") or "ACTIVE").upper()
    if state == "ACTIVE":
        log_event(logger, "gemini_raw_file_active", duration_ms=elapsed_ms(started), state=state)
        return file_obj
    if state not in {"PROCESSING", "STATE_UNSPECIFIED"}:
        raise RuntimeError(f"Gemini file is not usable (state={state})")

    file_name = str(file_obj.get("name") or "")
    if not file_name:
        raise RuntimeError("Gemini upload did not return a file name")

    deadline = time.time() + 60
    while time.time() < deadline:
        time.sleep(1.5)
        current = _get_file(file_name, api_key)
        state = str(current.get("state") or "").upper()
        if state == "ACTIVE":
            log_event(logger, "gemini_raw_file_active", duration_ms=elapsed_ms(started), state=state)
            return current
        if state not in {"PROCESSING", "STATE_UNSPECIFIED"}:
            raise RuntimeError(f"Gemini file is not usable (state={state})")

    log_event(logger, "gemini_raw_file_timeout", level=logging.WARNING, duration_ms=elapsed_ms(started))
    raise RuntimeError("Timed out while Gemini finished processing the masked file")


def _create_store(file_id: str, digest: str, api_key: str) -> dict[str, Any]:
    started = time.perf_counter()
    log_event(logger, "gemini_file_search_store_create_start", file_id=file_id)
    payload: dict[str, Any] = {
        "displayName": f"privy-file-{file_id[:8]}-{digest[:12]}",
    }
    if _DEFAULT_EMBEDDING_MODEL:
        payload["embeddingModel"] = _DEFAULT_EMBEDDING_MODEL

    response = requests.post(
        f"{GEMINI_API_ROOT}/fileSearchStores",
        headers=_headers(api_key),
        json=payload,
        timeout=(10, 30),
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Gemini File Search store creation failed (HTTP {response.status_code})"
        )

    store = _get_json(response, "Gemini File Search returned an invalid store response")
    name = str(store.get("name") or "")
    if not name:
        raise RuntimeError("Gemini File Search store response did not include a store name")
    log_event(
        logger,
        "gemini_file_search_store_create_complete",
        duration_ms=elapsed_ms(started),
        store_name=name,
    )
    return store


def _import_file_into_store(
    *,
    store_name: str,
    file_name: str,
    api_key: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    log_event(logger, "gemini_file_search_import_start", store_name=store_name)
    response = requests.post(
        f"{GEMINI_API_ROOT}/{store_name}:importFile",
        headers=_headers(api_key),
        json={"fileName": file_name},
        timeout=(10, 30),
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Gemini File Search import failed (HTTP {response.status_code})"
        )
    operation = _poll_operation(
        _get_json(response, "Gemini File Search returned an invalid import operation"),
        api_key,
    )
    log_event(
        logger,
        "gemini_file_search_import_complete",
        duration_ms=elapsed_ms(started),
        store_name=store_name,
    )
    return operation


def _poll_operation(operation: dict[str, Any], api_key: str) -> dict[str, Any]:
    started = time.perf_counter()
    if bool(operation.get("done")):
        log_event(logger, "gemini_operation_already_complete")
        error = operation.get("error")
        if error:
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise RuntimeError(f"Gemini operation failed: {message}")
        return operation

    operation_name = str(operation.get("name") or "")
    if not operation_name:
        raise RuntimeError("Gemini operation did not return an operation name")

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
                f"Gemini operation lookup failed (HTTP {response.status_code})"
            )

        operation = _get_json(response, "Gemini returned an invalid operation response")
        if bool(operation.get("done")):
            error = operation.get("error")
            if error:
                message = error.get("message") if isinstance(error, dict) else str(error)
                raise RuntimeError(f"Gemini operation failed: {message}")
            log_event(logger, "gemini_operation_complete", duration_ms=elapsed_ms(started))
            return operation

    log_event(logger, "gemini_operation_timeout", level=logging.WARNING, duration_ms=elapsed_ms(started))
    raise RuntimeError("Timed out while Gemini was processing the masked file")


def _store_exists(store_name: str, api_key: str) -> bool:
    started = time.perf_counter()
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
        # Preserve a valid local cache on transient metadata failures.
        log_event(logger, "gemini_file_search_store_check_error", level=logging.WARNING, duration_ms=elapsed_ms(started))
        return True
    exists = response.status_code == 200
    log_event(logger, "gemini_file_search_store_check", exists=exists, status_code=response.status_code, duration_ms=elapsed_ms(started))
    return exists


def _delete_raw_file(file_name: str, api_key: str) -> None:
    try:
        response = requests.delete(
            f"{GEMINI_API_ROOT}/{file_name}",
            headers={
                "x-goog-api-key": api_key,
                "Api-Revision": os.environ.get("GEMINI_API_REVISION", "2026-05-20"),
            },
            timeout=(10, 30),
        )
        if response.status_code not in {200, 204, 404}:
            log_event(logger, "gemini_raw_file_delete_failed", level=logging.WARNING, status_code=response.status_code)
    except requests.RequestException:
        log_event(logger, "gemini_raw_file_delete_request_failed", level=logging.WARNING)


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
            log_event(logger, "gemini_file_search_store_delete_failed", level=logging.WARNING, status_code=response.status_code)
    except requests.RequestException:
        log_event(logger, "gemini_file_search_store_delete_request_failed", level=logging.WARNING)


def _refresh_raw_file(
    *,
    entry: dict[str, Any],
    file_id: str,
    filename: str,
    masked_csv: str,
    digest: str,
    api_key: str,
) -> dict[str, Any]:
    old_file_name = entry.get("file_name")
    if old_file_name:
        _delete_raw_file(str(old_file_name), api_key)

    display_name = f"privy-masked-{file_id[:8]}-{digest[:12]}.csv"
    file_obj = _upload_raw_file(
        masked_csv=masked_csv,
        display_name=display_name,
        api_key=api_key,
    )

    file_name = str(file_obj.get("name") or "")
    file_uri = str(file_obj.get("uri") or "")
    mime_type = str(file_obj.get("mimeType") or "text/csv")
    if not file_name or not file_uri:
        raise RuntimeError("Gemini upload did not return a reusable file URI")

    entry.update(
        {
            "content_sha256": digest,
            "api_key_fingerprint": _api_key_fingerprint(api_key),
            "file_name": file_name,
            "file_uri": file_uri,
            "mime_type": mime_type,
            "uploaded_at": time.time(),
            "expires_at": _parse_expiration(file_obj.get("expirationTime")),
            "source_filename": filename,
        }
    )
    return entry


def get_or_upload_gemini_file(
    *,
    file_id: str,
    filename: str,
    masked_csv: str,
    api_key: str,
) -> dict[str, Any]:
    """Return reusable Gemini resources for the current masked content.

    The masked CSV is uploaded only when necessary.  The raw File API resource
    and persistent File Search Store are both cached by content hash.

    The provider boundary is intentionally defensive: even if the caller has
    already validated the dataset, this function re-validates the exact bytes
    before any cache hit or provider request can reuse them.
    """
    started = time.perf_counter()

    if looks_unmasked(masked_csv):
        log_event(
            logger,
            "gemini_outbound_file_blocked",
            level=logging.ERROR,
            file_id=file_id,
            bytes=len(masked_csv.encode("utf-8")),
        )
        raise RuntimeError("Gemini upload blocked by outbound privacy validation")

    digest = _content_hash(masked_csv)
    log_event(logger, "gemini_file_prepare_start", file_id=file_id, bytes=len(masked_csv.encode("utf-8")))

    with _CACHE_LOCK:
        entries = _load_cache()
        cached = entries.get(file_id)

        if isinstance(cached, dict) and _store_cache_key_valid(cached, digest, api_key):
            store_name = str(cached["store_name"])
            store_is_available = True

            if not _store_verification_fresh(cached):
                store_is_available = _store_exists(store_name, api_key)
                if store_is_available:
                    cached["store_verified_at"] = time.time()
                    entries[file_id] = cached
                    try:
                        _save_cache(entries)
                    except OSError:
                        log_event(logger, "gemini_file_cache_write_failed", level=logging.WARNING)

            if store_is_available:
                if not _raw_file_valid(cached, digest, api_key):
                    cached = _refresh_raw_file(
                        entry=cached,
                        file_id=file_id,
                        filename=filename,
                        masked_csv=masked_csv,
                        digest=digest,
                        api_key=api_key,
                    )
                    cached["store_verified_at"] = time.time()
                    entries[file_id] = cached
                    try:
                        _save_cache(entries)
                    except OSError:
                        log_event(logger, "gemini_file_cache_write_failed", level=logging.WARNING)
                    log_event(
                        logger,
                        "gemini_raw_file_refreshed",
                        file_id=file_id,
                        duration_ms=elapsed_ms(started),
                    )
                    return cached

                log_event(
                    logger,
                    "gemini_file_cache_hit",
                    file_id=file_id,
                    duration_ms=elapsed_ms(started),
                    raw_file_reused=True,
                    file_search_store_reused=True,
                    store_check_skipped=_store_verification_fresh(cached),
                )
                return cached

        if isinstance(cached, dict):
            if cached.get("file_name"):
                _delete_raw_file(str(cached["file_name"]), api_key)
            if cached.get("store_name"):
                _delete_store(str(cached["store_name"]), api_key)
        entries.pop(file_id, None)

        # One upload to the temporary Files API.
        display_name = f"privy-masked-{file_id[:8]}-{digest[:12]}.csv"
        file_obj = _upload_raw_file(
            masked_csv=masked_csv,
            display_name=display_name,
            api_key=api_key,
        )
        file_name = str(file_obj.get("name") or "")
        file_uri = str(file_obj.get("uri") or "")
        mime_type = str(file_obj.get("mimeType") or "text/csv")
        if not file_name or not file_uri:
            raise RuntimeError("Gemini upload did not return a reusable file URI")

        # Import that same uploaded File into a persistent File Search store.
        store = _create_store(
            file_id=file_id,
            digest=digest,
            api_key=api_key,
        )
        store_name = str(store["name"])
        try:
            import_operation = _import_file_into_store(
                store_name=store_name,
                file_name=file_name,
                api_key=api_key,
            )
        except Exception:
            _delete_store(store_name, api_key)
            _delete_raw_file(file_name, api_key)
            raise

        document_name = ""
        response = import_operation.get("response") or {}
        document = response.get("document") or {}
        if isinstance(document, dict):
            document_name = str(document.get("name") or "")

        entry = {
            "content_sha256": digest,
            "api_key_fingerprint": _api_key_fingerprint(api_key),
            "store_name": store_name,
            "document_name": document_name,
            "file_name": file_name,
            "file_uri": file_uri,
            "mime_type": mime_type,
            "uploaded_at": time.time(),
            "expires_at": _parse_expiration(file_obj.get("expirationTime")),
            "indexed_at": time.time(),
            "store_verified_at": time.time(),
            "source_filename": filename,
        }
        entries[file_id] = entry
        try:
            _save_cache(entries)
        except OSError:
            log_event(logger, "gemini_file_cache_write_failed", level=logging.WARNING)

        log_event(logger, "gemini_file_registered", file_id=file_id)
        return entry


def delete_gemini_file_cache(file_id: str, api_key: str | None = None) -> None:
    """Delete both provider resources associated with a Privy file."""
    with _CACHE_LOCK:
        entries = _load_cache()
        entry = entries.pop(file_id, None)
        try:
            _save_cache(entries)
        except OSError:
            pass

    if not entry or not api_key:
        return
    if entry.get("file_name"):
        _delete_raw_file(str(entry["file_name"]), api_key)
    if entry.get("store_name"):
        _delete_store(str(entry["store_name"]), api_key)
