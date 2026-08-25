"""
storage.py
----------
Google Cloud Storage adapter for encrypted Privy files.

The adapter stores ONLY ciphertext. Callers are responsible for passing the
plaintext into encryption.encrypt_bytes() first, or use put_encrypted_bytes().

Configuration:
    GCS_BUCKET_NAME=privy-files-dev
    GCS_PROJECT_ID=your-project-id   (optional when ADC already knows it)

Authentication uses Google Application Default Credentials. For local
Windows development, this can be configured with:
    gcloud auth application-default login

No GCP calls are made when this module is imported.
"""

from __future__ import annotations

import os

from google.cloud import storage

from .encryption import decrypt_bytes, encrypt_bytes


class StorageConfigError(RuntimeError):
    """Raised when GCS configuration is missing."""


class GCSStorage:
    def __init__(self) -> None:
        self.bucket_name = os.getenv("GCS_BUCKET_NAME", "").strip()
        if not self.bucket_name:
            raise StorageConfigError(
                "GCS_BUCKET_NAME is not configured. "
                "Create a bucket and add its name to backend .env."
            )

        project = os.getenv("GCS_PROJECT_ID", "").strip() or None
        self.client = storage.Client(project=project)
        self.bucket = self.client.bucket(self.bucket_name)

    @staticmethod
    def object_name(chat_id: str, file_id: str, kind: str = "original") -> str:
        if not chat_id or not file_id:
            raise ValueError("chat_id and file_id are required")
        if kind not in {"original", "masked"}:
            raise ValueError("kind must be 'original' or 'masked'")
        return f"chats/{chat_id}/{file_id}/{kind}.enc"

    def upload_encrypted(
        self,
        *,
        chat_id: str,
        file_id: str,
        data: bytes,
        kind: str = "original",
        content_type: str = "application/octet-stream",
    ) -> str:
        """Encrypt bytes in memory, then upload ciphertext to GCS."""
        object_id = self.object_name(chat_id, file_id, kind)
        encrypted = encrypt_bytes(data, object_id)

        blob = self.bucket.blob(object_id)
        blob.upload_from_string(
            encrypted.payload,
            content_type=content_type,
        )
        return object_id

    def download_decrypted(
        self,
        *,
        chat_id: str,
        file_id: str,
        kind: str = "original",
    ) -> bytes:
        """Download ciphertext and decrypt it only in application memory."""
        object_id = self.object_name(chat_id, file_id, kind)
        blob = self.bucket.blob(object_id)
        payload = blob.download_as_bytes()
        return decrypt_bytes(payload, object_id)

    def delete(self, *, chat_id: str, file_id: str, kind: str | None = None) -> None:
        """Delete one encrypted object or both representations for a file."""
        kinds = [kind] if kind else ["original", "masked"]
        for item_kind in kinds:
            object_id = self.object_name(chat_id, file_id, item_kind)
            blob = self.bucket.blob(object_id)
            blob.delete()

    def exists(self, *, chat_id: str, file_id: str, kind: str = "original") -> bool:
        object_id = self.object_name(chat_id, file_id, kind)
        blob = self.bucket.blob(object_id)
        return blob.exists()


_storage: GCSStorage | None = None


def get_storage() -> GCSStorage:
    """Return a lazily-created singleton GCS adapter."""
    global _storage
    if _storage is None:
        _storage = GCSStorage()
    return _storage
