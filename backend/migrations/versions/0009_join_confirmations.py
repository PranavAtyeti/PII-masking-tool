"""Persist short-lived, one-time confirmation state for ambiguous joins."""

from alembic import op
import sqlalchemy as sa

revision = "0009_join_confirmations"
down_revision = "0008_persistent_turns"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "join_confirmations",
        sa.Column("confirmation_id", sa.String(length=36), nullable=False),
        sa.Column("chat_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=255), nullable=False),
        sa.Column("candidate_json", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("confirmed_at", sa.Float(), nullable=True),
        sa.Column("consumed_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.chat_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("confirmation_id"),
    )
    op.create_index(
        "ix_join_confirmations_owner_expiry",
        "join_confirmations",
        ["chat_id", "user_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_join_confirmations_owner_expiry", table_name="join_confirmations")
    op.drop_table("join_confirmations")
