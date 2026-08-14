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
