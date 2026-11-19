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
from app.models import ChatFile, ChatMessage, TokenEntry


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


def migrate_chat_files(db, batch_size: int = 100) -> tuple[int, int]:
    changed = 0
    failed = 0
    offset = 0
    while True:
        rows = db.scalars(
            select(ChatFile).order_by(ChatFile.file_id).offset(offset).limit(batch_size)
        ).all()
        if not rows:
            break
        offset += batch_size
        for row in rows:
            try:
                encrypted = False
                if not is_encrypted(row.filename):
                    row.filename = encrypt_text(
                        row.filename,
                        aad=f"chat-file:{row.file_id}:filename",
                    )
                    encrypted = True
                if not is_encrypted(row.masked_csv):
                    row.masked_csv = encrypt_text(
                        row.masked_csv,
                        aad=f"chat-file:{row.file_id}:masked_csv",
                    )
                    encrypted = True
                if not is_encrypted(row.columns_json):
                    row.columns_json = encrypt_text(
                        row.columns_json,
                        aad=f"chat-file:{row.file_id}:columns",
                    )
                    encrypted = True
                if row.privacy_policy_json and not is_encrypted(row.privacy_policy_json):
                    row.privacy_policy_json = encrypt_text(
                        row.privacy_policy_json,
                        aad=f"chat-file:{row.file_id}:privacy",
                    )
                    encrypted = True
                if encrypted:
                    changed += 1
                    decrypt_text(row.filename, aad=f"chat-file:{row.file_id}:filename")
                    decrypt_text(row.masked_csv, aad=f"chat-file:{row.file_id}:masked_csv")
            except Exception:
                db.rollback()
                failed += 1
        db.commit()
    return changed, failed


def main() -> None:
    with SessionLocal() as db:
        token_changes = migrate_token_entries(db)
        message_changes = migrate_messages(db)
        file_changes, file_failures = migrate_chat_files(db)
        db.commit()

    print(
        "Phase 4 encryption migration complete: "
        f"token_changes={token_changes}, message_changes={message_changes}, "
        f"file_changes={file_changes}, file_failures={file_failures}"
    )


if __name__ == "__main__":
    main()
