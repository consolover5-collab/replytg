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
                              wave_started_ts=ts, repeat_at_ts=None, pending_incoming=0)
            return []
        if state in ("collecting", "generating"):
            return []  # копится в текущую волну
        if state == "silence":
            db.set_chat_state(self.conn, chat_id, pending_incoming=1)
            return []
        # awaiting: счётчики в ноль, старая карточка устарела
        actions: list[Action] = []
        if st["card_message_id"] is not None:
            actions.append(CloseCard(chat_id, st["card_message_id"], reason="new_wave"))
        db.set_chat_state(self.conn, chat_id, state="collecting", wave_started_ts=ts,
                          repeat_at_ts=None, card_message_id=None,
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
                              wave_started_ts=None, repeat_at_ts=None)
            return []
        if state == "awaiting":
            actions: list[Action] = []
            if st["card_message_id"] is not None:
                actions.append(CloseCard(chat_id, st["card_message_id"], reason="answered"))
            db.set_chat_state(self.conn, chat_id, state="idle", repeat_at_ts=None,
                              card_message_id=None, variants_json=None)
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
                    db.set_chat_state(self.conn, chat_id, state="collecting",
                                      wave_started_ts=now, pending_incoming=0,
                                      silence_until_ts=None)
                else:
                    db.set_chat_state(self.conn, chat_id, state="idle", silence_until_ts=None)
        return actions

    # --- обратная связь от демона/кнопок ---

    def note_card_sent(self, chat_id: int, gen_id: int, card_message_id: int,
                       variants: list[str], allow_repeat: bool, now: int) -> bool:
        """Фиксирует отправленную карточку. False, если генерация устарела
        (владелец успел ответить сам, пока LLM думал) — карточку надо закрыть."""
        st = self.current(chat_id)
        expected = "generating" if allow_repeat else "awaiting"
        if st is None or st["state"] != expected or st["gen_id"] != gen_id:
            return False
        db.set_chat_state(
            self.conn, chat_id, state="awaiting",
            card_message_id=card_message_id,
            variants_json=json.dumps(variants, ensure_ascii=False),
            repeat_at_ts=(now + self.cfg.repeat_after_sec) if allow_repeat else None,
        )
        return True

    def note_generation_failed(self, chat_id: int) -> None:
        db.set_chat_state(self.conn, chat_id, state="idle", wave_started_ts=None)

    def note_used(self, chat_id: int, now: int) -> None:
        """✅ вариант или ✍️ свой ответ: час тишины. Вызывается в момент нажатия."""
        db.set_chat_state(self.conn, chat_id, state="silence",
                          silence_until_ts=now + self.cfg.used_silence_sec,
                          repeat_at_ts=None, variants_json=None)

    def note_dismissed(self, chat_id: int) -> None:
        db.set_chat_state(self.conn, chat_id, state="idle", repeat_at_ts=None,
                          card_message_id=None, variants_json=None)

    def note_variants(self, chat_id: int, variants: list[str]) -> int:
        """🔄: новые варианты в существующей карточке, gen_id++ (старые кнопки протухают)."""
        st = self.current(chat_id)
        gen_id = (st["gen_id"] if st else 0) + 1
        db.set_chat_state(self.conn, chat_id, gen_id=gen_id,
                          variants_json=json.dumps(variants, ensure_ascii=False))
        return gen_id

    def variants(self, chat_id: int) -> list[str]:
        st = self.current(chat_id)
        if st is None or not st["variants_json"]:
            return []
        return json.loads(st["variants_json"])
