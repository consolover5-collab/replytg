"""Сборка: aiogram-поллинг бота-пульта + фоновый цикл (скан bridge.db → engine → действия).

Решения по надёжности (см. Аддендум 1 плана):
- первый запуск слушает только будущее (курсор = хвост messages);
- рестарт из generating восстанавливается в collecting (перегенерация тиком);
- ровно одно enabled business-подключение и его owner == REPLYTG_OWNER_ID, иначе отказ;
- карточка строится из того же снапшота волны, который видел LLM;
- LLM-вызовы не блокируют скан (create_task + in-flight по чату).
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from aiogram import Bot, Dispatcher

from replytg import bridge_reader, cards, db, drafts_writer, suggest
from replytg.config import Settings, assert_data_dir_safe
from replytg.waves import CloseCard, Generate, RepeatCard, WaveConfig, WaveEngine

log = logging.getLogger(__name__)

CLOSE_NOTES = {"answered": "✔️ Ты уже ответил сам", "new_wave": "⏭ Пришли новые сообщения"}


def ensure_cursor(state_conn, bridge_ro) -> None:
    """Первый запуск: курсор на хвост messages — историю не читаем (иначе демон
    прогнал бы всю накопленную переписку как «новые» события)."""
    row = state_conn.execute("SELECT 1 FROM kv WHERE key='last_seen_rowid'").fetchone()
    if row is None:
        db.set_cursor(state_conn, bridge_reader.max_message_id(bridge_ro))


def recover_generating(state_conn) -> None:
    """Рестарт демона посреди LLM-вызова: generating → collecting, wave_started_ts
    сохраняется — ближайший tick честно перегенерирует."""
    state_conn.execute(
        "UPDATE chat_state SET state='collecting' WHERE state='generating'")
    state_conn.commit()


def check_owner_connection(bridge_ro, owner_id: int) -> None:
    """Ровно одно enabled business-подключение и оно принадлежит владельцу —
    иначе карточки и драфты могут разъехаться по разным людям. Отказ запуска."""
    rows = bridge_ro.execute(
        "SELECT owner_id FROM connections WHERE is_enabled=1").fetchall()
    if len(rows) == 0:
        raise SystemExit("в bridge.db нет активного business-подключения — включи бота "
                         "бриджа в настройках Telegram Business и дождись первого события")
    if len(rows) > 1:
        raise SystemExit("в bridge.db несколько enabled-подключений; поддерживается "
                         "ровно одно — отключи лишние")
    if rows[0]["owner_id"] != owner_id:
        raise SystemExit(f"owner business-подключения ({rows[0]['owner_id']}) не совпадает "
                         f"с REPLYTG_OWNER_ID ({owner_id}) — проверь конфиг/базу")


@dataclass
class Deps:
    settings: Any
    engine: WaveEngine
    bot: Any
    bridge_ro: Any
    bridge_rw: Any
    style_profile: str
    now: Callable[[], int] = field(default=lambda: int(time.time()))
    generate_fn: Callable | None = None  # тестовый шов; по умолчанию _generate
    _tasks: set = field(default_factory=set)
    _inflight_regen: set = field(default_factory=set)

    # --- скан bridge.db ---

    def scan_bridge(self, now: int) -> None:
        cursor = db.get_cursor(self.engine.conn)
        rows = bridge_reader.fetch_new(self.bridge_ro, cursor)
        actions: list = []
        for r in rows:
            if r["chat_id"] in self.settings.chat_blocklist or r["chat_id"] == self.settings.owner_id:
                continue
            if r["direction"] == "in":
                actions += self.engine.note_incoming(r["chat_id"], ts=r["ts"], now=now)
            elif not r["is_auto"]:
                # авто-ответы бриджа (away/offline) — не ручной ответ владельца,
                # волну не отменяют
                actions += self.engine.note_outgoing(r["chat_id"], now=now)
        if rows:
            db.set_cursor(self.engine.conn, rows[-1]["id"])
        if actions:  # CloseCard-действия от сканирования
            self._spawn(self.process_actions(actions))

    # --- действия engine ---

    async def process_actions(self, actions: list) -> None:
        for a in actions:
            try:
                if isinstance(a, Generate):
                    # параллельная старая генерация того же чата безвредна: её
                    # note_card_sent/note_generation_failed отобьются guard'ом по gen_id
                    self._spawn(self._do_generate(a))
                elif isinstance(a, RepeatCard):
                    await self._do_repeat(a)
                elif isinstance(a, CloseCard):
                    await self._edit_card(a.card_message_id,
                                          CLOSE_NOTES.get(a.reason, a.reason))
            except Exception:  # noqa: BLE001 — одно действие не роняет остальные
                log.exception("action failed: %r", a)

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self) -> None:
        """Дождаться всех фоновых задач (используется тестами и shutdown'ом)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _do_generate(self, a: Generate) -> None:
        # любой сбой (LLM, Telegram) не должен оставить чат в generating навечно:
        # guarded note_generation_failed вернёт в idle только ЭТУ генерацию
        try:
            # снапшот волны читается ОДИН раз: LLM и карточка видят одно и то же
            wave = bridge_reader.wave_incoming(self.bridge_ro, a.chat_id, a.wave_started_ts)
            gen = self.generate_fn or self._generate
            try:
                variants = await gen(a.chat_id, a.wave_started_ts, wave)
            except suggest.SuggestError as e:
                log.warning("генерация для чата %s не удалась: %s", a.chat_id, e)
                self.engine.note_generation_failed(a.chat_id, a.gen_id)
                return
            text = cards.build_card_text(wave, variants)
            sent = await self.bot.send_message(
                chat_id=self.settings.owner_id, text=text,
                reply_markup=cards.build_keyboard(a.chat_id, a.gen_id, self.settings.variant_count))
            ok = self.engine.note_card_sent(
                a.chat_id, a.gen_id, sent.message_id, variants, now=self.now(),
            )
            if not ok:  # волна перезапущена/закрыта, пока LLM думал
                await self._edit_card(sent.message_id, CLOSE_NOTES["answered"])
        except Exception:  # noqa: BLE001
            log.exception("генерация для чата %s упала", a.chat_id)
            self.engine.note_generation_failed(a.chat_id, a.gen_id)

    async def _do_repeat(self, a: RepeatCard) -> None:
        st = self.engine.current(a.chat_id)
        variants = self.engine.variants(a.chat_id)
        # ранний guard по gen_id: тик мог родить RepeatCard для генерации, которую
        # уже сменил 🔄/новая волна — фантомный повтор устаревшей пары не шлём
        if (
            st is None
            or st["state"] != "awaiting"
            or st["gen_id"] != a.gen_id
            or len(variants) != self.settings.variant_count
        ):
            return

        old_card = st["card_message_id"]
        # regen уступает дорогу: если крутится 🔄, повтор не мешает — таймер восстановит
        # сама регенерация (успех → новый цикл, провал → resume_repeat)
        if old_card is None or a.chat_id in self._inflight_regen:
            return

        wave = bridge_reader.wave_incoming(
            self.bridge_ro, a.chat_id, st["wave_started_ts"],
        )
        try:
            sent = await self.bot.send_message(
                chat_id=self.settings.owner_id,
                text="🔁 Напоминаю:\n\n" + cards.build_card_text(wave, variants),
                reply_markup=cards.build_keyboard(
                    a.chat_id, a.gen_id, self.settings.variant_count,
                ),
            )
        except Exception:
            # отправка не удалась — перевооружаем таймер, чтобы карточка не осталась
            # без повтора навсегда
            self.engine.resume_repeat(a.chat_id, a.gen_id, old_card, self.now())
            raise

        # повторная проверка после await: 🔄 мог стартовать, пока летел send_message —
        # тогда новая карточка лишняя, гасим именно ЕЁ клавиатуру
        if a.chat_id in self._inflight_regen:
            await self._remove_keyboard(sent.message_id)
            return

        accepted = self.engine.note_repeat_sent(
            a.chat_id, a.gen_id,
            expected_card_message_id=old_card,
            new_card_message_id=sent.message_id,
            now=self.now(),
        )
        # CAS-отказ: текущая карточка уже сменилась — гасим НОВУЮ (только что отправленную),
        # а текущую/старую не трогаем
        if not accepted:
            await self._remove_keyboard(sent.message_id)
            return
        await self._remove_keyboard(old_card)  # старая клавиатура больше не нужна

    async def _remove_keyboard(self, message_id: int) -> None:
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=self.settings.owner_id,
                message_id=message_id,
                reply_markup=None,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось снять клавиатуру карточки %s: %s", message_id, exc)

    async def _generate(self, chat_id: int, wave_started_ts: int, wave_rows: list) -> list[str]:
        hist = bridge_reader.history(self.bridge_ro, chat_id, self.settings.history_limit,
                                     before_ts=wave_started_ts)

        def fmt(m) -> str:
            who = "Я: " if m["direction"] == "out" else f"{m['sender_name'] or 'Контакт'}: "
            return who + (m["text"] or f"[{m['media_type'] or 'media'}]")

        def fmt_in(m) -> str:
            return f"{m['sender_name'] or 'Контакт'}: " + (m["text"] or f"[{m['media_type'] or 'media'}]")

        async with suggest.make_client(self.settings.llm_base_url,
                                       self.settings.llm_api_key,
                                       self.settings.llm_timeout_sec) as client:
            return await suggest.generate_variants(
                client, self.settings.llm_model, self.style_profile,
                history_text="\n".join(fmt(m) for m in hist),
                wave_text="\n".join(fmt_in(m) for m in wave_rows),
                max_len=self.settings.max_variant_len,
                count=self.settings.variant_count)

    # --- используется handlers.py ---

    async def regenerate(self, chat_id: int, card) -> None:
        if chat_id in self._inflight_regen:
            return  # уже генерирую — второй 🔄 молча игнорируется
        st = self.engine.current(chat_id)
        if st is None or st["state"] != "awaiting":
            return

        expected_gen = st["gen_id"]
        expected_card = card.message_id
        self._inflight_regen.add(chat_id)
        try:
            # пауза таймера повтора: пока крутится LLM, тик не должен родить повтор
            # старых вариантов. Любой выход ниже обязан вернуть таймер (resume/note_variants)
            if not self.engine.pause_repeat(chat_id, expected_gen, expected_card):
                return
            try:
                wave = bridge_reader.wave_incoming(
                    self.bridge_ro, chat_id, st["wave_started_ts"],
                )
                gen = self.generate_fn or self._generate
                try:
                    variants = await gen(chat_id, st["wave_started_ts"], wave)
                except suggest.SuggestError as exc:
                    self.engine.resume_repeat(chat_id, expected_gen, expected_card, self.now())
                    await self.bot.send_message(
                        self.settings.owner_id, f"⚠️ LLM не ответил: {exc}",
                    )
                    return

                current = self.engine.current(chat_id)
                if (
                    current is None
                    or current["state"] != "awaiting"
                    or current["gen_id"] != expected_gen
                    or current["card_message_id"] != expected_card
                ):
                    # resume не нужен: карточка уже не наша, repeat-таймером владеет новое состояние
                    return  # карточка сменилась/закрылась, пока LLM думал — выбрасываем

                new_gen = expected_gen + 1
                # edit ДО записи: если Telegram откажет, сохранённое состояние остаётся
                # согласованным с видимой карточкой (старые варианты/gen_id/таймер)
                try:
                    await card.edit_text(
                        text=cards.build_card_text(wave, variants),
                        reply_markup=cards.build_keyboard(
                            chat_id, new_gen, self.settings.variant_count,
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("не удалось обновить карточку: %s", exc)
                    self.engine.resume_repeat(chat_id, expected_gen, expected_card, self.now())
                    return

                # edit прошёл — запись синхронна, без await между ней и edit_text,
                # так что другая корутина не влезет в это окно
                stored_gen = self.engine.note_variants(
                    chat_id, variants,
                    expected_gen_id=expected_gen,
                    expected_card_message_id=expected_card,
                    now=self.now(),
                )
                if stored_gen is None:
                    # resume не нужен: карточка сменилась под edit'ом, repeat-таймером владеет новое состояние
                    await self._edit_card(expected_card, "Карточка устарела")
            except Exception:
                # непредвиденный сбой (SuggestError/edit обработаны выше) не должен
                # оставить карточку без таймера повтора: пауза требует парного resume
                self.engine.resume_repeat(chat_id, expected_gen, expected_card, self.now())
                raise
        finally:
            self._inflight_regen.discard(chat_id)

    async def send_and_report(self, chat_id: int, text: str, card_message_id: int) -> None:
        """Вставка approved-драфта + статус reply-сообщением на карточку.
        Guard'ы (note_used) уже сделаны вызывающим."""
        try:
            draft_id = drafts_writer.insert_approved(self.bridge_rw, chat_id, text)
        except ValueError as e:
            await self._reply_status(card_message_id, f"⚠️ Не отправлено: {e}")
            return
        status, error = await drafts_writer.wait_draft_result(
            self.bridge_rw, draft_id, timeout_sec=self.settings.draft_wait_timeout_sec)
        if status == "sent":
            note = "✅ Отправлено"
        elif status == "failed":
            note = f"⚠️ Не отправлено: {error}"
        else:  # timeout: драфт остался approved и может уйти позже — это НЕ отказ
            note = ("⏳ Бридж не подтвердил отправку — статус неизвестен. "
                    "Не отправляй повторно вслепую, сначала проверь чат.")
        await self._reply_status(card_message_id, note)

    async def _reply_status(self, card_message_id: int, note: str) -> None:
        try:
            await self.bot.send_message(self.settings.owner_id, note,
                                        reply_to_message_id=card_message_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось отправить статус к карточке %s: %s", card_message_id, exc)

    async def _edit_card(self, message_id: int, note: str) -> None:
        try:
            await self.bot.edit_message_text(
                chat_id=self.settings.owner_id, message_id=message_id, text=note)
        except Exception as exc:  # noqa: BLE001
            log.warning("не удалось отредактировать карточку %s: %s", message_id, exc)


async def poll_loop(deps: Deps, interval: float) -> None:
    while True:
        try:
            now = deps.now()
            deps.scan_bridge(now)
            await deps.process_actions(deps.engine.tick(now))
        except Exception:  # noqa: BLE001 — цикл не должен умирать (паттерн бриджа)
            log.exception("poll iteration failed")
        await asyncio.sleep(interval)


async def amain() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings()
    assert_data_dir_safe(settings)
    if not settings.bridge_db_path.exists():
        raise SystemExit(f"bridge.db не найдена: {settings.bridge_db_path}")

    bridge_ro = bridge_reader.connect_ro(settings.bridge_db_path)
    bridge_reader.check_schema(bridge_ro)
    check_owner_connection(bridge_ro, settings.owner_id)

    import sqlite3
    bridge_rw = sqlite3.connect(settings.bridge_db_path, timeout=30)
    bridge_rw.row_factory = sqlite3.Row
    bridge_rw.execute("PRAGMA busy_timeout=30000")

    style = ""
    if settings.style_profile_path.exists():
        style = settings.style_profile_path.read_text()
    else:
        log.warning("стиль-профиль %s не найден — подсказки без стилизации",
                    settings.style_profile_path)

    engine = WaveEngine(db.connect(settings.db_path),
                        WaveConfig(settings.wave_window_sec, settings.used_silence_sec,
                                   settings.repeat_after_sec, settings.repeat_max_count))
    ensure_cursor(engine.conn, bridge_ro)
    recover_generating(engine.conn)

    bot = Bot(token=settings.bot_token)
    deps = Deps(settings=settings, engine=engine, bot=bot,
                bridge_ro=bridge_ro, bridge_rw=bridge_rw, style_profile=style)

    from replytg.handlers import make_router
    dp = Dispatcher()
    dp.include_router(make_router(deps))

    loop_task = asyncio.create_task(poll_loop(deps, settings.poll_interval_sec))
    log.info("replytg запущен: owner=%s, bridge=%s", settings.owner_id, settings.bridge_db_path)
    try:
        await dp.start_polling(bot)
    finally:
        loop_task.cancel()
        await deps.drain()


def main() -> None:
    asyncio.run(amain())
