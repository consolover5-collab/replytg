"""Машина состояний волн и кулдаунов. Чистая логика: время приходит параметром,
side-эффекты (LLM, Telegram) возвращаются наружу списком Action'ов."""
import json
import sqlite3
from dataclasses import dataclass

from replytg import db


@dataclass(frozen=True)
class WaveConfig:
    wave_window_sec: int
    used_silence_sec: int
    repeat_after_sec: int
    repeat_max_count: int


@dataclass(frozen=True)
class Generate:
    chat_id: int
    wave_started_ts: int
    gen_id: int


@dataclass(frozen=True)
class RepeatCard:
    chat_id: int
    gen_id: int


@dataclass(frozen=True)
class CloseCard:
    chat_id: int
    card_message_id: int
    reason: str  # 'answered' | 'new_wave'


Action = Generate | RepeatCard | CloseCard


class WaveEngine:
    def __init__(self, conn: sqlite3.Connection, cfg: WaveConfig) -> None:
        self.conn = conn
        self.cfg = cfg

    def current(self, chat_id: int) -> sqlite3.Row | None:
        return db.get_chat_state(self.conn, chat_id)

    # --- события из bridge.db ---

    def note_incoming(self, chat_id: int, ts: int, now: int) -> list[Action]:
        st = self.current(chat_id)
        state = st["state"] if st else "idle"
        if state in ("idle",):
            db.set_chat_state(self.conn, chat_id, state="collecting",
                              wave_started_ts=ts, repeat_at_ts=None,
                              repeat_count=0, pending_incoming=0)
            return []
        if state == "collecting":
            return []  # копится в текущую волну
        if state == "generating":
            # LLM уже получил старый снапшот — его карточка отобьётся guard'ом
            # note_card_sent; волна перезапускается от этого сообщения
            db.set_chat_state(self.conn, chat_id, state="collecting", wave_started_ts=ts,
                              repeat_at_ts=None, repeat_count=0, card_message_id=None,
                              variants_json=None, pending_incoming=0)
            return []
        if state == "silence":
            if not st["pending_incoming"]:
                db.set_chat_state(self.conn, chat_id, pending_incoming=1,
                                  pending_since_ts=ts)
            return []
        # awaiting: счётчики в ноль, старая карточка устарела
        actions: list[Action] = []
        if st["card_message_id"] is not None:
            actions.append(CloseCard(chat_id, st["card_message_id"], reason="new_wave"))
        db.set_chat_state(self.conn, chat_id, state="collecting", wave_started_ts=ts,
                          repeat_at_ts=None, repeat_count=0, card_message_id=None,
                          variants_json=None, pending_incoming=0)
        return actions

    def note_outgoing(self, chat_id: int, now: int) -> list[Action]:
        st = self.current(chat_id)
        if st is None:
            return []
        state = st["state"]
        if state in ("collecting", "generating"):
            # владелец ответил сам — волна не нужна, LLM не зовём (спека)
            db.set_chat_state(self.conn, chat_id, state="idle",
                              wave_started_ts=None, repeat_at_ts=None, repeat_count=0)
            return []
        if state == "awaiting":
            actions: list[Action] = []
            if st["card_message_id"] is not None:
                actions.append(CloseCard(chat_id, st["card_message_id"], reason="answered"))
            db.set_chat_state(self.conn, chat_id, state="idle", repeat_at_ts=None,
                              repeat_count=0, card_message_id=None, variants_json=None)
            return actions
        return []  # idle | silence (в т.ч. наш собственный approve-драфт)

    # --- периодический тик ---

    def tick(self, now: int) -> list[Action]:
        actions: list[Action] = []
        for st in db.list_chat_states(self.conn):
            chat_id, state = st["chat_id"], st["state"]
            if state == "collecting" and now >= st["wave_started_ts"] + self.cfg.wave_window_sec:
                gen_id = st["gen_id"] + 1
                db.set_chat_state(self.conn, chat_id, state="generating", gen_id=gen_id)
                actions.append(Generate(chat_id, st["wave_started_ts"], gen_id))
            elif state == "awaiting" and st["repeat_at_ts"] is not None and now >= st["repeat_at_ts"]:
                db.set_chat_state(self.conn, chat_id, repeat_at_ts=None)
                actions.append(RepeatCard(chat_id, st["gen_id"]))
            elif state == "silence" and now >= (st["silence_until_ts"] or 0):
                if st["pending_incoming"]:
                    # волна открывается от ПЕРВОГО накопленного в тишине сообщения —
                    # иначе wave_incoming не увидит контекст, собранный за тишину
                    db.set_chat_state(self.conn, chat_id, state="collecting",
                                      wave_started_ts=st["pending_since_ts"],
                                      pending_incoming=0, pending_since_ts=None,
                                      silence_until_ts=None)
                else:
                    db.set_chat_state(self.conn, chat_id, state="idle", silence_until_ts=None)
        return actions

    # --- обратная связь от демона/кнопок ---

    def _next_repeat(self, now: int, repeat_count: int) -> int | None:
        """Когда повторить карточку в следующий раз; None — лимит повторов исчерпан
        (в т.ч. когда repeat_max_count=0 — повторы выключены совсем)."""
        if repeat_count >= self.cfg.repeat_max_count:
            return None
        return now + self.cfg.repeat_after_sec

    def _current_card(self, chat_id: int, gen_id: int,
                      card_message_id: int) -> sqlite3.Row | None:
        """Строка чата, только если это ВСЁ ЕЩЁ та же awaiting-карточка (та же
        генерация И тот же message_id). Иначе None — пока вызывающий держал карточку
        в руках, её успели сменить (новый повтор, 🔄, новая волна)."""
        st = self.current(chat_id)
        if (
            st is None
            or st["state"] != "awaiting"
            or st["gen_id"] != gen_id
            or st["card_message_id"] != card_message_id
        ):
            return None
        return st

    def pause_repeat(self, chat_id: int, gen_id: int, card_message_id: int) -> bool:
        """Снять таймер повтора на время регенерации: тик не должен родить RepeatCard,
        пока крутится 🔄. Обязателен парный resume_repeat/note_variants на выходе."""
        if self._current_card(chat_id, gen_id, card_message_id) is None:
            return False
        db.set_chat_state(self.conn, chat_id, repeat_at_ts=None)
        return True

    def resume_repeat(self, chat_id: int, gen_id: int,
                      card_message_id: int, now: int) -> bool:
        """Вернуть таймер повтора (регенерация не удалась). CAS по карточке: если она
        уже сменилась — no-op, чужой цикл не трогаем."""
        st = self._current_card(chat_id, gen_id, card_message_id)
        if st is None:
            return False
        db.set_chat_state(
            self.conn, chat_id,
            repeat_at_ts=self._next_repeat(now, st["repeat_count"]),
        )
        return True

    def note_card_sent(self, chat_id: int, gen_id: int, card_message_id: int,
                       variants: list[str], now: int) -> bool:
        """Фиксирует отправленную карточку исходной генерации. False, если генерация
        устарела (владелец успел ответить сам, пока LLM думал) — карточку надо закрыть."""
        st = self.current(chat_id)
        if st is None or st["state"] != "generating" or st["gen_id"] != gen_id:
            return False
        db.set_chat_state(
            self.conn, chat_id,
            state="awaiting",
            card_message_id=card_message_id,
            variants_json=json.dumps(variants, ensure_ascii=False),
            repeat_count=0,
            repeat_at_ts=self._next_repeat(now, 0),
        )
        return True

    def note_repeat_sent(self, chat_id: int, gen_id: int,
                         expected_card_message_id: int,
                         new_card_message_id: int, now: int) -> bool:
        """Фиксирует отправленную повторную карточку. CAS по card_message_id: запоздавший
        повтор не должен подменить карточку, которая уже сменилась (новый повтор,
        🔄 или новая волна)."""
        st = self.current(chat_id)
        if (
            st is None
            or st["state"] != "awaiting"
            or st["gen_id"] != gen_id
            or st["card_message_id"] != expected_card_message_id
        ):
            return False
        repeat_count = st["repeat_count"] + 1
        db.set_chat_state(
            self.conn, chat_id,
            card_message_id=new_card_message_id,
            repeat_count=repeat_count,
            repeat_at_ts=self._next_repeat(now, repeat_count),
        )
        return True

    def note_generation_failed(self, chat_id: int, gen_id: int) -> None:
        """Провал генерации. Guard: только если чат всё ещё в generating ЭТОЙ генерации —
        упавший старый запрос не должен убивать волну, перезапущенную новым входящим."""
        st = self.current(chat_id)
        if st is None or st["state"] != "generating" or st["gen_id"] != gen_id:
            return
        db.set_chat_state(self.conn, chat_id, state="idle", wave_started_ts=None)

    def note_used(self, chat_id: int, gen_id: int, now: int) -> bool:
        """✅ вариант или ✍️ свой ответ: час тишины. Guard: только из awaiting
        актуальной генерации — запоздавший клик по закрытой карточке не должен
        глотать новую волну."""
        st = self.current(chat_id)
        if st is None or st["state"] != "awaiting" or st["gen_id"] != gen_id:
            return False
        db.set_chat_state(self.conn, chat_id, state="silence",
                          silence_until_ts=now + self.cfg.used_silence_sec,
                          repeat_at_ts=None, repeat_count=0, variants_json=None)
        return True

    def note_dismissed(self, chat_id: int, gen_id: int) -> bool:
        st = self.current(chat_id)
        if st is None or st["state"] != "awaiting" or st["gen_id"] != gen_id:
            return False
        db.set_chat_state(self.conn, chat_id, state="idle", repeat_at_ts=None,
                          repeat_count=0, card_message_id=None, variants_json=None)
        return True

    def note_variants(self, chat_id: int, variants: list[str],
                      expected_gen_id: int, expected_card_message_id: int,
                      now: int) -> int | None:
        """🔄: новые варианты в существующей карточке, gen_id++ (старые кнопки протухают)
        и цикл повтора начинается заново. CAS по gen_id И card_message_id: за время await
        LLM могла появиться новая волна/карточка или прилететь повтор — запоздавший
        результат не должен перезаписать чужую карточку."""
        if self._current_card(
            chat_id, expected_gen_id, expected_card_message_id,
        ) is None:
            return None
        gen_id = expected_gen_id + 1
        db.set_chat_state(
            self.conn, chat_id,
            gen_id=gen_id,
            variants_json=json.dumps(variants, ensure_ascii=False),
            repeat_count=0,
            repeat_at_ts=self._next_repeat(now, 0),
        )
        return gen_id

    def variants(self, chat_id: int) -> list[str]:
        st = self.current(chat_id)
        if st is None or not st["variants_json"]:
            return []
        return json.loads(st["variants_json"])
