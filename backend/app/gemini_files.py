"""Gemini Files API helper for reusable masked-file references.

Privy uploads only the fully masked CSV to Gemini. The original Excel/CSV bytes
never leave the Privy upload handler. A small local cache remembers the Gemini
File URI so subsequent questions can reference the same remote file instead of
re-uploading the masked dataset.

Gemini Files API objects expire automatically after 48 hours. The cache treats
an entry as stale shortly before the provider expiration and transparently
re-uploads the current masked content when needed.
"""

from __future__ import annotations

import hashlib
import logging
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

logger = logging.getLogger(__name__)

GEMINI_FILES_BASE_URL = os.environ.get(
    "GEMINI_FILES_BASE_URL",
    "https://generativelanguage.googleapis.com/v1beta",
).rstrip("/")
GEMINI_UPLOAD_BASE_URL = os.environ.get(
    "GEMINI_UPLOAD_BASE_URL",
    "https://generativelanguage.googleapis.com/upload/v1beta",
).rstrip("/")

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
_CACHE_VERSION = 1
_CACHE_SAFETY_SECONDS = 60
_DEFAULT_EXPIRY_SECONDS = 48 * 60 * 60


def _content_hash(masked_csv: str) -> str:
    return hashlib.sha256(masked_csv.encode("utf-8")).hexdigest()


def _load_cache() -> dict[str, Any]:
    try:
        if not _CACHE_PATH.exists():
            return {}
        with _CACHE_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            return {}
        if data.get("version") != _CACHE_VERSION:
            return {}
        entries = data.get("entries")
        return entries if isinstance(entries, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _save_cache(entries: dict[str, Any]) -> None:
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path = _CACHE_PATH.with_suffix(".tmp")
    payload = {"version": _CACHE_VERSION, "entries": entries}
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, separators=(",", ":"))
    os.replace(temp_path, _CACHE_PATH)


def _parse_expiration(value: Any) -> float:
    if not value:
        return time.time() + _DEFAULT_EXPIRY_SECONDS
    try:
        text = str(value).strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).astimezone(timezone.utc).timestamp()
    except (TypeError, ValueError, OverflowError):
        return time.time() + _DEFAULT_EXPIRY_SECONDS


def _cache_entry_valid(entry: dict[str, Any], content_hash: str, api_key: str) -> bool:
    if entry.get("content_sha256") != content_hash:
        return False
    api_key_fingerprint = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
    if entry.get("api_key_fingerprint") != api_key_fingerprint:
        return False
    uri = entry.get("file_uri")
    mime_type = entry.get("mime_type")
    if not uri or not mime_type:
        return False
    try:
        expires_at = float(entry.get("expires_at", 0))
    except (TypeError, ValueError):
        return False
    return time.time() < expires_at - _CACHE_SAFETY_SECONDS


def _api_headers(api_key: str) -> dict[str, str]:
    return {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
        "Api-Revision": os.environ.get("GEMINI_API_REVISION", "2026-05-20"),
    }


def _upload_masked_csv(
    *,
    masked_csv: str,
    display_name: str,
    api_key: str,
) -> dict[str, Any]:
    """Upload one masked CSV through Gemini's resumable Files API."""
    data = masked_csv.encode("utf-8")
    start_url = f"{GEMINI_UPLOAD_BASE_URL}/files"
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
            f"Gemini Files API setup failed ({response.status_code}): "
            f"{response.text[:500]}"
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
            f"Gemini file upload failed ({upload_response.status_code}): "
            f"{upload_response.text[:500]}"
        )

    try:
        payload = upload_response.json()
        file_obj = payload["file"]
    except (ValueError, KeyError, TypeError) as exc:
        raise RuntimeError("Gemini Files API returned an invalid upload response") from exc

    return file_obj


def _get_file(file_name: str, api_key: str) -> dict[str, Any]:
    response = requests.get(
        f"{GEMINI_FILES_BASE_URL}/{file_name}",
        headers=_api_headers(api_key),
        timeout=(10, 30),
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Gemini Files API metadata lookup failed ({response.status_code}): "
            f"{response.text[:500]}"
        )
    try:
        payload = response.json()
        return payload.get("file") or payload
    except (ValueError, TypeError):
        raise RuntimeError("Gemini Files API returned invalid file metadata")


def _wait_until_active(file_obj: dict[str, Any], api_key: str) -> dict[str, Any]:
    state = str(file_obj.get("state") or "ACTIVE").upper()
    if state == "ACTIVE":
        return file_obj
    if state not in {"PROCESSING", "STATE_UNSPECIFIED"}:
        raise RuntimeError(f"Gemini file is not usable (state={state})")

    file_name = str(file_obj.get("name") or "")
    if not file_name:
        raise RuntimeError("Gemini upload response did not include a file name")

    deadline = time.time() + float(os.environ.get("PRIVY_GEMINI_FILE_READY_TIMEOUT", "60"))
    while time.time() < deadline:
        time.sleep(1.5)
        current = _get_file(file_name, api_key)
        state = str(current.get("state") or "").upper()
        if state == "ACTIVE":
            return current
        if state not in {"PROCESSING", "STATE_UNSPECIFIED"}:
            raise RuntimeError(f"Gemini file is not usable (state={state})")

    raise RuntimeError("Timed out while waiting for Gemini to finish processing the masked file")


def get_or_upload_gemini_file(
    *,
    file_id: str,
    filename: str,
    masked_csv: str,
    api_key: str,
) -> dict[str, Any]:
    """Return a reusable Gemini File reference for the current masked content."""
    digest = _content_hash(masked_csv)

    with _CACHE_LOCK:
        entries = _load_cache()
        cached = entries.get(file_id)
        if isinstance(cached, dict) and _cache_entry_valid(cached, digest, api_key):
            logger.info("gemini_file_cache_hit file_id=%s", file_id)
            return cached

        # The content changed, the provider credential changed, or the prior
        # entry expired. Remove the previous remote object when possible before
        # creating its replacement so edits do not accumulate orphaned files.
        if isinstance(cached, dict) and cached.get("file_name"):
            try:
                requests.delete(
                    f"{GEMINI_FILES_BASE_URL}/{cached['file_name']}",
                    headers={
                        "x-goog-api-key": api_key,
                        "Api-Revision": os.environ.get("GEMINI_API_REVISION", "2026-05-20"),
                    },
                    timeout=(10, 30),
                )
            except requests.RequestException:
                pass
        entries.pop(file_id, None)

        # Use an internal display name that never mirrors the user's original
        # filename. Only the masked bytes are uploaded to Google.
        display_name = f"privy-masked-{file_id[:8]}-{digest[:12]}.csv"
        file_obj = _upload_masked_csv(
            masked_csv=masked_csv,
            display_name=display_name,
            api_key=api_key,
        )
        file_obj = _wait_until_active(file_obj, api_key)

        file_name = str(file_obj.get("name") or "")
        file_uri = str(file_obj.get("uri") or "")
        mime_type = str(file_obj.get("mimeType") or "text/csv")
        if not file_name or not file_uri:
            raise RuntimeError("Gemini upload did not return a reusable file URI")

        logger.info("gemini_file_uploaded file_id=%s", file_id)
        entry = {
            "content_sha256": digest,
            "api_key_fingerprint": hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16],
            "file_name": file_name,
            "file_uri": file_uri,
            "mime_type": mime_type,
            "uploaded_at": time.time(),
            "expires_at": _parse_expiration(file_obj.get("expirationTime")),
            "source_filename": filename,
        }
        entries[file_id] = entry
        try:
            _save_cache(entries)
        except OSError as exc:
            # The upload succeeded; don't fail the chat just because the local
            # cache could not be written. The next request may re-upload once.
            entries.pop(file_id, None)
        return entry


def delete_gemini_file_cache(file_id: str, api_key: str | None = None) -> None:
    """Delete the cached provider file when the associated Privy file is removed."""
    with _CACHE_LOCK:
        entries = _load_cache()
        entry = entries.pop(file_id, None)
        try:
            _save_cache(entries)
        except OSError:
            pass

    if not entry or not api_key:
        return
    file_name = entry.get("file_name")
    if not file_name:
        return

    try:
        response = requests.delete(
            f"{GEMINI_FILES_BASE_URL}/{file_name}",
            headers={
                        "x-goog-api-key": api_key,
                        "Api-Revision": os.environ.get("GEMINI_API_REVISION", "2026-05-20"),
                    },
            timeout=(10, 30),
        )
        # A 404 is harmless because Gemini files are automatically removed
        # after their retention window.
        if response.status_code not in {200, 204, 404}:
            return
    except requests.RequestException:
        return
