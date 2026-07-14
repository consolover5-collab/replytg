import sqlite3

import pytest

from replytg import db
from replytg.daemon import Deps, check_owner_connection, ensure_cursor, recover_generating
from replytg.suggest import SuggestError
from replytg.waves import Generate, RepeatCard, WaveConfig, WaveEngine
from tests.conftest import BRIDGE_SCHEMA, add_msg


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.on_send = None

    async def send_message(self, chat_id, text, reply_markup=None,
                           reply_to_message_id=None):
        self.sent.append((chat_id, text))
        message_id = 900 + len(self.sent)
        if self.on_send is not None:
            self.on_send(message_id)

        class M:
            pass

        result = M()
        result.message_id = message_id
        return result

    async def edit_message_text(self, **kw):
        self.edits.append(("text", kw.get("message_id")))

    async def edit_message_reply_markup(self, **kw):
        self.edits.append(("kb", kw.get("message_id")))


class FakeSettings:
    owner_id = 42
    llm_model = "m"
    llm_base_url = "http://x"
    llm_api_key = "k"
    llm_timeout_sec = 1.0
    history_limit = 30
    max_variant_len = 1000
    variant_count = 2
    draft_wait_timeout_sec = 1
    chat_blocklist = []


def make_deps(tmp_path, repeat_max_count=1):
    bridge = sqlite3.connect(tmp_path / "bridge.db")
    bridge.row_factory = sqlite3.Row
    bridge.executescript(BRIDGE_SCHEMA)
    engine = WaveEngine(db.connect(tmp_path / "r.db"),
                        WaveConfig(600, 3600, 7200, repeat_max_count))

    async def fake_generate(chat_id, wave_started_ts, wave_rows):
        return ["в1", "в2"]

    deps = Deps(settings=FakeSettings(), engine=engine, bot=FakeBot(),
                bridge_ro=bridge, bridge_rw=bridge,
                style_profile="", now=lambda: 10_000)
    deps.generate_fn = fake_generate
    return deps, bridge


def test_first_run_initializes_cursor_to_tail(tmp_path):
    deps, bridge = make_deps(tmp_path)
    for i in range(3):
        add_msg(bridge, chat_id=1, ts=100 + i, text=f"старое {i}")
    ensure_cursor(deps.engine.conn, bridge)
    assert db.get_cursor(deps.engine.conn) == 3
    deps.scan_bridge(now=10_000)
    assert deps.engine.current(1) is None          # история не открыла волну
    ensure_cursor(deps.engine.conn, bridge)        # повторный вызов не сбрасывает
    add_msg(bridge, chat_id=1, ts=200)
    assert db.get_cursor(deps.engine.conn) == 3    # курсор не перепрыгнул новое


async def test_generate_action_sends_card_from_snapshot(tmp_path):
    deps, bridge = make_deps(tmp_path)
    add_msg(bridge, chat_id=7, ts=9000, text="вопрос?")
    deps.engine.note_incoming(7, ts=9000, now=9000)
    actions = deps.engine.tick(now=9600)
    assert actions == [Generate(7, 9000, 1)]
    await deps.process_actions(actions)
    await deps.drain()
    assert deps.bot.sent and deps.bot.sent[0][0] == 42
    assert "вопрос?" in deps.bot.sent[0][1]
    assert deps.engine.current(7)["state"] == "awaiting"


async def test_generation_failure_resets_chat(tmp_path):
    deps, bridge = make_deps(tmp_path)

    async def boom(chat_id, wave_started_ts, wave_rows):
        raise SuggestError("недоступен")

    deps.generate_fn = boom
    add_msg(bridge, chat_id=7, ts=9000)
    deps.engine.note_incoming(7, ts=9000, now=9000)
    await deps.process_actions(deps.engine.tick(now=9600))
    await deps.drain()
    assert deps.bot.sent == []
    assert deps.engine.current(7)["state"] == "idle"


async def test_send_failure_does_not_stick_generating(tmp_path):
    """Ошибка Telegram при отправке карточки не оставляет чат в generating навечно."""
    deps, bridge = make_deps(tmp_path)

    class BoomBot(FakeBot):
        async def send_message(self, *a, **kw):
            raise RuntimeError("telegram down")

    deps.bot = BoomBot()
    add_msg(bridge, chat_id=7, ts=9000)
    deps.engine.note_incoming(7, ts=9000, now=9000)
    await deps.process_actions(deps.engine.tick(now=9600))
    await deps.drain()
    assert deps.engine.current(7)["state"] == "idle"


async def test_scan_skips_auto_outgoing(tmp_path):
    """Авто-ответ бриджа (away/offline, is_auto=1) не считается ручным ответом."""
    deps, bridge = make_deps(tmp_path)
    add_msg(bridge, chat_id=7, ts=9000, direction="in")
    add_msg(bridge, chat_id=7, ts=9001, direction="out", is_auto=1)
    deps.scan_bridge(now=9010)
    assert deps.engine.current(7)["state"] == "collecting"   # волна жива


async def test_scan_routes_directions(tmp_path):
    deps, bridge = make_deps(tmp_path)
    add_msg(bridge, chat_id=7, ts=9000, direction="in")
    add_msg(bridge, chat_id=8, ts=9001, direction="in")
    add_msg(bridge, chat_id=8, ts=9002, direction="out")
    deps.scan_bridge(now=9010)
    assert deps.engine.current(7)["state"] == "collecting"
    assert deps.engine.current(8)["state"] == "idle"      # сам ответил
    assert db.get_cursor(deps.engine.conn) == 3


async def test_pending_from_silence_reaches_card(tmp_path):
    deps, bridge = make_deps(tmp_path)
    add_msg(bridge, chat_id=7, ts=1000, text="первое")
    deps.engine.note_incoming(7, ts=1000, now=1000)
    await deps.process_actions(deps.engine.tick(now=1600))
    await deps.drain()
    assert deps.engine.note_used(7, gen_id=1, now=1700)   # тишина до 5300
    add_msg(bridge, chat_id=7, ts=2000, text="из тишины")
    deps.engine.note_incoming(7, ts=2000, now=2000)
    deps.engine.tick(now=5300)                            # тишина кончилась
    actions = deps.engine.tick(now=5301)                  # окно волны уже истекло
    assert actions == [Generate(7, wave_started_ts=2000, gen_id=2)]
    await deps.process_actions(actions)
    await deps.drain()
    assert "из тишины" in deps.bot.sent[-1][1]            # контекст тишины не потерян


def test_recover_generating(tmp_path):
    deps, _ = make_deps(tmp_path)
    db.set_chat_state(deps.engine.conn, 5, state="generating", wave_started_ts=100, gen_id=2)
    recover_generating(deps.engine.conn)
    st = deps.engine.current(5)
    assert st["state"] == "collecting" and st["wave_started_ts"] == 100


def test_check_owner_connection(tmp_path):
    deps, bridge = make_deps(tmp_path)
    with pytest.raises(SystemExit, match="подключ"):
        check_owner_connection(bridge, 42)                 # нет enabled
    bridge.execute("INSERT INTO connections VALUES ('c1', 42, '{}', 1, 0)")
    bridge.commit()
    check_owner_connection(bridge, 42)                     # ок
    with pytest.raises(SystemExit, match="owner"):
        check_owner_connection(bridge, 43)                 # чужой owner
    bridge.execute("INSERT INTO connections VALUES ('c2', 42, '{}', 1, 1)")
    bridge.commit()
    with pytest.raises(SystemExit, match="ровно одно"):
        check_owner_connection(bridge, 42)                 # два enabled


async def test_regenerate_edit_failure_keeps_old_generation(tmp_path):
    deps, _ = make_deps(tmp_path)
    deps.engine.note_incoming(7, ts=9000, now=9000)
    await deps.process_actions(deps.engine.tick(9600))
    await deps.drain()
    before = deps.engine.current(7)

    class BrokenCard:
        message_id = before["card_message_id"]

        async def edit_text(self, **kwargs):
            raise RuntimeError("telegram down")

    await deps.regenerate(7, BrokenCard())
    after = deps.engine.current(7)
    assert after["gen_id"] == before["gen_id"]
    assert deps.engine.variants(7) == ["в1", "в2"]
    assert after["repeat_at_ts"] is not None


async def test_rejected_repeat_keeps_current_card_active(tmp_path):
    deps, bridge = make_deps(tmp_path)
    add_msg(bridge, chat_id=7, ts=9000, text="вопрос")
    deps.engine.note_incoming(7, ts=9000, now=9000)
    await deps.process_actions(deps.engine.tick(9600))
    await deps.drain()
    old_card = deps.engine.current(7)["card_message_id"]

    actions = deps.engine.tick(17_200)
    assert actions == [RepeatCard(7, gen_id=1)]

    def replace_current_card(_new_message_id):
        db.set_chat_state(deps.engine.conn, 7, card_message_id=999)

    deps.bot.on_send = replace_current_card
    await deps.process_actions(actions)

    new_message_id = 902
    assert ("kb", new_message_id) in deps.bot.edits
    assert ("kb", 999) not in deps.bot.edits
    assert ("kb", old_card) not in deps.bot.edits      # старую карточку не трогали
    assert deps.engine.current(7)["card_message_id"] == 999


async def test_repeat_happy_path_updates_card_and_schedule(tmp_path):
    deps, bridge = make_deps(tmp_path, repeat_max_count=2)
    add_msg(bridge, chat_id=7, ts=9000, text="вопрос")
    deps.engine.note_incoming(7, ts=9000, now=9000)
    await deps.process_actions(deps.engine.tick(9600))
    await deps.drain()
    old_card = deps.engine.current(7)["card_message_id"]

    actions = deps.engine.tick(17_200)
    assert actions == [RepeatCard(7, gen_id=1)]
    await deps.process_actions(actions)

    assert deps.bot.sent[-1][1].startswith("🔁 Напоминаю:")
    st = deps.engine.current(7)
    assert st["repeat_count"] == 1
    assert st["card_message_id"] != old_card           # текущая карточка — новая
    assert st["repeat_at_ts"] == 10_000 + 7200         # лимит 2 не достигнут — следующий назначен
    assert ("kb", old_card) in deps.bot.edits          # старая клавиатура снята


async def test_unexpected_error_rearms_repeat_timer(tmp_path):
    deps, _ = make_deps(tmp_path)
    deps.engine.note_incoming(7, ts=9000, now=9000)
    await deps.process_actions(deps.engine.tick(9600))
    await deps.drain()
    before = deps.engine.current(7)

    async def boom(chat_id, wave_started_ts, wave_rows):
        raise RuntimeError("непредвиденное")

    deps.generate_fn = boom

    class Card:
        message_id = before["card_message_id"]

    with pytest.raises(RuntimeError):
        await deps.regenerate(7, Card())
    after = deps.engine.current(7)
    assert after["gen_id"] == before["gen_id"]
    assert after["repeat_at_ts"] is not None           # таймер перевооружён
    assert 7 not in deps._inflight_regen
