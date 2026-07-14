from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

CARD_LIMIT = 3500  # запас до 4096 Telegram (suffix-статусы, заголовки)
NUMBER_LABELS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")  # variant_count ограничен ≤5 в Settings
NON_VARIANT_ACTIONS = {"more", "own", "x"}
_TRUNCATE_MARKER = "… [начало обрезано]\n"


def _fmt_msg(m) -> str:
    text = m["text"] or f"[{m['media_type'] or 'media'}]"
    return f"💬 {m['sender_name'] or '?'}: {text}"


def build_card_text(wave_msgs: list, variants: list[str]) -> str:
    """Блок входящих + все варианты. Варианты НИКОГДА не обрезаются (владелец
    подтверждает глазами ровно тот текст, который уйдёт); при нехватке места
    усечётся начало блока входящих с явным маркером."""
    variants_block = "\n\n".join(
        f"{NUMBER_LABELS[index]} {variant}"
        for index, variant in enumerate(variants)
    )
    lines = [_fmt_msg(m) for m in wave_msgs] or ["💬 (новые сообщения)"]
    wave_block = "\n".join(lines)
    budget = CARD_LIMIT - len(variants_block) - 2  # разделитель \n\n
    if len(wave_block) > budget:
        keep = budget - len(_TRUNCATE_MARKER)
        wave_block = _TRUNCATE_MARKER + wave_block[-keep:] if keep > 0 else ""
    if not wave_block:
        return variants_block
    return f"{wave_block}\n\n{variants_block}"


def build_keyboard(chat_id: int, gen_id: int, variant_count: int) -> InlineKeyboardMarkup:
    def btn(label: str, action: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=label, callback_data=f"rt:{chat_id}:{gen_id}:{action}")

    variant_buttons = [
        btn(NUMBER_LABELS[index], f"v{index + 1}")
        for index in range(variant_count)
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        variant_buttons,
        [btn("🔄 Ещё", "more"), btn("✍️ Свой", "own"), btn("❌", "x")],
    ])


def variant_index(action: str) -> int | None:
    """'v3' → 2 (0-based индекс в списке variants). Не число/не v-действие → None."""
    if action.startswith("v") and action[1:].isdigit():
        index = int(action[1:]) - 1
        return index if index >= 0 else None
    return None


def parse_callback(data: str) -> tuple[int, int, str] | None:
    parts = data.split(":")
    if len(parts) != 4 or parts[0] != "rt":
        return None
    _, chat_id_s, gen_id_s, action = parts
    if not (chat_id_s.lstrip("-").isdigit() and gen_id_s.isdigit()):
        return None
    if not (action in NON_VARIANT_ACTIONS or variant_index(action) is not None):
        return None
    return int(chat_id_s), int(gen_id_s), action
