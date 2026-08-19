"""
mapping_store.py
-----------------
SQLite-backed store for the token <-> original-value mapping.

Token identity is keyed by the normalized VALUE ALONE, not value+type. This
matters: the same real-world value can be masked from two different code
paths within one session (e.g. "Mumbai" sitting in a spreadsheet's City
column vs. "mumbai" typed into a chat question), and those paths don't
always agree on a type tag. If identity were type-scoped, the same value
could end up with two different, unrelated tokens -- and the model would
have no way to know a token in your question refers to the same thing as a
token in the data, breaking lookups and comparisons. Keying by value alone
guarantees one token per real-world value per session, however it was found.

Why SQLite instead of pure in-memory:
- Survives an app restart / crash within the same session (a browser refresh
  in Streamlit re-runs the script, so without this the mapping would be lost
  exactly like the JS version).
- Each session gets its own session_id so two users (or two files) never
  share tokens/mappings.
- The DB file is local only -- nothing here is ever sent over the network.

This is intentionally simple (no ORM) since the table is tiny and the access
pattern is trivial (lookup by key, insert if missing).
"""

import sqlite3
import os
import time
import uuid
from contextlib import contextmanager

DB_PATH = os.path.join(os.path.dirname(__file__), "mapping_store.db")


def init_db():
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS token_entries (
                session_id   TEXT NOT NULL,
                value_norm   TEXT NOT NULL,   -- normalized original value: the session-wide identity key
                token        TEXT NOT NULL,
                original     TEXT NOT NULL,
                value_type   TEXT NOT NULL,   -- type seen when this value was FIRST masked; only used for the token's display prefix
                PRIMARY KEY (session_id, value_norm)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_session_token
            ON token_entries (session_id, token)
            """
        )
        # Admin-set app config (currently: shared LLM api key + model). Global,
        # not session-scoped -- one row per key, last write wins. Separate
        # table from token_entries on purpose: this is app config an admin
        # sets, not PII passing through the masking pipeline, even though it
        # lives in the same local-only DB file.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS admin_config (
                config_key   TEXT PRIMARY KEY,
                config_value TEXT NOT NULL
            )
            """
        )
        # Chat history. No auth yet -- these are app-wide (anyone using this
        # deployment sees the same chat list), same trust boundary as
        # admin_config above. Revisit once auth exists.
        # created_at/updated_at are Unix timestamps (REAL, seconds incl.
        # fractional part) computed in Python at insert time -- NOT sqlite's
        # datetime('now'), which only has 1-second resolution and produces
        # ties (and therefore unpredictable ORDER BY) whenever two chats are
        # touched within the same second, which is routine.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chats (
                chat_id      TEXT PRIMARY KEY,
                title        TEXT NOT NULL,
                created_at   REAL NOT NULL,
                updated_at   REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id      TEXT NOT NULL,
                role         TEXT NOT NULL,
                content      TEXT NOT NULL,
                masked_count INTEGER NOT NULL DEFAULT 0,
                created_at   REAL NOT NULL,
                FOREIGN KEY (chat_id) REFERENCES chats (chat_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_chat_messages_chat_id
            ON chat_messages (chat_id, id)
            """
        )


def get_admin_config(config_key: str, default: str = "") -> str:
    with _connect() as conn:
        row = conn.execute(
            "SELECT config_value FROM admin_config WHERE config_key = ?", (config_key,)
        ).fetchone()
    return row[0] if row else default


def set_admin_config(config_key: str, config_value: str):
    with _connect() as conn:
        conn.execute(
            "INSERT INTO admin_config (config_key, config_value) VALUES (?, ?) "
            "ON CONFLICT(config_key) DO UPDATE SET config_value = excluded.config_value",
            (config_key, config_value),
        )


def create_chat(title: str = "New chat") -> str:
    """Creates a new chat row and returns its id."""
    chat_id = str(uuid.uuid4())
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chats (chat_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (chat_id, title, now, now),
        )
    return chat_id


def list_chats() -> list:
    """All chats, most recently active first. Each item:
    {chat_id, title, created_at, updated_at}."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chat_id, title, created_at, updated_at FROM chats "
            "ORDER BY updated_at DESC"
        ).fetchall()
    return [
        {"chat_id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3]}
        for r in rows
    ]


def get_chat_messages(chat_id: str) -> list:
    """All messages for a chat, oldest first. Each item:
    {role, content, masked_count}."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content, masked_count FROM chat_messages "
            "WHERE chat_id = ? ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
    return [{"role": r[0], "content": r[1], "masked_count": r[2]} for r in rows]


def add_message(chat_id: str, role: str, content: str, masked_count: int = 0):
    """Appends a message to a chat and bumps the chat's updated_at so it
    sorts to the top of list_chats()."""
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chat_messages (chat_id, role, content, masked_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, role, content, masked_count, now),
        )
        conn.execute(
            "UPDATE chats SET updated_at = ? WHERE chat_id = ?",
            (now, chat_id),
        )


def rename_chat(chat_id: str, title: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE chats SET title = ? WHERE chat_id = ?", (title, chat_id)
        )


def delete_chat(chat_id: str):
    with _connect() as conn:
        conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_or_create_token(session_id: str, value_type: str, original_value: str, counters: dict) -> str:
    """
    Returns the existing token for this value within the session (regardless
    of which value_type first created it), or creates + stores a new one.
    `counters` is a dict tracking the next numeric suffix per type, kept in
    the caller (Streamlit session_state) so numbering stays stable within a
    run without extra DB round trips.
    """
    value_norm = str(original_value).strip().lower()

    with _connect() as conn:
        row = conn.execute(
            "SELECT token FROM token_entries WHERE session_id = ? AND value_norm = ?",
            (session_id, value_norm),
        ).fetchone()
        if row:
            return row[0]

        counters[value_type] = counters.get(value_type, 0) + 1
        token = f"[{value_type}_{counters[value_type]}]"

        conn.execute(
            "INSERT INTO token_entries (session_id, value_norm, token, original, value_type) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, value_norm, token, str(original_value), value_type),
        )
        return token


def get_reverse_map(session_id: str) -> dict:
    """token -> original, for unmasking model responses."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT token, original FROM token_entries WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    return {token: original for token, original in rows}


def clear_session(session_id: str):
    with _connect() as conn:
        conn.execute("DELETE FROM token_entries WHERE session_id = ?", (session_id,))


def session_entry_count(session_id: str) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM token_entries WHERE session_id = ?", (session_id,)
        ).fetchone()
    return row[0] if row else 0
