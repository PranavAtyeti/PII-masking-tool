"""Persist per-column Share/Mask/Exclude policy on each chat file."""

from alembic import op
import sqlalchemy as sa

revision = "0005_column_privacy_policy"
down_revision = "618fe1fa310c"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chat_files", sa.Column("privacy_policy_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("chat_files", "privacy_policy_json")
