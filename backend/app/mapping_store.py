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
from collections.abc import Iterable

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from .db import SessionLocal
from .models import (
    AdminConfig,
    AuthSession,
    Chat,
    ChatFile,
    ChatMessage,
    GuestSession,
    TokenEntry,
    User,
)


# Keep ``IN (...)`` queries comfortably below driver and database parameter
# limits when a large worksheet contains many distinct values.
TOKEN_QUERY_CHUNK_SIZE = 1_000


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
        "email_verified": bool(row.email_verified),
    }


# --- Local authentication ---------------------------------------------------

AUTH_SESSION_TTL_SECONDS = int(
    os.getenv("PRIVY_AUTH_SESSION_TTL_SECONDS", "604800")
)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def get_local_user_by_email(email: str) -> dict | None:
    """Fetch a local account by normalized email.

    Legacy Auth0 accounts and guest accounts are intentionally excluded from
    local-password login so the Auth0 -> local-auth transition is explicit.
    """
    normalized = normalize_email(email)

    with SessionLocal() as db:
        row = db.scalar(
            select(User)
            .where(
                func.lower(User.email) == normalized,
                User.auth0_sub.like("local:%"),
                User.role != "guest",
            )
            .order_by(User.created_at.asc())
        )

        if not row:
            return None

        result = _user_dict(row)
        result["password_hash"] = row.password_hash
        return result


def get_local_user(user_id: str) -> dict | None:
    with SessionLocal() as db:
        row = db.get(User, user_id)
        if not row or not row.auth0_sub.startswith("local:"):
            return None
        return _user_dict(row)


def create_local_user(
    email: str,
    password_hash: str,
    display_name: str | None = None,
) -> dict:
    normalized_email = normalize_email(email)
    if not normalized_email:
        raise ValueError("Email is required")

    now = time.time()

    with SessionLocal() as db:
        existing = db.scalar(
            select(User).where(func.lower(User.email) == normalized_email)
        )
        if existing and existing.role != "guest":
            raise ValueError("An account with that email already exists")

        local_user_count = db.scalar(
            select(func.count())
            .select_from(User)
            .where(
                User.auth0_sub.like("local:%"),
                User.role != "guest",
            )
        ) or 0

        row = User(
            auth0_sub=f"local:{uuid.uuid4()}",
            email=normalized_email,
            display_name=(display_name or "").strip() or None,
            role="admin" if local_user_count == 0 else "user",
            created_at=now,
            last_login_at=now,
            password_hash=password_hash,
            email_verified=False,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return _user_dict(row)


def update_last_login(user_id: str) -> dict | None:
    now = time.time()
    with SessionLocal() as db:
        row = db.get(User, user_id)
        if not row:
            return None
        row.last_login_at = now
        db.commit()
        db.refresh(row)
        return _user_dict(row)


def create_auth_session(
    user_id: str,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[str, float]:
    """Create a refresh session and store only the token hash."""
    now = time.time()
    expires_at = now + max(3600, AUTH_SESSION_TTL_SECONDS)
    refresh_token = secrets.token_urlsafe(48)
    refresh_token_hash = hashlib.sha256(
        refresh_token.encode("utf-8")
    ).hexdigest()

    row = AuthSession(
        id=uuid.uuid4().hex,
        user_auth0_sub=user_id,
        refresh_token_hash=refresh_token_hash,
        created_at=now,
        expires_at=expires_at,
        last_used_at=now,
        revoked_at=None,
        user_agent=(user_agent or "")[:512] or None,
        ip_address=(ip_address or "")[:64] or None,
    )

    with SessionLocal() as db:
        db.add(row)
        db.commit()

    return refresh_token, expires_at


def rotate_auth_session(
    refresh_token: str,
) -> tuple[dict, str, float] | None:
    """Validate and atomically rotate a refresh token."""
    if not refresh_token:
        return None

    token_hash = hashlib.sha256(
        refresh_token.encode("utf-8")
    ).hexdigest()
    now = time.time()

    with SessionLocal() as db:
        row = db.scalar(
            select(AuthSession)
            .where(AuthSession.refresh_token_hash == token_hash)
            .with_for_update()
        )

        if not row or row.revoked_at is not None:
            return None

        if row.expires_at <= now:
            row.revoked_at = now
            db.commit()
            return None

        user = db.get(User, row.user_auth0_sub)
        if not user or user.role == "guest" or user.password_hash is None:
            row.revoked_at = now
            db.commit()
            return None

        new_refresh_token = secrets.token_urlsafe(48)
        new_hash = hashlib.sha256(
            new_refresh_token.encode("utf-8")
        ).hexdigest()

        row.refresh_token_hash = new_hash
        row.last_used_at = now
        db.commit()

        return _user_dict(user), new_refresh_token, row.expires_at


def revoke_auth_session(refresh_token: str | None) -> None:
    if not refresh_token:
        return

    token_hash = hashlib.sha256(
        refresh_token.encode("utf-8")
    ).hexdigest()

    with SessionLocal() as db:
        row = db.scalar(
            select(AuthSession).where(
                AuthSession.refresh_token_hash == token_hash
            )
        )
        if row and row.revoked_at is None:
            row.revoked_at = time.time()
            db.commit()


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
            "email_verified": bool(row.email_verified),
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


def get_or_create_tokens(
    session_id: str,
    values: Iterable[tuple[str, str]],
    counters: dict,
) -> dict[str, str]:
    """Get or mint tokens for many values in one database transaction.

    ``TokenEntry`` is unique per normalized value within a chat, so repeated
    spreadsheet values need one lookup and one token, regardless of how many
    cells contain them. Keeping this operation set-based avoids a PostgreSQL
    round trip and commit for every masked cell.
    """
    requested: dict[str, tuple[str, str]] = {}
    for value_type, original_value in values:
        value_norm = str(original_value).strip().lower()
        if value_norm and value_norm not in requested:
            requested[value_norm] = (value_type, str(original_value))

    if not requested:
        return {}

    with SessionLocal() as db:
        tokens = _get_existing_tokens(db, session_id, requested)

        new_entries: list[TokenEntry] = []
        for value_norm, (value_type, original_value) in requested.items():
            if value_norm in tokens:
                continue
            counters[value_type] = counters.get(value_type, 0) + 1
            token = f"[{value_type}_{counters[value_type]}]"
            tokens[value_norm] = token
            new_entries.append(
                TokenEntry(
                    session_id=session_id,
                    value_norm=value_norm,
                    token=token,
                    original=original_value,
                    value_type=value_type,
                )
            )

        if not new_entries:
            return tokens

        try:
            db.add_all(new_entries)
            db.commit()
        except IntegrityError:
            # A concurrent request may have minted one of these values first.
            # Roll back, fetch the now-authoritative mapping, and only retry
            # values that are genuinely still absent.
            db.rollback()
            tokens.update(_get_existing_tokens(db, session_id, requested))
            unresolved = [entry for entry in new_entries if entry.value_norm not in tokens]
            if unresolved:
                db.add_all(unresolved)
                db.commit()
                tokens.update({entry.value_norm: entry.token for entry in unresolved})

        return tokens


def _get_existing_tokens(db, session_id: str, value_norms) -> dict[str, str]:
    """Fetch existing mappings in bounded query batches."""
    value_norms = list(value_norms)
    tokens: dict[str, str] = {}
    for start in range(0, len(value_norms), TOKEN_QUERY_CHUNK_SIZE):
        chunk = value_norms[start:start + TOKEN_QUERY_CHUNK_SIZE]
        rows = db.execute(
            select(TokenEntry.value_norm, TokenEntry.token).where(
                TokenEntry.session_id == session_id,
                TokenEntry.value_norm.in_(chunk),
            )
        ).all()
        tokens.update({value_norm: token for value_norm, token in rows})
    return tokens


def get_or_create_token(session_id: str, value_type: str, original_value: str, counters: dict) -> str:
    """Single-value compatibility wrapper for chat masking call sites."""
    value_norm = str(original_value).strip().lower()
    return get_or_create_tokens(
        session_id,
        [(value_type, str(original_value))],
        counters,
    )[value_norm]


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
