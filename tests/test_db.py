from replytg import db


def test_cursor_roundtrip(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    assert db.get_cursor(conn) == 0
    db.set_cursor(conn, 41)
    db.set_cursor(conn, 42)
    assert db.get_cursor(conn) == 42


def test_chat_state_upsert_and_get(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    assert db.get_chat_state(conn, 100) is None
    db.set_chat_state(conn, 100, state="collecting", wave_started_ts=10)
    row = db.get_chat_state(conn, 100)
    assert row["state"] == "collecting" and row["wave_started_ts"] == 10
    # частичный апдейт не затирает прочие поля
    db.set_chat_state(conn, 100, gen_id=3)
    row = db.get_chat_state(conn, 100)
    assert row["state"] == "collecting" and row["gen_id"] == 3


def test_list_chat_states(tmp_path):
    conn = db.connect(tmp_path / "r.db")
    db.set_chat_state(conn, 1, state="awaiting")
    db.set_chat_state(conn, 2, state="silence")
    states = {r["chat_id"]: r["state"] for r in db.list_chat_states(conn)}
    assert states == {1: "awaiting", 2: "silence"}
