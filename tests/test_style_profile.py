from replytg.style_profile import extract_pairs
from tests.conftest import add_msg


def test_extract_pairs_incoming_then_outgoing(bridge_db):
    add_msg(bridge_db, 1, ts=10, direction="in", text="Привет, как дела?")
    add_msg(bridge_db, 1, ts=20, direction="out", text="норм, ты как")
    add_msg(bridge_db, 1, ts=30, direction="out", text="сорян, занят был")  # без входящего
    add_msg(bridge_db, 2, ts=40, direction="in", text="Созвон завтра?")
    add_msg(bridge_db, 2, ts=50, direction="out", text="давай после 15")
    pairs = extract_pairs(bridge_db, min_len=3)
    assert ("Привет, как дела?", "норм, ты как") in pairs
    assert ("Созвон завтра?", "давай после 15") in pairs
    assert len(pairs) == 2


def test_extract_pairs_skips_auto_and_short(bridge_db):
    add_msg(bridge_db, 1, ts=10, direction="in", text="Ты тут?")
    add_msg(bridge_db, 1, ts=20, direction="out", text="ок")                # короткое
    add_msg(bridge_db, 1, ts=30, direction="in", text="Точно тут?")
    add_msg(bridge_db, 1, ts=40, direction="out", text="автоответ бота", is_auto=1)
    assert extract_pairs(bridge_db, min_len=3) == []


def test_extract_pairs_excludes_sent_drafts(bridge_db):
    """Ответы, отправленные через replytg-драфты, не учат стиль (анти-feedback-loop)."""
    add_msg(bridge_db, 1, ts=10, direction="in", text="Как дела?")
    add_msg(bridge_db, 1, ts=20, direction="out", text="сгенерировано моделью")
    bridge_db.execute(
        "INSERT INTO drafts (chat_id, text, status, created_ts) "
        "VALUES (1, 'сгенерировано моделью', 'sent', 15)")
    bridge_db.commit()
    assert extract_pairs(bridge_db, min_len=3) == []


def test_extract_pairs_balances_chats(bridge_db):
    for i in range(5):
        add_msg(bridge_db, 1, ts=100 + i * 10, direction="in", text=f"вопрос {i}")
        add_msg(bridge_db, 1, ts=105 + i * 10, direction="out", text=f"ответ один {i}")
    add_msg(bridge_db, 2, ts=200, direction="in", text="эй")
    add_msg(bridge_db, 2, ts=205, direction="out", text="ответ два")
    pairs = extract_pairs(bridge_db, per_chat=2)
    ours = [b for _, b in pairs]
    assert ours.count("ответ два") == 1
    assert sum(1 for b in ours if b.startswith("ответ один")) == 2   # баланс, не 5
    assert "ответ один 4" in ours and "ответ один 3" in ours          # именно свежие
    assert pairs[-1][1] == "ответ два"                                # хронология
