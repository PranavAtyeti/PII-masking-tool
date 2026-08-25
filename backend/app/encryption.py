"""
encryption.py
-------------
Application-level envelope-style encryption for Privy file payloads.

This first cloud-storage phase deliberately does NOT use Cloud KMS so local
and low-volume development can remain free. Files are encrypted with
AES-256-GCM before they are sent to Google Cloud Storage.

A single 32-byte application master key is read from PRIVY_ENCRYPTION_KEY.
Each stored object gets its own random salt and nonce; a per-object key is
then derived from the master key with HKDF-SHA256.

The resulting blob contains only:
    magic | version | salt | nonce | ciphertext+tag

No plaintext file content is written by this module.
"""

from __future__ import annotations

import base64
import binascii
import os
import struct
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives import hashes

MAGIC = b"PRIVYENC"
VERSION = 1
SALT_SIZE = 16
NONCE_SIZE = 12
KEY_SIZE = 32


class EncryptionConfigError(RuntimeError):
    """Raised when the application encryption key is missing or invalid."""


class EncryptionError(RuntimeError):
    """Raised for malformed or unauthentic encrypted payloads."""


@dataclass(frozen=True)
class EncryptedFile:
    payload: bytes
    version: int = VERSION


def _load_master_key() -> bytes:
    encoded = os.getenv("PRIVY_ENCRYPTION_KEY", "").strip()
    if not encoded:
        raise EncryptionConfigError(
            "PRIVY_ENCRYPTION_KEY is not configured. "
            "Generate a 32-byte key and put its base64 value in the backend .env."
        )

    try:
        key = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (ValueError, UnicodeEncodeError, binascii.Error) as exc:
        raise EncryptionConfigError(
            "PRIVY_ENCRYPTION_KEY must be a valid URL-safe base64 value."
        ) from exc

    if len(key) != KEY_SIZE:
        raise EncryptionConfigError(
            "PRIVY_ENCRYPTION_KEY must decode to exactly 32 bytes (256 bits)."
        )

    return key


def generate_master_key() -> str:
    """Generate a URL-safe base64 encoded 256-bit application key."""
    return base64.urlsafe_b64encode(os.urandom(KEY_SIZE)).decode("ascii")


def _derive_object_key(master_key: bytes, salt: bytes, object_id: str) -> bytes:
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_SIZE,
        salt=salt,
        info=f"privy-file:{object_id}".encode("utf-8"),
    ).derive(master_key)


def encrypt_bytes(data: bytes, object_id: str) -> EncryptedFile:
    """Encrypt bytes with AES-256-GCM and bind the object id as AAD."""
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("data must be bytes-like")
    if not object_id:
        raise ValueError("object_id is required")

    master_key = _load_master_key()
    salt = os.urandom(SALT_SIZE)
    nonce = os.urandom(NONCE_SIZE)
    key = _derive_object_key(master_key, salt, object_id)

    aad = f"privy|v{VERSION}|{object_id}".encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, bytes(data), aad)

    header = MAGIC + struct.pack("!B", VERSION) + salt + nonce
    return EncryptedFile(payload=header + ciphertext)


def decrypt_bytes(payload: bytes, object_id: str) -> bytes:
    """Decrypt and authenticate a stored encrypted blob."""
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("payload must be bytes-like")
    if not object_id:
        raise ValueError("object_id is required")

    raw = bytes(payload)
    minimum = len(MAGIC) + 1 + SALT_SIZE + NONCE_SIZE + 16  # GCM tag
    if len(raw) < minimum:
        raise EncryptionError("Encrypted payload is too short or malformed.")

    offset = 0
    if raw[offset : offset + len(MAGIC)] != MAGIC:
        raise EncryptionError("Encrypted payload has an invalid magic header.")
    offset += len(MAGIC)

    version = raw[offset]
    offset += 1
    if version != VERSION:
        raise EncryptionError(f"Unsupported encrypted payload version: {version}")

    salt = raw[offset : offset + SALT_SIZE]
    offset += SALT_SIZE
    nonce = raw[offset : offset + NONCE_SIZE]
    offset += NONCE_SIZE
    ciphertext = raw[offset:]

    master_key = _load_master_key()
    key = _derive_object_key(master_key, salt, object_id)
    aad = f"privy|v{version}|{object_id}".encode("utf-8")

    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except Exception as exc:  # cryptography raises a generic auth failure here
        raise EncryptionError(
            "Encrypted payload failed authentication or the wrong key was used."
        ) from exc
