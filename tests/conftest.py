import sqlite3
from pathlib import Path

import pytest

BRIDGE_SCHEMA = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    connection_id TEXT, chat_id INTEGER NOT NULL, message_id INTEGER,
    sender_id INTEGER, sender_name TEXT, ts INTEGER NOT NULL,
    direction TEXT NOT NULL DEFAULT 'in', is_auto INTEGER NOT NULL DEFAULT 0,
    media_type TEXT, text TEXT, media_path TEXT, file_id TEXT,
    file_unique_id TEXT, file_size INTEGER, raw_json TEXT NOT NULL
);
CREATE TABLE connections (
    connection_id TEXT PRIMARY KEY, owner_id INTEGER NOT NULL,
    rights_json TEXT NOT NULL, is_enabled INTEGER NOT NULL, updated_ts INTEGER NOT NULL
);
CREATE TABLE drafts (
    id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
    status TEXT NOT NULL, error TEXT, created_ts INTEGER NOT NULL, card_message_id INTEGER
);
"""


class _ConnWithPath(sqlite3.Connection):
    """Обычный sqlite3.Connection не имеет __dict__, атрибут path на нём не поставить.
    Тривиальный подкласс получает __dict__ и позволяет тестам таскать путь к файлу."""


@pytest.fixture
def bridge_db(tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "bridge.db"
    conn = sqlite3.connect(path, factory=_ConnWithPath)
    conn.row_factory = sqlite3.Row
    conn.executescript(BRIDGE_SCHEMA)
    conn.commit()
    conn.path = path  # type: ignore[attr-defined]  # тестам нужен путь для connect()
    return conn


def add_msg(conn, chat_id, ts, direction="in", text="hi", sender_name="Контакт",
            media_type=None, is_auto=0):
    conn.execute(
        "INSERT INTO messages (chat_id, ts, direction, is_auto, media_type, text,"
        " sender_name, raw_json) VALUES (?, ?, ?, ?, ?, ?, ?, '{}')",
        (chat_id, ts, direction, is_auto, media_type, text, sender_name),
    )
    conn.commit()
