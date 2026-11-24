"""SQLAlchemy ORM models for Privy."""

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class TokenEntry(Base):
    __tablename__ = "token_entries"

    session_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    value_norm: Mapped[str] = mapped_column(Text, primary_key=True)
    token: Mapped[str] = mapped_column(String(255), nullable=False)
    original: Mapped[str] = mapped_column(Text, nullable=False)
    value_type: Mapped[str] = mapped_column(String(100), nullable=False)

    __table_args__ = (
        Index("ix_token_entries_session_token", "session_id", "token"),
    )


class AdminConfig(Base):
    __tablename__ = "admin_config"

    config_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    config_value: Mapped[str] = mapped_column(Text, nullable=False)


class User(Base):
    __tablename__ = "users"

    auth0_sub: Mapped[str] = mapped_column(String(255), primary_key=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[str] = mapped_column(String(50), nullable=False, default="user")
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    last_login_at: Mapped[float] = mapped_column(Float, nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    chats: Mapped[list["Chat"]] = relationship(
        back_populates="user",
        cascade="all, delete-orphan",
        foreign_keys="Chat.user_id",
    )


class Chat(Base):
    __tablename__ = "chats"

    chat_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.auth0_sub", ondelete="CASCADE"),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)

    user: Mapped[User] = relationship(back_populates="chats", foreign_keys=[user_id])
    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="chat",
        cascade="all, delete-orphan",
        order_by="ChatMessage.id",
    )
    files: Mapped[list["ChatFile"]] = relationship(
        back_populates="chat",
        cascade="all, delete-orphan",
        order_by="ChatFile.created_at",
    )

    __table_args__ = (
        Index("ix_chats_user_updated", "user_id", "updated_at"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("chats.chat_id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(50), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    masked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)

    chat: Mapped[Chat] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_chat_messages_chat_id", "chat_id", "id"),
    )


class ConversationTurn(Base):
    """Non-content lifecycle metadata for one idempotent chat request."""

    __tablename__ = "conversation_turns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("chats.chat_id", ondelete="CASCADE"),
        nullable=False,
    )
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    user_message_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("chat_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    assistant_message_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("chat_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    context_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        UniqueConstraint("chat_id", "request_id", name="uq_conversation_turn_request"),
        Index(
            "uq_conversation_turn_active_chat",
            "chat_id",
            unique=True,
            postgresql_where=text("status = 'in_progress'"),
            sqlite_where=text("status = 'in_progress'"),
        ),
        Index("ix_conversation_turn_chat_created", "chat_id", "created_at"),
    )


class JoinConfirmation(Base):
    """One-time consent for a specific ambiguous join candidate."""

    __tablename__ = "join_confirmations"

    confirmation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chat_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("chats.chat_id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False)
    candidate_json: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    confirmed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    consumed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)

    __table_args__ = (
        Index("ix_join_confirmations_owner_expiry", "chat_id", "user_id", "expires_at"),
    )


class ChatFile(Base):
    __tablename__ = "chat_files"

    file_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chat_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("chats.chat_id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    masked_csv: Mapped[str] = mapped_column(Text, nullable=False)
    columns_json: Mapped[str] = mapped_column(Text, nullable=False)
    privacy_policy_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    workbook_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    masked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)

    chat: Mapped[Chat] = relationship(back_populates="files")

    __table_args__ = (
        Index("ix_chat_files_chat_created", "chat_id", "created_at"),
    )


class GuestSession(Base):
    __tablename__ = "guest_sessions"

    session_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_auth0_sub: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.auth0_sub", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)

    user: Mapped[User] = relationship(foreign_keys=[user_auth0_sub])

    __table_args__ = (
        Index("ix_guest_sessions_expires_at", "expires_at"),
    )



class AuthSession(Base):
    """Server-side refresh-token session metadata."""

    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_auth0_sub: Mapped[str] = mapped_column(
        String(255),
        ForeignKey("users.auth0_sub", ondelete="CASCADE"),
        nullable=False,
    )
    refresh_token_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    expires_at: Mapped[float] = mapped_column(Float, nullable=False)
    last_used_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped[User] = relationship(foreign_keys=[user_auth0_sub])

    __table_args__ = (
        Index("ix_auth_sessions_expires_at", "expires_at"),
        Index("ix_auth_sessions_user", "user_auth0_sub"),
    )
