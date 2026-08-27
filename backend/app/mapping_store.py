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

import re
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
        # Authenticated application users. Auth0 remains the identity provider;
        # this table stores only the stable Auth0 subject plus local app metadata.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                auth0_sub      TEXT PRIMARY KEY,
                email          TEXT,
                display_name   TEXT,
                role           TEXT NOT NULL DEFAULT 'user',
                created_at     REAL NOT NULL,
                last_login_at  REAL NOT NULL
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
                user_id      TEXT,
                title        TEXT NOT NULL,
                created_at   REAL NOT NULL,
                updated_at   REAL NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users (auth0_sub)
            )
            """
        )
        # Existing local databases were created before Auth0. Add the new
        # nullable owner column without destroying existing chats. Those
        # orphaned chats are assigned to the first authenticated user in
        # get_or_create_user().
        chat_columns = {row[1] for row in conn.execute("PRAGMA table_info(chats)").fetchall()}
        if "user_id" not in chat_columns:
            conn.execute("ALTER TABLE chats ADD COLUMN user_id TEXT")

        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_chats_user_updated "
            "ON chats (user_id, updated_at DESC)"
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
        # The MASKED version of one uploaded file per chat. Raw file content
        # is never written here -- see masking.py's module docstring.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_files (
                chat_id      TEXT PRIMARY KEY,
                filename     TEXT NOT NULL,
                masked_csv   TEXT NOT NULL,
                columns_json TEXT NOT NULL,
                row_count    INTEGER NOT NULL,
                truncated    INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (chat_id) REFERENCES chats (chat_id)
            )
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


def get_or_create_user(auth0_sub: str, email: str | None = None, display_name: str | None = None) -> dict:
    """Create the local user on first login and assign legacy orphan chats.

    The first authenticated user becomes admin for this development-era
    single-admin model; subsequent users are ordinary users. This avoids a
    bootstrap lockout while keeping role data local and easy to replace with
    an explicit admin-management flow later.
    """
    now = time.time()
    with _connect() as conn:
        row = conn.execute(
            "SELECT auth0_sub, email, display_name, role, created_at, last_login_at "
            "FROM users WHERE auth0_sub = ?",
            (auth0_sub,),
        ).fetchone()

        if row:
            conn.execute(
                "UPDATE users SET email = ?, display_name = ?, last_login_at = ? "
                "WHERE auth0_sub = ?",
                (email or row[1], display_name or row[2], now, auth0_sub),
            )
            role = row[3]
            created_at = row[4]
        else:
            user_count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
            role = "admin" if user_count == 0 else "user"
            conn.execute(
                "INSERT INTO users (auth0_sub, email, display_name, role, created_at, last_login_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (auth0_sub, email, display_name, role, now, now),
            )
            created_at = now

        # The existing local DB may already contain chats created before auth.
        # Assign them to the first user who signs in so nothing is lost.
        conn.execute(
            "UPDATE chats SET user_id = ? WHERE user_id IS NULL",
            (auth0_sub,),
        )

    return {
        "auth0_sub": auth0_sub,
        "email": email or row[1] if row else email,
        "display_name": display_name or row[2] if row else display_name,
        "role": role,
        "created_at": created_at,
        "last_login_at": now,
    }


def get_user(auth0_sub: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT auth0_sub, email, display_name, role, created_at, last_login_at "
            "FROM users WHERE auth0_sub = ?",
            (auth0_sub,),
        ).fetchone()
    if not row:
        return None
    return {
        "auth0_sub": row[0],
        "email": row[1],
        "display_name": row[2],
        "role": row[3],
        "created_at": row[4],
        "last_login_at": row[5],
    }


def create_chat(user_id: str, title: str = "New chat") -> str:
    """Creates a new chat owned by the authenticated user."""
    chat_id = str(uuid.uuid4())
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chats (chat_id, user_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, user_id, title, now, now),
        )
    return chat_id


def list_chats(user_id: str) -> list:
    """Only chats owned by the current user, newest first."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT chat_id, title, created_at, updated_at FROM chats "
            "WHERE user_id = ? ORDER BY updated_at DESC",
            (user_id,),
        ).fetchall()
    return [
        {"chat_id": r[0], "title": r[1], "created_at": r[2], "updated_at": r[3]}
        for r in rows
    ]


def get_chat_messages(chat_id: str) -> list:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT role, content, masked_count FROM chat_messages "
            "WHERE chat_id = ? ORDER BY id ASC",
            (chat_id,),
        ).fetchall()
    return [{"role": r[0], "content": r[1], "masked_count": r[2]} for r in rows]


def add_message(chat_id: str, role: str, content: str, masked_count: int = 0):
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


def get_chat(chat_id: str, user_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT chat_id, title, created_at, updated_at FROM chats "
            "WHERE chat_id = ? AND user_id = ?",
            (chat_id, user_id),
        ).fetchone()
    if not row:
        return None
    return {"chat_id": row[0], "title": row[1], "created_at": row[2], "updated_at": row[3]}


def rename_chat(chat_id: str, user_id: str, title: str):
    with _connect() as conn:
        conn.execute(
            "UPDATE chats SET title = ? WHERE chat_id = ? AND user_id = ?",
            (title, chat_id, user_id),
        )


def delete_chat(chat_id: str, user_id: str):
    with _connect() as conn:
        conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM chat_files WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM chats WHERE chat_id = ? AND user_id = ?", (chat_id, user_id))


def set_chat_file(chat_id: str, filename: str, masked_csv: str, columns_json: str,
                   row_count: int, truncated: bool):
    """Stores the MASKED version of an uploaded file against a chat -- never
    the raw file. One file per chat; uploading again replaces it. See
    masking.py's module docstring for why raw data isn't persisted here."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO chat_files (chat_id, filename, masked_csv, columns_json, row_count, truncated) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET "
            "filename=excluded.filename, masked_csv=excluded.masked_csv, "
            "columns_json=excluded.columns_json, row_count=excluded.row_count, "
            "truncated=excluded.truncated",
            (chat_id, filename, masked_csv, columns_json, row_count, int(truncated)),
        )


def get_chat_file(chat_id: str):
    with _connect() as conn:
        row = conn.execute(
            "SELECT filename, masked_csv, columns_json, row_count, truncated "
            "FROM chat_files WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    if not row:
        return None
    return {
        "filename": row[0], "masked_csv": row[1], "columns_json": row[2],
        "row_count": row[3], "truncated": bool(row[4]),
    }

def delete_chat_file(chat_id: str):
    """Remove the masked file attached to a chat.

    Token mappings are intentionally retained because earlier chat messages
    may still contain masked tokens that need to be unmasked for display.
    """
    with _connect() as conn:
        conn.execute("DELETE FROM chat_files WHERE chat_id = ?", (chat_id,))


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


def load_counters(session_id: str) -> dict:
    """
    Rebuilds the {value_type: highest_number_used} dict from the DB for a
    session. Needed because the backend is stateless across HTTP requests --
    unlike the Streamlit version, there's no st.session_state living in
    memory between one request and the next to carry the running counters
    in. Without this, a fresh empty counters dict on every request would
    restart numbering at 1 each time and mint colliding tokens (e.g. a
    second, different value also becoming "[PERSON_1]"). Call this once at
    the start of a request that might mint new tokens, before any
    get_or_create_token() calls.
    """
    counters = {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT token FROM token_entries WHERE session_id = ?", (session_id,)
        ).fetchall()
    for (token,) in rows:
        m = re.match(r"^\[(.+)_(\d+)\]$", token)
        if not m:
            continue
        value_type, num = m.group(1), int(m.group(2))
        counters[value_type] = max(counters.get(value_type, 0), num)
    return counters


def get_known_values(session_id: str) -> dict:
    """value_norm -> token for every value already masked in this session,
    structured or free-text alike. This IS the same dict masking.py's
    mask_dataframe() calls "known_values" -- token_entries.value_norm is
    defined identically (str(value).strip().lower()), so it can be
    reconstructed from the DB instead of needing its own storage. Lets a
    later question in the same chat reuse a token minted while masking the
    uploaded file (or an earlier question), without the backend having to
    keep anything in memory between requests."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT value_norm, token FROM token_entries WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    return {value_norm: token for value_norm, token in rows}


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
