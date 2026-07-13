import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_state (
    chat_id INTEGER PRIMARY KEY,
    state TEXT NOT NULL DEFAULT 'idle',   -- idle|collecting|generating|awaiting|silence
    wave_started_ts INTEGER,
    gen_id INTEGER NOT NULL DEFAULT 0,    -- версия генерации (инвалидация старых кнопок)
    variants_json TEXT,
    card_message_id INTEGER,
    repeat_at_ts INTEGER,                 -- когда повторить карточку; NULL = не повторять
    silence_until_ts INTEGER,
    pending_incoming INTEGER NOT NULL DEFAULT 0
);
"""

_STATE_FIELDS = {
    "state", "wave_started_ts", "gen_id", "variants_json",
    "card_message_id", "repeat_at_ts", "silence_until_ts", "pending_incoming",
}


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_cursor(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM kv WHERE key='last_seen_rowid'").fetchone()
    return row["value"] if row else 0


def set_cursor(conn: sqlite3.Connection, rowid: int) -> None:
    conn.execute(
        "INSERT INTO kv (key, value) VALUES ('last_seen_rowid', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (rowid,),
    )
    conn.commit()


def get_chat_state(conn: sqlite3.Connection, chat_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM chat_state WHERE chat_id=?", (chat_id,)).fetchone()


def set_chat_state(conn: sqlite3.Connection, chat_id: int, **fields) -> None:
    """Частичный upsert: неупомянутые поля не затираются."""
    bad = set(fields) - _STATE_FIELDS
    if bad:
        raise ValueError(f"unknown chat_state fields: {bad}")
    conn.execute("INSERT OR IGNORE INTO chat_state (chat_id) VALUES (?)", (chat_id,))
    if fields:
        sets = ", ".join(f"{k}=:{k}" for k in fields)
        conn.execute(
            f"UPDATE chat_state SET {sets} WHERE chat_id=:chat_id",
            {**fields, "chat_id": chat_id},
        )
    conn.commit()


def list_chat_states(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM chat_state").fetchall()
