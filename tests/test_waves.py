from replytg import db
from replytg.daemon import recover_generating
from replytg.waves import CloseCard, Generate, RepeatCard, WaveConfig, WaveEngine

CFG = WaveConfig(
    wave_window_sec=600,
    used_silence_sec=3600,
    repeat_after_sec=7200,
    repeat_max_count=2,
)


def engine(tmp_path):
    return WaveEngine(db.connect(tmp_path / "r.db"), CFG)


def to_awaiting(e, chat_id=1, t0=1000):
    """Хелпер: прогоняет чат до awaiting-карточки. Возвращает время отправки карточки."""
    e.note_incoming(chat_id, ts=t0, now=t0)
    acts = e.tick(now=t0 + 600)
    assert acts == [Generate(chat_id, wave_started_ts=t0, gen_id=1)]
    ok = e.note_card_sent(chat_id, gen_id=1, card_message_id=555,
                          variants=["в1", "в2"], now=t0 + 610)
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


def test_repeat_respects_configured_count(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)

    assert e.tick(sent + 7200) == [RepeatCard(1, gen_id=1)]
    assert e.note_repeat_sent(1, 1, expected_card_message_id=555,
                              new_card_message_id=556, now=sent + 7201)
    assert e.current(1)["repeat_count"] == 1

    assert e.tick(sent + 7201 + 7200) == [RepeatCard(1, gen_id=1)]
    assert e.note_repeat_sent(1, 1, expected_card_message_id=556,
                              new_card_message_id=557, now=sent + 7202 + 7200)
    assert e.current(1)["repeat_count"] == 2
    assert e.current(1)["repeat_at_ts"] is None


def test_zero_repeat_limit_does_not_schedule(tmp_path):
    cfg = WaveConfig(600, 3600, 7200, repeat_max_count=0)
    e = WaveEngine(db.connect(tmp_path / "r.db"), cfg)
    e.note_incoming(1, ts=1000, now=1000)
    e.tick(1600)
    assert e.note_card_sent(1, 1, 555, ["а", "б"], now=1610)
    assert e.current(1)["repeat_at_ts"] is None


def test_stale_repeat_cannot_replace_current_card(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    e.tick(sent + 7200)
    assert not e.note_repeat_sent(1, 1, expected_card_message_id=999,
                                  new_card_message_id=556, now=sent + 7201)
    assert e.current(1)["card_message_id"] == 555


def test_repeat_limit_survives_restart(tmp_path):
    """Критерий приёмки: не больше N повторов, включая после рестарта процесса."""
    e = engine(tmp_path)
    sent = to_awaiting(e)
    assert e.tick(sent + 7200) == [RepeatCard(1, gen_id=1)]
    assert e.note_repeat_sent(1, 1, expected_card_message_id=555,
                              new_card_message_id=556, now=sent + 7201)

    # «рестарт демона»: новое соединение к тому же файлу + штатный recovery
    e2 = WaveEngine(db.connect(tmp_path / "r.db"), CFG)
    recover_generating(e2.conn)
    st = e2.current(1)
    assert st["state"] == "awaiting" and st["repeat_count"] == 1  # цикл пережил рестарт

    assert e2.tick(sent + 7201 + 7200) == [RepeatCard(1, gen_id=1)]
    assert e2.note_repeat_sent(1, 1, expected_card_message_id=556,
                               new_card_message_id=557, now=sent + 7202 + 7200)
    st = e2.current(1)
    assert st["repeat_count"] == 2 and st["repeat_at_ts"] is None  # лимит исчерпан
    assert e2.tick(sent + 99_999) == []  # больше никогда


def test_used_then_silence_then_pending_wave(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    e.note_used(1, gen_id=1, now=sent + 60)
    st = e.current(1)
    assert st["state"] == "silence" and st["silence_until_ts"] == sent + 60 + 3600
    assert e.note_incoming(1, ts=sent + 100, now=sent + 100) == []   # копится
    assert e.current(1)["pending_incoming"] == 1
    assert e.tick(now=sent + 60 + 3600) == []           # тишина кончилась → новая волна
    st = e.current(1)
    assert st["state"] == "collecting" and st["wave_started_ts"] == sent + 100


def test_second_pending_keeps_first_ts(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    e.note_used(1, gen_id=1, now=sent)
    e.note_incoming(1, ts=sent + 100, now=sent + 100)
    e.note_incoming(1, ts=sent + 200, now=sent + 200)
    assert e.current(1)["pending_since_ts"] == sent + 100
    e.tick(now=sent + 3600)
    assert e.current(1)["wave_started_ts"] == sent + 100


def test_incoming_during_generating_restarts_wave(tmp_path):
    e = engine(tmp_path)
    e.note_incoming(1, ts=1000, now=1000)
    assert e.tick(now=1600) == [Generate(1, wave_started_ts=1000, gen_id=1)]
    e.note_incoming(1, ts=1650, now=1650)               # пришло, пока LLM думал
    assert e.current(1)["state"] == "collecting"
    ok = e.note_card_sent(1, gen_id=1, card_message_id=9,
                          variants=["a", "b"], now=1660)
    assert not ok                                       # устаревшая карточка отбита
    assert e.tick(now=1650 + 600) == [Generate(1, wave_started_ts=1650, gen_id=2)]


def test_silence_without_pending_goes_idle(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    e.note_used(1, gen_id=1, now=sent)
    e.tick(now=sent + 3600)
    assert e.current(1)["state"] == "idle"


def test_new_incoming_during_awaiting_restarts_wave(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    acts = e.note_incoming(1, ts=sent + 50, now=sent + 50)
    assert acts == [CloseCard(1, card_message_id=555, reason="new_wave")]
    st = e.current(1)
    assert st["state"] == "collecting" and st["repeat_at_ts"] is None
    assert st["repeat_count"] == 0


def test_manual_reply_during_awaiting_closes_card(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    acts = e.note_outgoing(1, now=sent + 50)
    assert acts == [CloseCard(1, card_message_id=555, reason="answered")]
    assert e.current(1)["state"] == "idle"


def test_dismiss_cancels_repeat(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    e.note_dismissed(1, gen_id=1)
    assert e.current(1)["state"] == "idle"
    assert e.tick(now=sent + 99999) == []


def test_card_sent_guard_after_manual_reply(tmp_path):
    e = engine(tmp_path)
    e.note_incoming(1, ts=1000, now=1000)
    e.tick(now=1600)                                    # generating
    e.note_outgoing(1, now=1601)                        # ответил, пока LLM думал
    ok = e.note_card_sent(1, gen_id=1, card_message_id=9,
                          variants=["a", "b"], now=1602)
    assert not ok                                       # карточка не нужна


def test_regenerate_bumps_gen_id(tmp_path):
    e = engine(tmp_path)
    to_awaiting(e)
    new_gen = e.note_variants(1, ["н1", "н2"], expected_gen_id=1)
    assert new_gen == 2
    assert e.current(1)["gen_id"] == 2


def test_regenerate_stale_gen_rejected(tmp_path):
    """Запоздавший результат 🔄 не перезаписывает новую пару (CAS по gen_id)."""
    e = engine(tmp_path)
    to_awaiting(e)
    e.note_variants(1, ["с1", "с2"], expected_gen_id=1)   # первый 🔄: gen 2
    assert e.note_variants(1, ["поздний1", "поздний2"], expected_gen_id=1) is None
    assert e.variants(1) == ["с1", "с2"]


def test_generation_failed_guarded_by_gen(tmp_path):
    """Упавшая старая генерация не убивает волну, перезапущенную новым входящим."""
    e = engine(tmp_path)
    e.note_incoming(1, ts=1000, now=1000)
    assert e.tick(now=1600) == [Generate(1, wave_started_ts=1000, gen_id=1)]
    e.note_incoming(1, ts=1650, now=1650)                 # рестарт волны в generating
    e.note_generation_failed(1, gen_id=1)                 # старый запрос упал
    assert e.current(1)["state"] == "collecting"          # новая волна жива
    assert e.tick(now=1650 + 600) == [Generate(1, wave_started_ts=1650, gen_id=2)]


def test_stale_used_after_new_wave_rejected(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    e.note_incoming(1, ts=sent + 50, now=sent + 50)      # новая волна закрыла карточку
    assert e.note_used(1, gen_id=1, now=sent + 60) is False   # запоздавший клик
    assert e.current(1)["state"] == "collecting"          # волна не проглочена
    assert e.tick(now=sent + 50 + 600) == [Generate(1, wave_started_ts=sent + 50, gen_id=2)]


def test_stale_dismiss_after_new_wave_rejected(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    e.note_incoming(1, ts=sent + 50, now=sent + 50)
    assert e.note_dismissed(1, gen_id=1) is False
    assert e.current(1)["state"] == "collecting"


def test_double_used_second_click_rejected(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    assert e.note_used(1, gen_id=1, now=sent + 60) is True
    until = e.current(1)["silence_until_ts"]
    assert e.note_used(1, gen_id=1, now=sent + 160) is False  # дребезг ✅
    assert e.current(1)["silence_until_ts"] == until          # тишина не продлилась


def test_regenerate_outside_awaiting_returns_none(tmp_path):
    e = engine(tmp_path)
    sent = to_awaiting(e)
    e.note_outgoing(1, now=sent + 50)                     # карточка закрыта ответом
    assert e.note_variants(1, ["н1", "н2"], expected_gen_id=1) is None
    assert e.current(1)["gen_id"] == 1                    # не бампнулся
