"""Add structured metadata to chat messages."""

from alembic import op
import sqlalchemy as sa

revision = "0003_message_metadata"
down_revision = "0002_multi_file_chat_files"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("metadata_json", sa.Text(), nullable=True))

def downgrade() -> None:
    op.drop_column("chat_messages", "metadata_json")
