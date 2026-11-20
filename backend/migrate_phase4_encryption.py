"""One-time migration for Privy Phase 4 application-level encryption.

Run from the backend directory after setting PRIVY_ENCRYPTION_KEY in backend/.env:

    python migrate_phase4_encryption.py

The migration converts:
- token_entries.value_norm plaintext -> keyed HMAC blind index
- token_entries.original and chat_messages.content -> encrypted values
- stored chat-file data, metadata, policies, filenames and chat titles

The operation is explicit, keyset-batched, verified before commit and resumable.
"""

from __future__ import annotations

from sqlalchemy import and_, or_, select

from app.crypto import decrypt_text, encrypt_text, is_encrypted, lookup_digest
from app.db import SessionLocal
from app.models import Chat, ChatFile, ChatMessage, TokenEntry


def migrate_token_entries(db, batch_size: int = 500) -> tuple[int, int]:
    changed = 0
    failed = 0
    last_key: tuple[str, str] | None = None
    while True:
        query = select(TokenEntry).order_by(TokenEntry.session_id, TokenEntry.token).limit(batch_size)
        if last_key:
            query = query.where(
                or_(
                    TokenEntry.session_id > last_key[0],
                    and_(
                        TokenEntry.session_id == last_key[0],
                        TokenEntry.token > last_key[1],
                    ),
                )
            )
        rows = db.scalars(query).all()
        if not rows:
            break
        last_key = (rows[-1].session_id, rows[-1].token)
        for row in rows:
            needs_change = False
            try:
                with db.begin_nested():
                    original_plain = decrypt_text(
                        row.original,
                        aad=f"token:{row.session_id}:{row.value_type}",
                    )
                    expected_blind_index = lookup_digest(
                        row.session_id,
                        str(original_plain).strip().lower(),
                    )
                    needs_change = row.value_norm != expected_blind_index or not is_encrypted(row.original)
                    if row.value_norm != expected_blind_index:
                        row.value_norm = expected_blind_index
                    if not is_encrypted(row.original):
                        row.original = encrypt_text(
                            original_plain,
                            aad=f"token:{row.session_id}:{row.value_type}",
                        )
                    if needs_change:
                        if decrypt_text(
                            row.original,
                            aad=f"token:{row.session_id}:{row.value_type}",
                        ) != original_plain:
                            raise ValueError("Token mapping encryption verification failed")
                if needs_change:
                    changed += 1
            except Exception:
                failed += 1
        db.commit()
    return changed, failed


def migrate_messages(db, batch_size: int = 500) -> tuple[int, int]:
    changed = 0
    failed = 0
    last_id = 0
    while True:
        rows = db.scalars(
            select(ChatMessage)
            .where(ChatMessage.id > last_id)
            .order_by(ChatMessage.id)
            .limit(batch_size)
        ).all()
        if not rows:
            break
        last_id = rows[-1].id
        for row in rows:
            row_changed = False
            try:
                with db.begin_nested():
                    if is_encrypted(row.content):
                        decrypt_text(
                            row.content,
                            aad=f"chat-message:{row.chat_id}:{row.role}",
                        )
                        continue
                    encrypted = encrypt_text(
                        row.content,
                        aad=f"chat-message:{row.chat_id}:{row.role}",
                    )
                    if decrypt_text(
                        encrypted,
                        aad=f"chat-message:{row.chat_id}:{row.role}",
                    ) != row.content:
                        raise ValueError("Chat message encryption verification failed")
                    row.content = encrypted
                    row_changed = True
                if row_changed:
                    changed += 1
            except Exception:
                failed += 1
        db.commit()
    return changed, failed


def migrate_chat_files(db, batch_size: int = 100) -> tuple[int, int]:
    changed = 0
    failed = 0
    last_file_id = ""
    while True:
        rows = db.scalars(
            select(ChatFile)
            .where(ChatFile.file_id > last_file_id)
            .order_by(ChatFile.file_id)
            .limit(batch_size)
        ).all()
        if not rows:
            break
        last_file_id = rows[-1].file_id
        for row in rows:
            row_changed = False
            try:
                with db.begin_nested():
                    fields = (
                        ("filename", "filename"),
                        ("masked_csv", "masked_csv"),
                        ("columns_json", "columns"),
                        ("privacy_policy_json", "privacy"),
                    )
                    row_changed = False
                    for attribute, aad_field in fields:
                        current = getattr(row, attribute)
                        if not current:
                            continue
                        aad = f"chat-file:{row.file_id}:{aad_field}"
                        if is_encrypted(current):
                            decrypt_text(current, aad=aad)
                            continue
                        encrypted = encrypt_text(current, aad=aad)
                        if decrypt_text(encrypted, aad=aad) != current:
                            raise ValueError("Chat file encryption verification failed")
                        setattr(row, attribute, encrypted)
                        row_changed = True
                if row_changed:
                    changed += 1
            except Exception:
                failed += 1
        db.commit()
    return changed, failed


def migrate_chat_titles(db, batch_size: int = 500) -> tuple[int, int]:
    changed = 0
    failed = 0
    last_chat_id = ""
    while True:
        rows = db.scalars(
            select(Chat)
            .where(Chat.chat_id > last_chat_id)
            .order_by(Chat.chat_id)
            .limit(batch_size)
        ).all()
        if not rows:
            break
        last_chat_id = rows[-1].chat_id
        for row in rows:
            row_changed = False
            try:
                with db.begin_nested():
                    aad = f"chat-title:{row.chat_id}"
                    if is_encrypted(row.title):
                        decrypt_text(row.title, aad=aad)
                    else:
                        encrypted = encrypt_text(row.title, aad=aad)
                        if decrypt_text(encrypted, aad=aad) != row.title:
                            raise ValueError("Chat title encryption verification failed")
                        row.title = encrypted
                        row_changed = True
                if row_changed:
                    changed += 1
            except Exception:
                failed += 1
        db.commit()
    return changed, failed


def main() -> None:
    with SessionLocal() as db:
        token_changes, token_failures = migrate_token_entries(db)
        message_changes, message_failures = migrate_messages(db)
        file_changes, file_failures = migrate_chat_files(db)
        title_changes, title_failures = migrate_chat_titles(db)
        db.commit()

    print(
        "Phase 4 encryption migration complete: "
        f"token_changes={token_changes}, token_failures={token_failures}, "
        f"message_changes={message_changes}, message_failures={message_failures}, "
        f"file_changes={file_changes}, file_failures={file_failures}, "
        f"title_changes={title_changes}, title_failures={title_failures}"
    )


if __name__ == "__main__":
    main()
