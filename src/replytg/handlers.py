"""Кнопки карточек и «свой ответ». Всё — только от владельца (REPLYTG_OWNER_ID).
Каждое действие проходит guard машины состояний: False/None = «карточка устарела»,
никакой отправки по протухшим кнопкам."""
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from replytg.cards import parse_callback
from replytg.waves import WaveEngine

log = logging.getLogger(__name__)


class OwnReply(StatesGroup):
    waiting_text = State()


def resolve_action(engine: WaveEngine, data: str) -> tuple[str, int, int, str | None] | None:
    """callback data → (action, chat_id, gen_id, текст варианта|None).
    None = мусор/протухшая генерация/вариантов больше нет."""
    parsed = parse_callback(data)
    if parsed is None:
        return None
    chat_id, gen_id, action = parsed
    st = engine.current(chat_id)
    if st is None or st["gen_id"] != gen_id:
        return None
    if action in ("v1", "v2"):
        variants = engine.variants(chat_id)
        if len(variants) != 2:
            return None
        return action, chat_id, gen_id, variants[0 if action == "v1" else 1]
    return action, chat_id, gen_id, None


def make_router(deps) -> Router:
    """deps — объект демона (Deps из daemon.py): engine, settings, now(),
    send_and_report(), regenerate(). Router создаётся после deps."""
    router = Router()
    owner = deps.settings.owner_id
    router.message.filter(F.from_user.id == owner)
    router.callback_query.filter(F.from_user.id == owner)

    @router.message(CommandStart())
    async def on_start(msg: Message) -> None:
        await msg.answer("replytg на связи: карточки с вариантами ответов будут приходить сюда.")

    @router.message(Command("cancel"), OwnReply.waiting_text)
    async def on_cancel(msg: Message, state: FSMContext) -> None:
        await state.clear()
        await msg.answer("Ок, отменил.")

    @router.message(OwnReply.waiting_text, F.text)
    async def on_own_text(msg: Message, state: FSMContext) -> None:
        data = await state.get_data()
        await state.clear()
        # тишина применяется, только если волна не перезапущена; текст, набранный
        # владельцем руками, отправляется в любом случае — это явное намерение
        deps.engine.note_used(data["target_chat_id"], data["gen_id"], now=deps.now())
        await deps.send_and_report(chat_id=data["target_chat_id"], text=msg.text,
                                   card_message_id=data["card_message_id"])

    @router.callback_query(F.data.startswith("rt:"))
    async def on_card_button(cb: CallbackQuery, state: FSMContext, bot: Bot) -> None:
        res = resolve_action(deps.engine, cb.data or "")
        if res is None:
            await cb.answer("Карточка устарела")
            return
        action, chat_id, gen_id, variant_text = res
        if action in ("v1", "v2"):
            # guard: тишина включается атомарно ДО отправки; False = устарело
            if not deps.engine.note_used(chat_id, gen_id, now=deps.now()):
                await cb.answer("Карточка устарела")
                return
            await cb.answer("Отправляю")
            await _strip_kb(cb)
            await deps.send_and_report(chat_id=chat_id, text=variant_text,
                                       card_message_id=cb.message.message_id)
        elif action == "more":
            await cb.answer("Генерирую ещё…")
            await deps.regenerate(chat_id, card=cb.message)
        elif action == "own":
            await state.set_state(OwnReply.waiting_text)
            await state.update_data(target_chat_id=chat_id, gen_id=gen_id,
                                    card_message_id=cb.message.message_id)
            await cb.answer()
            await cb.message.answer("Пиши текст ответа (или /cancel).")
        else:  # x
            if not deps.engine.note_dismissed(chat_id, gen_id):
                await cb.answer("Карточка устарела")
                return
            await cb.answer("Закрыто")
            await _edit_note(cb.message, "❌ Закрыто")

    return router


async def _strip_kb(cb: CallbackQuery) -> None:
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception as exc:  # noqa: BLE001 — правка карточки не критична (паттерн бриджа)
        log.warning("не удалось убрать клавиатуру: %s", exc)


async def _edit_note(message, suffix: str) -> None:
    try:
        await message.edit_text(text=(message.text or "") + f"\n\n{suffix}", reply_markup=None)
    except Exception as exc:  # noqa: BLE001
        log.warning("не удалось отредактировать карточку: %s", exc)
