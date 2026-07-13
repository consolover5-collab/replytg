"""Read-only доступ к bridge.db. Единственное место записи туда — drafts_writer."""
import sqlite3
from pathlib import Path

REQUIRED = {
    "messages": {"id", "chat_id", "ts", "direction", "text", "sender_name", "media_type", "is_auto"},
    "connections": {"connection_id", "owner_id", "is_enabled"},
    "drafts": {"id", "chat_id", "text", "status", "error", "created_ts", "card_message_id"},
}


def connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def check_schema(conn: sqlite3.Connection) -> None:
    """Совместимость со схемой бриджа; при несовпадении — отказ запуска с внятным текстом."""
    for table, cols in REQUIRED.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        missing = cols - have
        if missing:
            raise SystemExit(
                f"bridge.db несовместима: в таблице {table} нет колонок {sorted(missing)}. "
                "Проверь версию telegram-business-bridge (см. README)."
            )


def fetch_new(conn: sqlite3.Connection, after_rowid: int, limit: int = 500) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT id, chat_id, ts, direction, is_auto FROM messages "
        "WHERE id > ? ORDER BY id LIMIT ?",
        (after_rowid, limit),
    ).fetchall()


def history(conn: sqlite3.Connection, chat_id: int, limit: int = 30) -> list[sqlite3.Row]:
    """Последние limit сообщений чата по возрастанию времени."""
    rows = conn.execute(
        "SELECT ts, direction, sender_name, media_type, text FROM messages "
        "WHERE chat_id=? ORDER BY ts DESC, id DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    return list(reversed(rows))


def wave_incoming(conn: sqlite3.Connection, chat_id: int, since_ts: int) -> list[sqlite3.Row]:
    """Входящие текущей волны (для карточки)."""
    return conn.execute(
        "SELECT ts, sender_name, media_type, text FROM messages "
        "WHERE chat_id=? AND direction='in' AND ts >= ? ORDER BY ts, id",
        (chat_id, since_ts),
    ).fetchall()


def has_enabled_connection(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM connections WHERE is_enabled=1 LIMIT 1"
    ).fetchone() is not None
