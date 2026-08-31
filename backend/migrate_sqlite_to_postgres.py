"""One-time migration from Privy's legacy SQLite DB to PostgreSQL.

Run AFTER `alembic upgrade head` and BEFORE switching the application over to
PostgreSQL in production. The script is idempotent for rows with the same
primary keys: existing destination rows are left intact.
"""

import os
import sqlite3
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import AdminConfig, Chat, ChatFile, ChatMessage, TokenEntry, User

load_dotenv()

SQLITE_PATH = Path(os.environ.get("LEGACY_SQLITE_PATH", Path(__file__).resolve().parent / "app" / "mapping_store.db"))
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is required")


def rows(conn, table: str):
    return conn.execute(f"SELECT * FROM {table}").fetchall()


def main() -> None:
    if not SQLITE_PATH.exists():
        raise FileNotFoundError(f"Legacy SQLite database not found: {SQLITE_PATH}")

    target = create_engine(DATABASE_URL, pool_pre_ping=True)
    source = sqlite3.connect(SQLITE_PATH)
    source.row_factory = sqlite3.Row

    counts = {}
    with Session(target) as db:
        for r in rows(source, "admin_config"):
            if not db.get(AdminConfig, r["config_key"]):
                db.add(AdminConfig(config_key=r["config_key"], config_value=r["config_value"]))
        counts["admin_config"] = len(rows(source, "admin_config"))

        for r in rows(source, "users"):
            if not db.get(User, r["auth0_sub"]):
                db.add(
                    User(
                        auth0_sub=r["auth0_sub"],
                        email=r["email"],
                        display_name=r["display_name"],
                        role=r["role"],
                        created_at=r["created_at"],
                        last_login_at=r["last_login_at"],
                    )
                )
        counts["users"] = len(rows(source, "users"))

        # Older SQLite databases may contain NULL user_id. Keep them NULL
        # during import; the first authenticated user will claim them later.
        for r in rows(source, "chats"):
            if not db.get(Chat, r["chat_id"]):
                user_id = r["user_id"]
                db.add(
                    Chat(
                        chat_id=r["chat_id"],
                        user_id=user_id,
                        title=r["title"],
                        created_at=r["created_at"],
                        updated_at=r["updated_at"],
                    )
                )
        counts["chats"] = len(rows(source, "chats"))

        for r in rows(source, "chat_messages"):
            existing = db.execute(
                select(ChatMessage.id).where(
                    ChatMessage.id == r["id"]
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(
                    ChatMessage(
                        id=r["id"],
                        chat_id=r["chat_id"],
                        role=r["role"],
                        content=r["content"],
                        masked_count=r["masked_count"],
                        created_at=r["created_at"],
                    )
                )
        counts["chat_messages"] = len(rows(source, "chat_messages"))

        for r in rows(source, "chat_files"):
            if not db.get(ChatFile, r["chat_id"]):
                db.add(
                    ChatFile(
                        chat_id=r["chat_id"],
                        filename=r["filename"],
                        masked_csv=r["masked_csv"],
                        columns_json=r["columns_json"],
                        row_count=r["row_count"],
                        truncated=bool(r["truncated"]),
                    )
                )
        counts["chat_files"] = len(rows(source, "chat_files"))

        for r in rows(source, "token_entries"):
            key = {"session_id": r["session_id"], "value_norm": r["value_norm"]}
            if db.get(TokenEntry, key) is None:
                db.add(
                    TokenEntry(
                        session_id=r["session_id"],
                        value_norm=r["value_norm"],
                        token=r["token"],
                        original=r["original"],
                        value_type=r["value_type"],
                    )
                )
        counts["token_entries"] = len(rows(source, "token_entries"))

        db.commit()

    source.close()
    print("Migration complete:")
    for table, count in counts.items():
        print(f"  {table}: {count} source rows inspected")


if __name__ == "__main__":
    main()
