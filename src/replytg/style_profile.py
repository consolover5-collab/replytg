"""Пересборка стиль-профиля из bridge.db: пары «входящее → мой ответ» → LLM → markdown.
Запуск руками: uv run replytg-style. Первичный профиль (ретро-экспорт из Telegram)
собирается отдельно — см. README."""
import asyncio
import sqlite3

from replytg import bridge_reader
from replytg.config import Settings, assert_data_dir_safe
from replytg import suggest

PROFILE_PROMPT = """Ниже пары «сообщение собеседника → мой реальный ответ» из моей личной переписки.
Составь по ним профиль моего стиля письма в markdown:

1. Раздел «Манера»: 5-10 наблюдений (длина фраз, регистр, пунктуация, эмодзи, типичные слова,
   тон с разными людьми). Только то, что реально видно в примерах.
2. Раздел «Примеры»: выбери 20-30 самых характерных пар как есть, формат:
   - Собеседник: …
     Я: …

Пиши только markdown профиля, без предисловий.

=== ПАРЫ ===
{pairs}
"""


def extract_pairs(conn: sqlite3.Connection, min_len: int = 3,
                  limit: int = 500, per_chat: int = 40) -> list[tuple[str, str]]:
    """Пары «входящее → мой ручной ответ»: свежие в приоритете, баланс по чатам,
    ответы из sent-драфтов исключены (иначе модель учится на самой себе)."""
    draft_texts = {(r["chat_id"], r["text"]) for r in conn.execute(
        "SELECT chat_id, text FROM drafts WHERE status='sent'")}
    rows = conn.execute(
        "SELECT chat_id, direction, is_auto, text, ts FROM messages "
        "WHERE text IS NOT NULL ORDER BY chat_id, ts, id",
    ).fetchall()
    by_chat: dict[int, list[tuple[int, str, str]]] = {}
    prev: str | None = None
    prev_chat: int | None = None
    for r in rows:
        if r["chat_id"] != prev_chat:
            prev, prev_chat = None, r["chat_id"]
        if r["direction"] == "in":
            prev = r["text"]
        elif (prev is not None and not r["is_auto"] and len(r["text"]) >= min_len
              and (r["chat_id"], r["text"]) not in draft_texts):
            by_chat.setdefault(r["chat_id"], []).append((r["ts"], prev, r["text"]))
            prev = None
        else:
            prev = None
    picked: list[tuple[int, str, str]] = []
    for chat_pairs in by_chat.values():
        picked += sorted(chat_pairs, key=lambda p: -p[0])[:per_chat]
    picked = sorted(picked, key=lambda p: -p[0])[:limit]
    picked.sort(key=lambda p: p[0])  # хронология — так промпту естественнее
    return [(inc, out) for _, inc, out in picked]


async def rebuild(settings: Settings) -> str:
    conn = bridge_reader.connect_ro(settings.bridge_db_path)
    pairs = extract_pairs(conn)
    if len(pairs) < 10:
        raise SystemExit(f"мало данных: {len(pairs)} пар (< 10). Профиль пока не пересобрать.")
    pairs_text = "\n".join(f"Собеседник: {a}\nЯ: {b}\n" for a, b in pairs)
    prompt = PROFILE_PROMPT.replace("{pairs}", pairs_text)  # не .format: в парах бывают {}
    async with suggest.make_client(settings.llm_base_url, settings.llm_api_key,
                                   timeout=120.0) as client:
        resp = await client.post("/chat/completions", json={
            "model": settings.llm_model, "temperature": 0.3,
            "messages": [{"role": "user", "content": prompt}],
        })
        resp.raise_for_status()
        profile = resp.json()["choices"][0]["message"]["content"]
    settings.style_profile_path.write_text(profile)
    return f"профиль пересобран из {len(pairs)} пар → {settings.style_profile_path}"


def main() -> None:
    settings = Settings()
    assert_data_dir_safe(settings)
    print(asyncio.run(rebuild(settings)))
