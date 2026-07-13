from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CARD_LIMIT = 3500  # запас до 4096 Telegram (suffix-статусы, заголовки)
ACTIONS = {"v1", "v2", "more", "own", "x"}
_TRUNCATE_MARKER = "… [начало обрезано]\n"


def _fmt_msg(m) -> str:
    text = m["text"] or f"[{m['media_type'] or 'media'}]"
    return f"💬 {m['sender_name'] or '?'}: {text}"


def build_card_text(wave_msgs: list, variants: list[str]) -> str:
    """Блок входящих + оба варианта. Варианты НИКОГДА не обрезаются (владелец
    подтверждает глазами ровно тот текст, который уйдёт); при нехватке места
    усечётся начало блока входящих с явным маркером."""
    variants_block = f"1️⃣ {variants[0]}\n\n2️⃣ {variants[1]}"
    lines = [_fmt_msg(m) for m in wave_msgs] or ["💬 (новые сообщения)"]
    wave_block = "\n".join(lines)
    budget = CARD_LIMIT - len(variants_block) - 2  # разделитель \n\n
    if len(wave_block) > budget:
        keep = budget - len(_TRUNCATE_MARKER)
        wave_block = _TRUNCATE_MARKER + wave_block[-keep:] if keep > 0 else ""
    if not wave_block:
        return variants_block
    return f"{wave_block}\n\n{variants_block}"


def build_keyboard(chat_id: int, gen_id: int) -> InlineKeyboardMarkup:
    def btn(label: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=label, callback_data=f"rt:{chat_id}:{gen_id}:{action}")

    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("1️⃣", "v1"), btn("2️⃣", "v2")],
        [btn("🔄 Ещё", "more"), btn("✍️ Свой", "own"), btn("❌", "x")],
    ])


def parse_callback(data: str) -> tuple[int, int, str] | None:
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "rt":
        return None
    _, chat_id_s, gen_id_s, action = parts
    if not (chat_id_s.lstrip("-").isdigit() and gen_id_s.isdigit()):
        return None
    if action not in ACTIONS:
        return None
    return int(chat_id_s), int(gen_id_s), action
