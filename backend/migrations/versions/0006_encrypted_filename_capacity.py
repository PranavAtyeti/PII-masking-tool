"""Store encrypted filenames and chat titles without fixed-length limits."""

from alembic import op
import sqlalchemy as sa

revision = "0006_encrypted_filename_capacity"
down_revision = "0005_column_privacy_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "chat_files",
        "filename",
        existing_type=sa.String(length=512),
        type_=sa.Text(),
        existing_nullable=False,
    )
    op.alter_column(
        "chats",
        "title",
        existing_type=sa.String(length=200),
        type_=sa.Text(),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "chats",
        "title",
        existing_type=sa.Text(),
        type_=sa.String(length=200),
        existing_nullable=False,
    )
    op.alter_column(
        "chat_files",
        "filename",
        existing_type=sa.Text(),
        type_=sa.String(length=512),
        existing_nullable=False,
    )
