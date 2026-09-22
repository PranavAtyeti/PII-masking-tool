"""Application-level encryption helpers for Privy.

Privy stores two classes of sensitive data in PostgreSQL:
- token mappings back to the original sensitive values;
- chat message content, which may contain user-supplied or unmasked values.

This module uses AES-256-GCM for confidentiality + integrity and HMAC-SHA256
as a keyed blind index for token lookup. The encryption key is supplied through
PRIVY_ENCRYPTION_KEY and must be a URL-safe base64 encoded 32-byte key.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
from typing import Final

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_CIPHERTEXT_PREFIX: Final[str] = "v1"
_KEY_ENV = "PRIVY_ENCRYPTION_KEY"
_NONCE_BYTES: Final[int] = 12
_KEY_BYTES: Final[int] = 32


class EncryptionConfigurationError(RuntimeError):
    """Raised when Privy's application encryption key is missing/invalid."""


def _load_key() -> bytes:
    raw = os.environ.get(_KEY_ENV, "").strip()
    if not raw:
        raise EncryptionConfigurationError(
            f"{_KEY_ENV} is not configured. Generate a 32-byte base64 key "
            "and place it in backend/.env."
        )

    # Accept standard URL-safe base64 output from the setup command.
    padded = raw + "=" * (-len(raw) % 4)
    try:
        key = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, binascii.Error) as exc:
        raise EncryptionConfigurationError(
            f"{_KEY_ENV} is not valid URL-safe base64."
        ) from exc

    if len(key) != _KEY_BYTES:
        raise EncryptionConfigurationError(
            f"{_KEY_ENV} must decode to exactly {_KEY_BYTES} bytes."
        )
    return key


def generate_key() -> str:
    """Generate a new 256-bit URL-safe base64 key."""
    key = AESGCM.generate_key(bit_length=256)
    return base64.urlsafe_b64encode(key).decode("ascii")


def _aad_bytes(aad: str | None) -> bytes | None:
    return aad.encode("utf-8") if aad is not None else None


def encrypt_text(value: str, *, aad: str | None = None) -> str:
    """Encrypt text and return a versioned ASCII-safe representation."""
    if value is None:
        raise TypeError("encrypt_text() does not accept None")

    key = _load_key()
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(
        nonce,
        str(value).encode("utf-8"),
        _aad_bytes(aad),
    )
    encoded_nonce = base64.urlsafe_b64encode(nonce).decode("ascii")
    encoded_ciphertext = base64.urlsafe_b64encode(ciphertext).decode("ascii")
    return f"{_CIPHERTEXT_PREFIX}:{encoded_nonce}:{encoded_ciphertext}"


def is_encrypted(value: str | None) -> bool:
    return isinstance(value, str) and value.startswith(f"{_CIPHERTEXT_PREFIX}:")


def decrypt_text(value: str, *, aad: str | None = None) -> str:
    """Decrypt a Privy ciphertext.

    Legacy plaintext values are intentionally returned unchanged. This keeps
    old rows readable until the one-time Phase 4 migration is run.
    """
    if not is_encrypted(value):
        return value

    parts = value.split(":", 2)
    if len(parts) != 3 or parts[0] != _CIPHERTEXT_PREFIX:
        raise EncryptionConfigurationError("Invalid Privy ciphertext format")

    try:
        nonce = base64.urlsafe_b64decode(parts[1].encode("ascii"))
        ciphertext = base64.urlsafe_b64decode(parts[2].encode("ascii"))
    except (ValueError, binascii.Error) as exc:
        raise EncryptionConfigurationError("Invalid Privy ciphertext encoding") from exc

    if len(nonce) != _NONCE_BYTES:
        raise EncryptionConfigurationError("Invalid Privy ciphertext nonce")

    try:
        plaintext = AESGCM(_load_key()).decrypt(
            nonce,
            ciphertext,
            _aad_bytes(aad),
        )
    except Exception as exc:
        # Do not expose authentication/tag details to callers.
        raise EncryptionConfigurationError(
            "Privy could not decrypt stored application data. Check the encryption key."
        ) from exc

    return plaintext.decode("utf-8")


def lookup_digest(session_id: str, value_norm: str) -> str:
    """Create a keyed blind index for a normalized token value."""
    key = _load_key()
    message = f"{session_id}\x1f{value_norm}".encode("utf-8")
    return hmac.new(key, message, hashlib.sha256).hexdigest()
