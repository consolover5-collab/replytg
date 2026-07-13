import pytest

from replytg import bridge_reader
from tests.conftest import add_msg


def test_check_schema_ok(bridge_db):
    bridge_reader.check_schema(bridge_db)  # не бросает


def test_check_schema_missing_column(bridge_db):
    bridge_db.execute("ALTER TABLE drafts DROP COLUMN card_message_id")
    with pytest.raises(SystemExit, match="drafts"):
        bridge_reader.check_schema(bridge_db)


def test_fetch_new_respects_cursor(bridge_db):
    add_msg(bridge_db, 1, ts=10)
    add_msg(bridge_db, 1, ts=11, direction="out")
    add_msg(bridge_db, 2, ts=12)
    rows = bridge_reader.fetch_new(bridge_db, after_rowid=1)
    assert [r["id"] for r in rows] == [2, 3]
    assert rows[0]["direction"] == "out"


def test_history_last_n_ascending(bridge_db):
    for i in range(5):
        add_msg(bridge_db, 7, ts=100 + i, text=f"m{i}")
    hist = bridge_reader.history(bridge_db, 7, limit=3)
    assert [h["text"] for h in hist] == ["m2", "m3", "m4"]


def test_wave_incoming_filters(bridge_db):
    add_msg(bridge_db, 7, ts=50, text="старое")
    add_msg(bridge_db, 7, ts=100, text="раз")
    add_msg(bridge_db, 7, ts=101, direction="out", text="моё")
    add_msg(bridge_db, 7, ts=102, text=None, media_type="photo")
    wave = bridge_reader.wave_incoming(bridge_db, 7, since_ts=100)
    assert [w["text"] for w in wave] == ["раз", None]


def test_history_before_ts_excludes_wave(bridge_db):
    add_msg(bridge_db, 7, ts=100, text="старое")
    add_msg(bridge_db, 7, ts=200, text="волна")
    hist = bridge_reader.history(bridge_db, 7, limit=10, before_ts=200)
    assert [h["text"] for h in hist] == ["старое"]


def test_max_message_id(bridge_db):
    assert bridge_reader.max_message_id(bridge_db) == 0
    add_msg(bridge_db, 1, ts=10)
    add_msg(bridge_db, 1, ts=11)
    assert bridge_reader.max_message_id(bridge_db) == 2


def test_has_enabled_connection(bridge_db):
    assert not bridge_reader.has_enabled_connection(bridge_db)
    bridge_db.execute(
        "INSERT INTO connections VALUES ('c1', 5, '{}', 1, 0)")
    bridge_db.commit()
    assert bridge_reader.has_enabled_connection(bridge_db)
