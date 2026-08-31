"""PostgreSQL-backed persistence for Privy.

The public function names intentionally stay compatible with the previous
SQLite implementation so routers do not have to change in this step.
"""

import os
import json
import re
import time
import uuid
import hashlib
import secrets

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from .db import SessionLocal
from .models import AdminConfig, Chat, ChatFile, ChatMessage, GuestSession, TokenEntry, User


def init_db() -> None:
    """Database initialization is handled by Alembic.

    The function remains for compatibility with main.py startup hooks. It only
    verifies that a connection can be established; it does not auto-create or
    mutate the production schema.
    """
    with SessionLocal() as db:
        db.execute(select(func.now()))


def get_admin_config(config_key: str, default: str = "") -> str:
    with SessionLocal() as db:
        value = db.scalar(select(AdminConfig.config_value).where(AdminConfig.config_key == config_key))
        return value if value is not None else default


def set_admin_config(config_key: str, config_value: str) -> None:
    with SessionLocal() as db:
        row = db.get(AdminConfig, config_key)
        if row:
            row.config_value = config_value
        else:
            db.add(AdminConfig(config_key=config_key, config_value=config_value))
        db.commit()


def get_or_create_user(
    auth0_sub: str,
    email: str | None = None,
    display_name: str | None = None,
) -> dict:
    now = time.time()
    with SessionLocal() as db:
        row = db.get(User, auth0_sub)
        if row:
            if email:
                row.email = email
            if display_name:
                row.display_name = display_name
            row.last_login_at = now
        else:
            user_count = db.scalar(
                select(func.count())
                .select_from(User)
                .where(User.role != "guest")
            ) or 0
            row = User(
                auth0_sub=auth0_sub,
                email=email,
                display_name=display_name,
                role="admin" if user_count == 0 else "user",
                created_at=now,
                last_login_at=now,
            )
            db.add(row)
            db.flush()

        # Claim any legacy orphaned chats once, preserving existing data.
        db.execute(
            update(Chat)
            .where(Chat.user_id.is_(None))
            .values(user_id=auth0_sub)
        )
        db.commit()

        return {
            "auth0_sub": row.auth0_sub,
            "email": row.email,
            "display_name": row.display_name,
            "role": row.role,
            "created_at": row.created_at,
            "last_login_at": row.last_login_at,
        }


# --- Guest sessions -------------------------------------------------------

GUEST_SESSION_TTL_SECONDS = int(os.getenv("PRIVY_GUEST_SESSION_TTL_SECONDS", "3600"))


def _guest_session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _user_dict(row: User) -> dict:
    return {
        "auth0_sub": row.auth0_sub,
        "email": row.email,
        "display_name": row.display_name,
        "role": row.role,
        "created_at": row.created_at,
        "last_login_at": row.last_login_at,
    }


def create_guest_session(ttl_seconds: int = GUEST_SESSION_TTL_SECONDS) -> tuple[str, dict, float]:
    now = time.time()
    expires_at = now + max(300, ttl_seconds)
    session_id = secrets.token_urlsafe(32)
    guest_sub = f"guest:{uuid.uuid4()}"
    with SessionLocal() as db:
        # Remove expired guest sessions/users first.
        expired = db.scalars(select(GuestSession).where(GuestSession.expires_at <= now)).all()
        for session in expired:
            guest_user = db.get(User, session.user_auth0_sub)
            if guest_user:
                db.delete(guest_user)
            db.delete(session)

        guest = User(
            auth0_sub=guest_sub,
            email=None,
            display_name="Guest",
            role="guest",
            created_at=now,
            last_login_at=now,
        )
        db.add(guest)
        db.flush()
        db.add(GuestSession(
            session_hash=_guest_session_hash(session_id),
            user_auth0_sub=guest_sub,
            created_at=now,
            expires_at=expires_at,
        ))
        db.commit()
        return session_id, _user_dict(guest), expires_at


def get_guest_user(session_id: str) -> dict | None:
    now = time.time()
    session_hash = _guest_session_hash(session_id)
    with SessionLocal() as db:
        session = db.get(GuestSession, session_hash)
        if not session:
            return None
        if session.expires_at <= now:
            guest = db.get(User, session.user_auth0_sub)
            if guest:
                db.delete(guest)
            db.delete(session)
            db.commit()
            return None
        user = db.get(User, session.user_auth0_sub)
        return _user_dict(user) if user and user.role == "guest" else None


def count_user_chats(user_id: str) -> int:
    with SessionLocal() as db:
        return int(db.scalar(select(func.count()).select_from(Chat).where(Chat.user_id == user_id)) or 0)


def count_user_files(user_id: str) -> int:
    with SessionLocal() as db:
        return int(
            db.scalar(
                select(func.count())
                .select_from(ChatFile)
                .join(Chat, Chat.chat_id == ChatFile.chat_id)
                .where(Chat.user_id == user_id)
            )
            or 0
        )


def count_user_questions(user_id: str) -> int:
    with SessionLocal() as db:
        return int(
            db.scalar(
                select(func.count())
                .select_from(ChatMessage)
                .join(Chat, Chat.chat_id == ChatMessage.chat_id)
                .where(Chat.user_id == user_id, ChatMessage.role == "user")
            )
            or 0
        )


def get_user(auth0_sub: str) -> dict | None:
    with SessionLocal() as db:
        row = db.get(User, auth0_sub)
        if not row:
            return None
        return {
            "auth0_sub": row.auth0_sub,
            "email": row.email,
            "display_name": row.display_name,
            "role": row.role,
            "created_at": row.created_at,
            "last_login_at": row.last_login_at,
        }


def create_chat(user_id: str, title: str = "New chat") -> str:
    chat_id = str(uuid.uuid4())
    now = time.time()
    with SessionLocal() as db:
        db.add(Chat(chat_id=chat_id, user_id=user_id, title=title, created_at=now, updated_at=now))
        db.commit()
    return chat_id


def list_chats(user_id: str) -> list:
    with SessionLocal() as db:
        rows = db.scalars(
            select(Chat)
            .where(Chat.user_id == user_id)
            .order_by(Chat.updated_at.desc(), Chat.chat_id.desc())
        ).all()
        return [
            {"chat_id": r.chat_id, "title": r.title, "created_at": r.created_at, "updated_at": r.updated_at}
            for r in rows
        ]


def get_chat(chat_id: str, user_id: str) -> dict | None:
    with SessionLocal() as db:
        row = db.scalar(select(Chat).where(Chat.chat_id == chat_id, Chat.user_id == user_id))
        if not row:
            return None
        return {"chat_id": row.chat_id, "title": row.title, "created_at": row.created_at, "updated_at": row.updated_at}


def get_chat_messages(chat_id: str) -> list:
    with SessionLocal() as db:
        rows = db.scalars(
            select(ChatMessage).where(ChatMessage.chat_id == chat_id).order_by(ChatMessage.id.asc())
        ).all()
        out = []
        for r in rows:
            try:
                metadata = json.loads(r.metadata_json) if r.metadata_json else {}
            except (TypeError, ValueError):
                metadata = {}
            out.append({"role": r.role, "content": r.content, "masked_count": r.masked_count, "metadata": metadata})
        return out


def add_message(chat_id: str, role: str, content: str, masked_count: int = 0, metadata: dict | None = None) -> None:
    now = time.time()
    with SessionLocal() as db:
        db.add(ChatMessage(chat_id=chat_id, role=role, content=content, masked_count=masked_count, metadata_json=json.dumps(metadata or {}, ensure_ascii=True), created_at=now))
        db.execute(update(Chat).where(Chat.chat_id == chat_id).values(updated_at=now))
        db.commit()


def rename_chat(chat_id: str, user_id: str, title: str) -> None:
    with SessionLocal() as db:
        db.execute(
            update(Chat)
            .where(Chat.chat_id == chat_id, Chat.user_id == user_id)
            .values(title=title, updated_at=time.time())
        )
        db.commit()


def delete_chat(chat_id: str, user_id: str) -> None:
    with SessionLocal() as db:
        row = db.scalar(select(Chat).where(Chat.chat_id == chat_id, Chat.user_id == user_id))
        if row:
            db.delete(row)
            db.commit()


def set_chat_file(
    chat_id: str,
    filename: str,
    masked_csv: str,
    columns_json: str,
    row_count: int,
    truncated: bool,
    masked_count: int = 0,
    file_id: str | None = None,
) -> str:
    """Create or replace one file attachment for a chat.

    If file_id is omitted, a new UUID is created. This preserves the existing
    upload flow while allowing many ChatFile rows per chat.
    """
    now = time.time()
    resolved_file_id = file_id or str(uuid.uuid4())
    with SessionLocal() as db:
        row = db.get(ChatFile, resolved_file_id)
        if row:
            row.filename = filename
            row.masked_csv = masked_csv
            row.columns_json = columns_json
            row.row_count = row_count
            row.truncated = truncated
            row.masked_count = masked_count
            row.updated_at = now
        else:
            db.add(
                ChatFile(
                    file_id=resolved_file_id,
                    chat_id=chat_id,
                    filename=filename,
                    masked_csv=masked_csv,
                    columns_json=columns_json,
                    row_count=row_count,
                    truncated=truncated,
                    masked_count=masked_count,
                    created_at=now,
                    updated_at=now,
                )
            )
        db.commit()
    return resolved_file_id


def get_chat_files(chat_id: str) -> list[dict]:
    with SessionLocal() as db:
        rows = db.scalars(
            select(ChatFile)
            .where(ChatFile.chat_id == chat_id)
            .order_by(ChatFile.created_at.asc(), ChatFile.file_id.asc())
        ).all()
        return [
            {
                "file_id": row.file_id,
                "filename": row.filename,
                "masked_csv": row.masked_csv,
                "columns_json": row.columns_json,
                "row_count": row.row_count,
                "truncated": bool(row.truncated),
                "masked_count": row.masked_count,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]


def get_chat_file(chat_id: str, file_id: str | None = None):
    with SessionLocal() as db:
        if file_id:
            row = db.scalar(
                select(ChatFile).where(
                    ChatFile.chat_id == chat_id,
                    ChatFile.file_id == file_id,
                )
            )
        else:
            row = db.scalar(
                select(ChatFile)
                .where(ChatFile.chat_id == chat_id)
                .order_by(ChatFile.created_at.asc(), ChatFile.file_id.asc())
                .limit(1)
            )
        if not row:
            return None
        return {
            "file_id": row.file_id,
            "filename": row.filename,
            "masked_csv": row.masked_csv,
            "columns_json": row.columns_json,
            "row_count": row.row_count,
            "truncated": bool(row.truncated),
            "masked_count": row.masked_count,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


def delete_chat_file(chat_id: str, file_id: str) -> bool:
    with SessionLocal() as db:
        row = db.scalar(
            select(ChatFile).where(
                ChatFile.chat_id == chat_id,
                ChatFile.file_id == file_id,
            )
        )
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True


def get_or_create_token(session_id: str, value_type: str, original_value: str, counters: dict) -> str:
    value_norm = str(original_value).strip().lower()
    with SessionLocal() as db:
        row = db.scalar(
            select(TokenEntry.token).where(
                TokenEntry.session_id == session_id,
                TokenEntry.value_norm == value_norm,
            )
        )
        if row:
            return row

        counters[value_type] = counters.get(value_type, 0) + 1
        token = f"[{value_type}_{counters[value_type]}]"
        try:
            db.add(
                TokenEntry(
                    session_id=session_id,
                    value_norm=value_norm,
                    token=token,
                    original=str(original_value),
                    value_type=value_type,
                )
            )
            db.commit()
            return token
        except IntegrityError:
            # Handles concurrent requests trying to mint the same value.
            db.rollback()
            existing = db.scalar(
                select(TokenEntry.token).where(
                    TokenEntry.session_id == session_id,
                    TokenEntry.value_norm == value_norm,
                )
            )
            if existing:
                return existing
            raise


def load_counters(session_id: str) -> dict:
    counters = {}
    with SessionLocal() as db:
        tokens = db.scalars(select(TokenEntry.token).where(TokenEntry.session_id == session_id)).all()
    for token in tokens:
        m = re.match(r"^\[(.+)_(\d+)\]$", token)
        if not m:
            continue
        value_type, num = m.group(1), int(m.group(2))
        counters[value_type] = max(counters.get(value_type, 0), num)
    return counters


def get_known_values(session_id: str) -> dict:
    with SessionLocal() as db:
        rows = db.execute(
            select(TokenEntry.value_norm, TokenEntry.token).where(TokenEntry.session_id == session_id)
        ).all()
    return {value_norm: token for value_norm, token in rows}


def get_reverse_map(session_id: str) -> dict:
    with SessionLocal() as db:
        rows = db.execute(
            select(TokenEntry.token, TokenEntry.original).where(TokenEntry.session_id == session_id)
        ).all()
    return {token: original for token, original in rows}


def clear_session(session_id: str) -> None:
    with SessionLocal() as db:
        db.execute(delete(TokenEntry).where(TokenEntry.session_id == session_id))
        db.commit()


def session_entry_count(session_id: str) -> int:
    with SessionLocal() as db:
        return int(
            db.scalar(select(func.count()).select_from(TokenEntry).where(TokenEntry.session_id == session_id)) or 0
        )
