"""Add temporary guest sessions."""

from alembic import op
import sqlalchemy as sa

revision = "0004_guest_sessions"
down_revision = "0003_message_metadata"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "guest_sessions",
        sa.Column("session_hash", sa.String(length=64), primary_key=True),
        sa.Column("user_auth0_sub", sa.String(length=255), nullable=False, unique=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["user_auth0_sub"], ["users.auth0_sub"], ondelete="CASCADE"),
    )
    op.create_index("ix_guest_sessions_expires_at", "guest_sessions", ["expires_at"])

def downgrade() -> None:
    op.drop_index("ix_guest_sessions_expires_at", table_name="guest_sessions")
    op.drop_table("guest_sessions")
