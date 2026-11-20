"""Persist complete multi-worksheet workbook data."""

from alembic import op
import sqlalchemy as sa

revision = "0007_encrypted_workbook_document"
down_revision = "0006_encrypted_filename_capacity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_files", sa.Column("workbook_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_files", "workbook_json")
