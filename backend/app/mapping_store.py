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
from .crypto import decrypt_text, encrypt_text, lookup_digest
from .models import (
    AdminConfig,
    AuthSession,
    Chat,
    ChatFile,
    ChatMessage,
    ConversationTurn,
    GuestSession,
    JoinConfirmation,
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
        db.add(
            Chat(
                chat_id=chat_id,
                user_id=user_id,
                title=encrypt_text(title, aad=f"chat-title:{chat_id}"),
                created_at=now,
                updated_at=now,
            )
        )
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
            {
                "chat_id": r.chat_id,
                "title": decrypt_text(r.title, aad=f"chat-title:{r.chat_id}"),
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in rows
        ]


def get_chat(chat_id: str, user_id: str) -> dict | None:
    with SessionLocal() as db:
        row = db.scalar(select(Chat).where(Chat.chat_id == chat_id, Chat.user_id == user_id))
        if not row:
            return None
        return {
            "chat_id": row.chat_id,
            "title": decrypt_text(row.title, aad=f"chat-title:{row.chat_id}"),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }


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

            content = decrypt_text(
                r.content,
                aad=f"chat-message:{r.chat_id}:{r.role}",
            )
            out.append({
                "role": r.role,
                "content": content,
                "masked_count": r.masked_count,
                "metadata": metadata,
            })
        return out


def _message_dict(row: ChatMessage) -> dict:
    try:
        metadata = json.loads(row.metadata_json) if row.metadata_json else {}
    except (TypeError, ValueError):
        metadata = {}
    return {
        "role": row.role,
        "content": decrypt_text(
            row.content,
            aad=f"chat-message:{row.chat_id}:{row.role}",
        ),
        "masked_count": row.masked_count,
        "metadata": metadata,
    }


def _history_before(db, chat_id: str, message_id: int) -> list[dict]:
    rows = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.chat_id == chat_id, ChatMessage.id < message_id)
        .order_by(ChatMessage.id.asc())
    ).all()
    return [_message_dict(row) for row in rows]


def begin_conversation_turn(
    *,
    chat_id: str,
    user_id: str,
    request_id: str,
    request_fingerprint: str,
    question: str,
    context_revision: str,
) -> dict:
    """Atomically claim a chat turn and persist its encrypted user message."""
    if not request_id or len(request_id) > 64:
        raise ValueError("A valid request id is required")
    now = time.time()
    try:
        with SessionLocal() as db:
            chat = db.scalar(
                select(Chat)
                .where(Chat.chat_id == chat_id, Chat.user_id == user_id)
                .with_for_update()
            )
            if chat is None:
                raise LookupError("Chat not found")

            stale_after = max(
                60,
                int(os.getenv("PRIVY_ACTIVE_TURN_TIMEOUT_SECONDS", "1800")),
            )
            stale_turns = db.scalars(
                select(ConversationTurn)
                .where(
                    ConversationTurn.chat_id == chat_id,
                    ConversationTurn.status == "in_progress",
                    ConversationTurn.updated_at <= now - stale_after,
                )
                .with_for_update()
            ).all()
            for stale_turn in stale_turns:
                stale_turn.status = "interrupted"
                stale_turn.updated_at = now

            turn = db.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.chat_id == chat_id,
                    ConversationTurn.request_id == request_id,
                )
            )
            if turn is not None:
                if turn.request_fingerprint != request_fingerprint:
                    return {"status": "conflict", "previous_messages": [], "is_first_message": False}
                if turn.status == "completed":
                    assistant = db.get(ChatMessage, turn.assistant_message_id) if turn.assistant_message_id else None
                    if assistant is None:
                        raise RuntimeError("Completed conversation turn has no assistant message.")
                    return {
                        "status": "completed",
                        "assistant": _message_dict(assistant),
                        "previous_messages": [],
                        "is_first_message": False,
                    }
                if turn.status == "in_progress":
                    return {"status": "busy", "previous_messages": [], "is_first_message": False}
                if turn.assistant_message_id is not None:
                    old_assistant = db.get(ChatMessage, turn.assistant_message_id)
                    if old_assistant is not None:
                        db.delete(old_assistant)
                    turn.assistant_message_id = None
                history = _history_before(db, chat_id, turn.user_message_id)
                turn.status = "in_progress"
                turn.context_revision = context_revision
                turn.provider = None
                turn.model = None
                turn.updated_at = now
                db.commit()
                return {
                    "status": "resumed",
                    "previous_messages": history,
                    "is_first_message": not history,
                }

            prior_rows = db.scalars(
                select(ChatMessage)
                .where(ChatMessage.chat_id == chat_id)
                .order_by(ChatMessage.id.asc())
            ).all()
            history = [_message_dict(row) for row in prior_rows]
            user_message = ChatMessage(
                chat_id=chat_id,
                role="user",
                content=encrypt_text(question, aad=f"chat-message:{chat_id}:user"),
                masked_count=0,
                metadata_json="{}",
                created_at=now,
            )
            db.add(user_message)
            db.flush()
            db.add(
                ConversationTurn(
                    chat_id=chat_id,
                    request_id=request_id,
                    request_fingerprint=request_fingerprint,
                    user_message_id=user_message.id,
                    status="in_progress",
                    context_revision=context_revision,
                    created_at=now,
                    updated_at=now,
                )
            )
            chat.updated_at = now
            db.commit()
            return {
                "status": "started",
                "previous_messages": history,
                "is_first_message": not history,
            }
    except IntegrityError:
        with SessionLocal() as db:
            same_request = db.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.chat_id == chat_id,
                    ConversationTurn.request_id == request_id,
                )
            )
            if same_request is not None:
                if same_request.request_fingerprint != request_fingerprint:
                    return {"status": "conflict", "previous_messages": [], "is_first_message": False}
                if same_request.status == "completed":
                    assistant = (
                        db.get(ChatMessage, same_request.assistant_message_id)
                        if same_request.assistant_message_id
                        else None
                    )
                    if assistant is None:
                        raise RuntimeError("Completed conversation turn has no assistant message.")
                    return {
                        "status": "completed",
                        "assistant": _message_dict(assistant),
                        "previous_messages": [],
                        "is_first_message": False,
                    }
            active_turn = db.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.chat_id == chat_id,
                    ConversationTurn.status == "in_progress",
                )
            )
            if active_turn is not None:
                return {"status": "busy", "previous_messages": [], "is_first_message": False}
        raise


def update_conversation_turn(
    chat_id: str,
    request_id: str,
    *,
    provider: str,
    model: str,
) -> None:
    with SessionLocal() as db:
        turn = db.scalar(
            select(ConversationTurn).where(
                ConversationTurn.chat_id == chat_id,
                ConversationTurn.request_id == request_id,
                ConversationTurn.status == "in_progress",
            )
        )
        if turn is None:
            raise RuntimeError("Conversation turn is no longer active.")
        turn.provider = provider
        turn.model = model
        turn.updated_at = time.time()
        db.commit()


def finish_conversation_turn(
    chat_id: str,
    request_id: str,
    content: str,
    masked_count: int,
    metadata: dict | None = None,
    *,
    status: str = "completed",
) -> None:
    if status not in {"completed", "failed"}:
        raise ValueError("Invalid completed turn status")
    now = time.time()
    with SessionLocal() as db:
        turn = db.scalar(
            select(ConversationTurn).where(
                ConversationTurn.chat_id == chat_id,
                ConversationTurn.request_id == request_id,
                ConversationTurn.status == "in_progress",
            )
        )
        if turn is None:
            raise RuntimeError("Conversation turn is no longer active.")
        assistant = ChatMessage(
            chat_id=chat_id,
            role="assistant",
            content=encrypt_text(content, aad=f"chat-message:{chat_id}:assistant"),
            masked_count=masked_count,
            metadata_json=json.dumps(metadata or {}, ensure_ascii=True),
            created_at=now,
        )
        db.add(assistant)
        db.flush()
        turn.assistant_message_id = assistant.id
        turn.status = status
        turn.updated_at = now
        db.execute(update(Chat).where(Chat.chat_id == chat_id).values(updated_at=now))
        db.commit()


def interrupt_conversation_turn(chat_id: str, request_id: str) -> None:
    """Make an interrupted request eligible for a same-idempotency-key retry."""
    with SessionLocal() as db:
        db.execute(
            update(ConversationTurn)
            .where(
                ConversationTurn.chat_id == chat_id,
                ConversationTurn.request_id == request_id,
                ConversationTurn.status == "in_progress",
            )
            .values(status="interrupted", updated_at=time.time())
        )
        db.commit()


def add_message(chat_id: str, role: str, content: str, masked_count: int = 0, metadata: dict | None = None) -> None:
    now = time.time()
    encrypted_content = encrypt_text(
        content,
        aad=f"chat-message:{chat_id}:{role}",
    )
    with SessionLocal() as db:
        db.add(
            ChatMessage(
                chat_id=chat_id,
                role=role,
                content=encrypted_content,
                masked_count=masked_count,
                metadata_json=json.dumps(metadata or {}, ensure_ascii=True),
                created_at=now,
            )
        )
        db.execute(update(Chat).where(Chat.chat_id == chat_id).values(updated_at=now))
        db.commit()


def rename_chat(chat_id: str, user_id: str, title: str) -> None:
    with SessionLocal() as db:
        db.execute(
            update(Chat)
            .where(Chat.chat_id == chat_id, Chat.user_id == user_id)
            .values(
                title=encrypt_text(title, aad=f"chat-title:{chat_id}"),
                updated_at=time.time(),
            )
        )
        db.commit()


def delete_chat(chat_id: str, user_id: str) -> None:
    with SessionLocal() as db:
        row = db.scalar(select(Chat).where(Chat.chat_id == chat_id, Chat.user_id == user_id))
        if row:
            db.execute(delete(TokenEntry).where(TokenEntry.session_id == chat_id))
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
    privacy_policy_json: str | None = None,
    workbook_json: str | None = None,
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
            if row.chat_id != chat_id:
                raise ValueError("File does not belong to the requested chat")
            row.filename = encrypt_text(filename, aad=f"chat-file:{resolved_file_id}:filename")
            row.masked_csv = encrypt_text(masked_csv, aad=f"chat-file:{resolved_file_id}:masked_csv")
            row.columns_json = encrypt_text(columns_json, aad=f"chat-file:{resolved_file_id}:columns")
            row.row_count = row_count
            row.truncated = truncated
            row.masked_count = masked_count
            row.privacy_policy_json = (
                encrypt_text(privacy_policy_json, aad=f"chat-file:{resolved_file_id}:privacy")
                if privacy_policy_json
                else None
            )
            row.workbook_json = (
                encrypt_text(workbook_json, aad=f"chat-file:{resolved_file_id}:workbook")
                if workbook_json
                else None
            )
            row.updated_at = now
        else:
            db.add(
                ChatFile(
                    file_id=resolved_file_id,
                    chat_id=chat_id,
                    filename=encrypt_text(filename, aad=f"chat-file:{resolved_file_id}:filename"),
                    masked_csv=encrypt_text(masked_csv, aad=f"chat-file:{resolved_file_id}:masked_csv"),
                    columns_json=encrypt_text(columns_json, aad=f"chat-file:{resolved_file_id}:columns"),
                    row_count=row_count,
                    truncated=truncated,
                    masked_count=masked_count,
                    privacy_policy_json=(
                        encrypt_text(privacy_policy_json, aad=f"chat-file:{resolved_file_id}:privacy")
                        if privacy_policy_json
                        else None
                    ),
                    workbook_json=(
                        encrypt_text(workbook_json, aad=f"chat-file:{resolved_file_id}:workbook")
                        if workbook_json
                        else None
                    ),
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
                "filename": decrypt_text(row.filename, aad=f"chat-file:{row.file_id}:filename"),
                "masked_csv": decrypt_text(row.masked_csv, aad=f"chat-file:{row.file_id}:masked_csv"),
                "columns_json": decrypt_text(row.columns_json, aad=f"chat-file:{row.file_id}:columns"),
                "privacy_policy_json": (
                    decrypt_text(row.privacy_policy_json, aad=f"chat-file:{row.file_id}:privacy")
                    if row.privacy_policy_json
                    else None
                ),
                "workbook_json": (
                    decrypt_text(row.workbook_json, aad=f"chat-file:{row.file_id}:workbook")
                    if row.workbook_json
                    else None
                ),
                "row_count": row.row_count,
                "truncated": bool(row.truncated),
                "masked_count": row.masked_count,
                "created_at": row.created_at,
                "updated_at": row.updated_at,
            }
            for row in rows
        ]


def get_chat_file_revisions(chat_id: str) -> dict[str, float]:
    """Return file revision timestamps without decrypting persisted file data."""
    with SessionLocal() as db:
        rows = db.execute(
            select(ChatFile.file_id, ChatFile.updated_at).where(ChatFile.chat_id == chat_id)
        ).all()
        return {str(file_id): float(updated_at) for file_id, updated_at in rows}


def create_join_confirmation(
    *, chat_id: str, user_id: str, candidate: dict, ttl_seconds: int = 300
) -> str:
    """Persist non-content consent state bound to one candidate and owner."""
    now = time.time()
    confirmation_id = str(uuid.uuid4())
    with SessionLocal() as db:
        chat = db.scalar(select(Chat).where(Chat.chat_id == chat_id, Chat.user_id == user_id))
        if chat is None:
            raise LookupError("Chat not found")
        db.execute(delete(JoinConfirmation).where(JoinConfirmation.expires_at <= now))
        db.add(JoinConfirmation(
            confirmation_id=confirmation_id,
            chat_id=chat_id,
            user_id=user_id,
            candidate_json=json.dumps(candidate, ensure_ascii=True, sort_keys=True, separators=(",", ":")),
            expires_at=now + min(max(ttl_seconds, 30), 600),
            confirmed_at=None,
            consumed_at=None,
            created_at=now,
        ))
        db.commit()
    return confirmation_id


def confirm_join_confirmation(*, chat_id: str, user_id: str, confirmation_id: str) -> bool:
    """Atomically approve a live candidate from its authorized user."""
    now = time.time()
    with SessionLocal() as db:
        result = db.execute(update(JoinConfirmation).where(
            JoinConfirmation.confirmation_id == confirmation_id,
            JoinConfirmation.chat_id == chat_id,
            JoinConfirmation.user_id == user_id,
            JoinConfirmation.expires_at > now,
            JoinConfirmation.confirmed_at.is_(None),
            JoinConfirmation.consumed_at.is_(None),
        ).values(confirmed_at=now))
        db.commit()
        return result.rowcount == 1


def consume_join_confirmation(
    *, chat_id: str, user_id: str, confirmation_id: str
) -> dict | None:
    """Atomically consume user approval exactly once and return its binding."""
    now = time.time()
    with SessionLocal() as db:
        candidate_json = db.execute(update(JoinConfirmation).where(
            JoinConfirmation.confirmation_id == confirmation_id,
            JoinConfirmation.chat_id == chat_id,
            JoinConfirmation.user_id == user_id,
            JoinConfirmation.expires_at > now,
            JoinConfirmation.confirmed_at.is_not(None),
            JoinConfirmation.consumed_at.is_(None),
        ).values(consumed_at=now).returning(JoinConfirmation.candidate_json)).scalar_one_or_none()
        db.commit()
        if candidate_json is None:
            return None
        try:
            candidate = json.loads(candidate_json)
        except (TypeError, ValueError):
            return None
        return candidate


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
            "filename": decrypt_text(row.filename, aad=f"chat-file:{row.file_id}:filename"),
            "masked_csv": decrypt_text(row.masked_csv, aad=f"chat-file:{row.file_id}:masked_csv"),
            "columns_json": decrypt_text(row.columns_json, aad=f"chat-file:{row.file_id}:columns"),
            "privacy_policy_json": (
                decrypt_text(row.privacy_policy_json, aad=f"chat-file:{row.file_id}:privacy")
                if row.privacy_policy_json
                else None
            ),
            "workbook_json": (
                decrypt_text(row.workbook_json, aad=f"chat-file:{row.file_id}:workbook")
                if row.workbook_json
                else None
            ),
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
        remaining_files = db.scalar(
            select(func.count()).select_from(ChatFile).where(ChatFile.chat_id == chat_id)
        ) or 0
        if remaining_files == 0:
            db.execute(delete(TokenEntry).where(TokenEntry.session_id == chat_id))
        db.commit()
        return True


def get_or_create_tokens(
    session_id: str,
    values: Iterable[tuple[str, str]],
    counters: dict,
) -> dict[str, str]:
    """Get or mint tokens while keeping sensitive mapping data encrypted at rest.

    PostgreSQL stores only a keyed HMAC blind index in ``value_norm`` and an
    AES-256-GCM ciphertext in ``original``. Plaintext normalized values and
    originals exist only in application memory while masking/unmasking runs.
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
                    value_norm=lookup_digest(session_id, value_norm),
                    token=token,
                    original=encrypt_text(
                        original_value,
                        aad=f"token:{session_id}:{value_type}",
                    ),
                    value_type=value_type,
                )
            )

        if not new_entries:
            return tokens

        try:
            db.add_all(new_entries)
            db.commit()
        except IntegrityError:
            # A concurrent request may have created one or more mappings first.
            db.rollback()
            tokens = _get_existing_tokens(db, session_id, requested)

            unresolved: list[TokenEntry] = []
            for entry in new_entries:
                # Resolve the application-memory normalized value corresponding
                # to this entry's blind index.
                value_norm = next(
                    (norm for norm in requested if lookup_digest(session_id, norm) == entry.value_norm),
                    None,
                )
                if value_norm is not None and value_norm not in tokens:
                    unresolved.append(entry)
                    tokens[value_norm] = entry.token

            if unresolved:
                db.add_all(unresolved)
                db.commit()

        return tokens


def _get_existing_tokens(
    db,
    session_id: str,
    requested: dict[str, tuple[str, str]],
) -> dict[str, str]:
    """Fetch existing mappings by blind index, with legacy-row migration.

    Rows created before Phase 4 stored ``value_norm`` and ``original`` as
    plaintext. Those rows are still readable and are converted to the new
    representation when encountered.
    """
    normalized_values = list(requested)
    tokens: dict[str, str] = {}

    for start in range(0, len(normalized_values), TOKEN_QUERY_CHUNK_SIZE):
        chunk = normalized_values[start:start + TOKEN_QUERY_CHUNK_SIZE]
        blind_indexes = [lookup_digest(session_id, value_norm) for value_norm in chunk]

        rows = db.execute(
            select(TokenEntry.value_norm, TokenEntry.token).where(
                TokenEntry.session_id == session_id,
                TokenEntry.value_norm.in_(blind_indexes),
            )
        ).all()
        by_digest = {value_norm: token for value_norm, token in rows}
        for value_norm, blind_index in zip(chunk, blind_indexes):
            token = by_digest.get(blind_index)
            if token is not None:
                tokens[value_norm] = token

        # Legacy lookup: exact plaintext value_norm matching the requested
        # normalized value. Migrate matching rows in-place.
        legacy_rows = db.execute(
            select(
                TokenEntry.value_norm,
                TokenEntry.token,
                TokenEntry.original,
                TokenEntry.value_type,
            ).where(
                TokenEntry.session_id == session_id,
                TokenEntry.value_norm.in_(chunk),
            )
        ).all()

        for legacy_norm, token, original, value_type in legacy_rows:
            if legacy_norm in tokens:
                continue

            original_plain = decrypt_text(
                original,
                aad=f"token:{session_id}:{value_type}",
            )
            tokens[legacy_norm] = token

            db.execute(
                update(TokenEntry)
                .where(
                    TokenEntry.session_id == session_id,
                    TokenEntry.value_norm == legacy_norm,
                )
                .values(
                    value_norm=lookup_digest(session_id, legacy_norm),
                    original=encrypt_text(
                        original_plain,
                        aad=f"token:{session_id}:{value_type}",
                    ),
                )
            )

        if legacy_rows:
            db.commit()

    return tokens


def get_or_create_token(
    session_id: str,
    value_type: str,
    original_value: str,
    counters: dict,
) -> str:
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
        tokens = db.scalars(
            select(TokenEntry.token).where(TokenEntry.session_id == session_id)
        ).all()
    for token in tokens:
        m = re.match(r"^\[(.+)_(\d+)\]$", token)
        if not m:
            continue
        value_type, num = m.group(1), int(m.group(2))
        counters[value_type] = max(counters.get(value_type, 0), num)
    return counters


def get_known_values(session_id: str) -> dict:
    """Return normalized plaintext values only in application memory."""
    with SessionLocal() as db:
        rows = db.execute(
            select(
                TokenEntry.token,
                TokenEntry.original,
                TokenEntry.value_type,
            ).where(TokenEntry.session_id == session_id)
        ).all()

    result: dict[str, str] = {}
    for token, original, value_type in rows:
        original_plain = decrypt_text(
            original,
            aad=f"token:{session_id}:{value_type}",
        )
        result[str(original_plain).strip().lower()] = token
    return result


def get_reverse_map(session_id: str) -> dict:
    """Return token -> original plaintext only in application memory."""
    with SessionLocal() as db:
        rows = db.execute(
            select(
                TokenEntry.token,
                TokenEntry.original,
                TokenEntry.value_type,
            ).where(TokenEntry.session_id == session_id)
        ).all()

    result: dict[str, str] = {}
    for token, original, value_type in rows:
        result[token] = decrypt_text(
            original,
            aad=f"token:{session_id}:{value_type}",
        )
    return result



def clear_session(session_id: str) -> None:
    with SessionLocal() as db:
        db.execute(delete(TokenEntry).where(TokenEntry.session_id == session_id))
        db.commit()


def session_entry_count(session_id: str) -> int:
    with SessionLocal() as db:
        return int(
            db.scalar(select(func.count()).select_from(TokenEntry).where(TokenEntry.session_id == session_id)) or 0
        )
