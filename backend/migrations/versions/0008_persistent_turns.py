"""Persist idempotent chat-turn lifecycle metadata."""

from alembic import op
import sqlalchemy as sa

revision = "0008_persistent_turns"
down_revision = "0007_encrypted_workbook_document"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "conversation_turns",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.String(length=36), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("user_message_id", sa.Integer(), nullable=False),
        sa.Column("assistant_message_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=True),
        sa.Column("context_revision", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.chat_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_message_id"], ["chat_messages.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assistant_message_id"], ["chat_messages.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("chat_id", "request_id", name="uq_conversation_turn_request"),
    )
    op.create_index(
        "uq_conversation_turn_active_chat",
        "conversation_turns",
        ["chat_id"],
        unique=True,
        postgresql_where=sa.text("status = 'in_progress'"),
        sqlite_where=sa.text("status = 'in_progress'"),
    )
    op.create_index(
        "ix_conversation_turn_chat_created",
        "conversation_turns",
        ["chat_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversation_turn_chat_created", table_name="conversation_turns")
    op.drop_index("uq_conversation_turn_active_chat", table_name="conversation_turns")
    op.drop_table("conversation_turns")
