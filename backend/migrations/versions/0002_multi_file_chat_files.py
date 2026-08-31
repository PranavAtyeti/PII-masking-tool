"""Allow multiple files per chat.

The previous schema used chat_id as the primary key of chat_files, which meant
one chat could hold only one attachment. This migration introduces a stable
file_id primary key and keeps all existing file rows intact.
"""

import re
import uuid
import time

from alembic import op
import sqlalchemy as sa

revision = "0002_multi_file_chat_files"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_files",
        sa.Column("file_id", sa.String(length=36), nullable=True),
    )
    op.add_column(
        "chat_files",
        sa.Column("masked_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "chat_files",
        sa.Column("created_at", sa.Float(), nullable=True),
    )
    op.add_column(
        "chat_files",
        sa.Column("updated_at", sa.Float(), nullable=True),
    )

    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT chat_id FROM chat_files")).mappings().all()
    now = time.time()
    for row in rows:
        file_id = str(uuid.uuid4())
        current = bind.execute(
            sa.text("SELECT masked_csv FROM chat_files WHERE chat_id = :chat_id"),
            {"chat_id": row["chat_id"]},
        ).mappings().first()
        masked_csv = current["masked_csv"] if current else ""
        masked_count = len(re.findall(r"\[[A-Za-z0-9_]+\]", masked_csv))
        bind.execute(
            sa.text(
                "UPDATE chat_files "
                "SET file_id = :file_id, masked_count = :masked_count, "
                "created_at = :now, updated_at = :now "
                "WHERE chat_id = :chat_id"
            ),
            {"file_id": file_id, "masked_count": masked_count, "now": now, "chat_id": row["chat_id"]},
        )

    op.alter_column("chat_files", "file_id", nullable=False)
    op.alter_column("chat_files", "masked_count", nullable=False)
    op.alter_column("chat_files", "created_at", nullable=False)
    op.alter_column("chat_files", "updated_at", nullable=False)

    op.drop_constraint("chat_files_pkey", "chat_files", type_="primary")
    op.create_primary_key("chat_files_pkey", "chat_files", ["file_id"])
    op.create_index(
        "ix_chat_files_chat_created",
        "chat_files",
        ["chat_id", "created_at"],
    )


def downgrade() -> None:
    # Only safe when every chat has at most one file. The application should
    # therefore be drained back to one attachment per chat before downgrade.
    bind = op.get_bind()
    duplicate = bind.execute(
        sa.text(
            "SELECT chat_id FROM chat_files "
            "GROUP BY chat_id HAVING COUNT(*) > 1 LIMIT 1"
        )
    ).first()
    if duplicate:
        raise RuntimeError(
            "Cannot downgrade multi-file chat storage while any chat has more than one file."
        )

    op.drop_index("ix_chat_files_chat_created", table_name="chat_files")
    op.drop_constraint("chat_files_pkey", "chat_files", type_="primary")
    op.create_primary_key("chat_files_pkey", "chat_files", ["chat_id"])
    op.drop_column("chat_files", "updated_at")
    op.drop_column("chat_files", "created_at")
    op.drop_column("chat_files", "masked_count")
    op.drop_column("chat_files", "file_id")
