from replytg import db
from replytg.waves import CloseCard, Generate, RepeatCard, WaveConfig, WaveEngine

CFG = WaveConfig(wave_window_sec=600, used_silence_sec=3600, repeat_after_sec=7200)


def engine(tmp_path):
    return WaveEngine(db.connect(tmp_path / "r.db"), CFG)


def to_awaiting(e, chat_id=1, t0=1000):
    """Хелпер: прогоняет чат до awaiting-карточки. Возвращает время отправки карточки."""
    e.note_incoming(chat_id, ts=t0, now=t0)
    acts = e.tick(now=t0 + 600)
    assert acts == [Generate(chat_id, wave_started_ts=t0, gen_id=1)]
    ok = e.note_card_sent(chat_id, gen_id=1, card_message_id=555,
                          variants=["в1", "в2"], allow_repeat=True, now=t0 + 610)
    assert ok
    return t0 + 610


def test_incoming_opens_wave_tick_generates(tmp_path):
    e = engine(tmp_path)
    assert e.note_incoming(1, ts=1000, now=1000) == []
    assert e.tick(now=1300) == []                       # окно ещё не закрылось
    assert e.tick(now=1600) == [Generate(1, wave_started_ts=1000, gen_id=1)]
    assert e.current(1)["state"] == "generating"
    assert e.tick(now=1700) == []                       # без дублей


def test_owner_reply_during_collecting_drops_wave(tmp_path):
    e = engine(tmp_path)
    e.note_incoming(1, ts=1000, now=1000)
    assert e.note_outgoing(1, now=1100) == []
    assert e.current(1)["state"] == "idle"
    assert e.tick(now=1600) == []                       # LLM не зовётся


def test_repeat_once_then_quiet(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    assert e.tick(now=sent + 7100) == []
    assert e.tick(now=sent + 7200) == [RepeatCard(1, gen_id=1)]
    e.note_card_sent(1, gen_id=1, card_message_id=556,
                     variants=["в1", "в2"], allow_repeat=False, now=sent + 7201)
    assert e.tick(now=sent + 99999) == []               # «дальше тишина»


def test_used_then_silence_then_pending_wave(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    e.note_used(1, now=sent + 60)
    st = e.current(1)
    assert st["state"] == "silence" and st["silence_until_ts"] == sent + 60 + 3600
    assert e.note_incoming(1, ts=sent + 100, now=sent + 100) == []   # копится
    assert e.current(1)["pending_incoming"] == 1
    assert e.tick(now=sent + 60 + 3600) == []           # тишина кончилась → новая волна
    st = e.current(1)
    assert st["state"] == "collecting" and st["wave_started_ts"] == sent + 60 + 3600


def test_silence_without_pending_goes_idle(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    e.note_used(1, now=sent)
    e.tick(now=sent + 3600)
    assert e.current(1)["state"] == "idle"


def test_new_incoming_during_awaiting_restarts_wave(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    acts = e.note_incoming(1, ts=sent + 50, now=sent + 50)
    assert acts == [CloseCard(1, card_message_id=555, reason="new_wave")]
    st = e.current(1)
    assert st["state"] == "collecting" and st["repeat_at_ts"] is None


def test_manual_reply_during_awaiting_closes_card(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    acts = e.note_outgoing(1, now=sent + 50)
    assert acts == [CloseCard(1, card_message_id=555, reason="answered")]
    assert e.current(1)["state"] == "idle"


def test_dismiss_cancels_repeat(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    e.note_dismissed(1)
    assert e.current(1)["state"] == "idle"
    assert e.tick(now=sent + 99999) == []


def test_card_sent_guard_after_manual_reply(tmp_path):
    e = engine(tmp_path)
    e.note_incoming(1, ts=1000, now=1000)
    e.tick(now=1600)                                    # generating
    e.note_outgoing(1, now=1601)                        # ответил, пока LLM думал
    ok = e.note_card_sent(1, gen_id=1, card_message_id=9,
                          variants=["a", "b"], allow_repeat=True, now=1602)
    assert not ok                                       # карточка не нужна


def test_regenerate_bumps_gen_id(tmp_path):
    e = engine(tmp_path)
    to_awaiting(e)
    new_gen = e.note_variants(1, ["н1", "н2"])
    assert new_gen == 2
    assert e.current(1)["gen_id"] == 2
