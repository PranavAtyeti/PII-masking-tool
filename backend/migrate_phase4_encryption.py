"""One-time migration for Privy Phase 4 application-level encryption.

Run from the backend directory after setting PRIVY_ENCRYPTION_KEY in backend/.env:

    python migrate_phase4_encryption.py

The migration converts:
- token_entries.value_norm plaintext -> keyed HMAC blind index
- token_entries.original plaintext -> AES-256-GCM ciphertext
- chat_messages.content plaintext -> AES-256-GCM ciphertext

The operation is idempotent. It derives the expected blind index from the
stored original value, so it does not need to guess whether a 64-character
value_norm is already encrypted/blinded.
"""

from __future__ import annotations

from sqlalchemy import select

from app.crypto import decrypt_text, encrypt_text, is_encrypted, lookup_digest
from app.db import SessionLocal
from app.models import ChatMessage, TokenEntry


def migrate_token_entries(db) -> int:
    changed = 0
    rows = db.scalars(select(TokenEntry)).all()

    for row in rows:
        # decrypt_text() deliberately supports legacy plaintext, so this gives
        # us the original normalized value whether the row is old or new.
        original_plain = decrypt_text(
            row.original,
            aad=f"token:{row.session_id}:{row.value_type}",
        )

        expected_blind_index = lookup_digest(
            row.session_id,
            str(original_plain).strip().lower(),
        )

        if row.value_norm != expected_blind_index:
            row.value_norm = expected_blind_index
            changed += 1

        if not is_encrypted(row.original):
            row.original = encrypt_text(
                original_plain,
                aad=f"token:{row.session_id}:{row.value_type}",
            )
            changed += 1

    return changed


def migrate_messages(db) -> int:
    changed = 0
    rows = db.scalars(select(ChatMessage)).all()

    for row in rows:
        if is_encrypted(row.content):
            continue

        row.content = encrypt_text(
            row.content,
            aad=f"chat-message:{row.chat_id}:{row.role}",
        )
        changed += 1

    return changed


def main() -> None:
    with SessionLocal() as db:
        token_changes = migrate_token_entries(db)
        message_changes = migrate_messages(db)
        db.commit()

    print(
        "Phase 4 encryption migration complete: "
        f"token_changes={token_changes}, message_changes={message_changes}"
    )


if __name__ == "__main__":
    main()
