"""Centralized envelope key provider abstraction."""

from __future__ import annotations

import base64
import binascii
import os
from typing import Protocol

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class KeyManagementError(RuntimeError):
    pass


class KeyProvider(Protocol):
    key_id: str

    def wrap_dek(self, dek: bytes) -> tuple[bytes, str]:
        ...

    def unwrap_dek(self, wrapped_dek: bytes, key_id: str) -> bytes:
        ...


class LocalDevelopmentKeyProvider:
    key_id = "local-development"

    def __init__(self) -> None:
        encoded = os.getenv("PRIVY_ENCRYPTION_KEY", "").strip()
        if not encoded:
            raise KeyManagementError(
                "PRIVY_ENCRYPTION_KEY is required outside production."
            )
        try:
            raw = base64.urlsafe_b64decode(encoded.encode("ascii"))
        except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
            raise KeyManagementError("PRIVY_ENCRYPTION_KEY is invalid.") from exc
        if len(raw) != 32:
            raise KeyManagementError(
                "PRIVY_ENCRYPTION_KEY must decode to exactly 32 bytes."
            )
        self._key = raw

    def wrap_dek(self, dek: bytes) -> tuple[bytes, str]:
        nonce = os.urandom(12)
        ciphertext = AESGCM(self._key).encrypt(nonce, dek, b"privy-dek-wrap")
        return nonce + ciphertext, self.key_id

    def unwrap_dek(self, wrapped_dek: bytes, key_id: str) -> bytes:
        if key_id != self.key_id or len(wrapped_dek) < 28:
            raise KeyManagementError("Invalid local wrapped DEK metadata.")
        try:
            return AESGCM(self._key).decrypt(
                wrapped_dek[:12],
                wrapped_dek[12:],
                b"privy-dek-wrap",
            )
        except Exception as exc:
            raise KeyManagementError("Wrapped DEK authentication failed.") from exc


class CloudKMSKeyProvider:
    def __init__(self) -> None:
        try:
            from google.cloud import kms
        except Exception as exc:
            raise KeyManagementError(
                "Google Cloud KMS client is unavailable."
            ) from exc

        resource = os.getenv("PRIVY_KMS_KEY_NAME", "").strip()
        if not resource:
            raise KeyManagementError(
                "PRIVY_KMS_KEY_NAME is required in production."
            )
        self.key_id = resource
        self._client = kms.KeyManagementServiceClient()

    def wrap_dek(self, dek: bytes) -> tuple[bytes, str]:
        try:
            response = self._client.encrypt(
                request={
                    "name": self.key_id,
                    "plaintext": dek,
                }
            )
            return bytes(response.ciphertext), str(response.name)
        except Exception as exc:
            raise KeyManagementError("Cloud KMS is unavailable for encryption.") from exc

    def unwrap_dek(self, wrapped_dek: bytes, key_id: str) -> bytes:
        if not key_id:
            raise KeyManagementError("Missing KMS key identifier.")
        try:
            response = self._client.decrypt(
                request={
                    "name": key_id,
                    "ciphertext": wrapped_dek,
                }
            )
            return bytes(response.plaintext)
        except Exception as exc:
            raise KeyManagementError("Cloud KMS is unavailable for decryption.") from exc


def get_key_provider() -> KeyProvider:
    environment = os.getenv("PRIVY_ENVIRONMENT", "development").strip().lower()
    if environment in {"production", "prod"}:
        return CloudKMSKeyProvider()
    return LocalDevelopmentKeyProvider()
