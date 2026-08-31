"""Initial Privy PostgreSQL schema."""

from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "token_entries",
        sa.Column("session_id", sa.String(length=255), nullable=False),
        sa.Column("value_norm", sa.Text(), nullable=False),
        sa.Column("token", sa.String(length=255), nullable=False),
        sa.Column("original", sa.Text(), nullable=False),
        sa.Column("value_type", sa.String(length=100), nullable=False),
        sa.PrimaryKeyConstraint("session_id", "value_norm"),
    )
    op.create_index("ix_token_entries_session_token", "token_entries", ["session_id", "token"])

    op.create_table(
        "admin_config",
        sa.Column("config_key", sa.String(length=255), nullable=False),
        sa.Column("config_value", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("config_key"),
    )

    op.create_table(
        "users",
        sa.Column("auth0_sub", sa.String(length=255), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=255), nullable=True),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("last_login_at", sa.Float(), nullable=False),
        sa.PrimaryKeyConstraint("auth0_sub"),
    )

    op.create_table(
        "chats",
        sa.Column("chat_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.auth0_sub"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chat_id"),
    )
    op.create_index("ix_chats_user_updated", "chats", ["user_id", "updated_at"])

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("chat_id", sa.String(length=36), nullable=False),
        sa.Column("role", sa.String(length=50), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("masked_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.chat_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_messages_chat_id", "chat_messages", ["chat_id", "id"])

    op.create_table(
        "chat_files",
        sa.Column("chat_id", sa.String(length=36), nullable=False),
        sa.Column("filename", sa.String(length=512), nullable=False),
        sa.Column("masked_csv", sa.Text(), nullable=False),
        sa.Column("columns_json", sa.Text(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.chat_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("chat_id"),
    )


def downgrade() -> None:
    op.drop_table("chat_files")
    op.drop_index("ix_chat_messages_chat_id", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_chats_user_updated", table_name="chats")
    op.drop_table("chats")
    op.drop_table("users")
    op.drop_table("admin_config")
    op.drop_index("ix_token_entries_session_token", table_name="token_entries")
    op.drop_table("token_entries")
