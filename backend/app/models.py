"""SQLAlchemy ORM models for Privy."""

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, Text
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
    title: Mapped[str] = mapped_column(String(200), nullable=False)
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
    created_at: Mapped[float] = mapped_column(Float, nullable=False)

    chat: Mapped[Chat] = relationship(back_populates="messages")

    __table_args__ = (
        Index("ix_chat_messages_chat_id", "chat_id", "id"),
    )


class ChatFile(Base):
    __tablename__ = "chat_files"

    file_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chat_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("chats.chat_id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    masked_csv: Mapped[str] = mapped_column(Text, nullable=False)
    columns_json: Mapped[str] = mapped_column(Text, nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    truncated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    masked_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)

    chat: Mapped[Chat] = relationship(back_populates="files")

    __table_args__ = (
        Index("ix_chat_files_chat_created", "chat_id", "created_at"),
    )
