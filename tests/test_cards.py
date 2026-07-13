from replytg.cards import CARD_LIMIT, build_card_text, build_keyboard, parse_callback


def msg(text, media_type=None, sender_name="Маша"):
    return {"text": text, "media_type": media_type, "sender_name": sender_name}


def test_card_text_contains_messages_and_variants():
    text = build_card_text([msg("Ты завтра сможешь?"), msg(None, media_type="voice")],
                           ["Да, после обеда", "Не обещаю"])
    assert "Маша" in text
    assert "Ты завтра сможешь?" in text
    assert "[voice]" in text                 # медиа без текста — плейсхолдер
    assert "1️⃣ Да, после обеда" in text
    assert "2️⃣ Не обещаю" in text


def test_variants_never_truncated():
    """Подтверждение глазами — защита: отправляемый текст виден всегда целиком."""
    v1, v2 = "а" * 1000, "б" * 1000
    text = build_card_text([msg("х" * 5000)], [v1, v2])
    assert v1 in text and v2 in text          # оба варианта целиком
    assert len(text) <= CARD_LIMIT
    assert "обрезано" in text                 # маркер усечения истории


def test_empty_wave_still_renders():
    text = build_card_text([], ["в1", "в2"])
    assert "1️⃣ в1" in text and "2️⃣ в2" in text


def test_keyboard_callback_data():
    kb = build_keyboard(chat_id=123, gen_id=7)
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert data == ["rt:123:7:v1", "rt:123:7:v2", "rt:123:7:more",
                    "rt:123:7:own", "rt:123:7:x"]


def test_parse_callback():
    assert parse_callback("rt:123:7:v1") == (123, 7, "v1")
    assert parse_callback("rt:-42:7:x") == (-42, 7, "x")
    assert parse_callback("rt:abc:7:v1") is None
    assert parse_callback("draft:5:approve") is None
    assert parse_callback("rt:1:2:hack") is None
