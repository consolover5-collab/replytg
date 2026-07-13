"""Единственная запись в bridge.db: драфт со статусом approved.
Отправку контакту делает вотчер бриджа (поллинг 3 с): approved → sending → sent|failed."""
import asyncio
import sqlite3
import time

DRAFT_TEXT_LIMIT = 4096  # лимит Telegram; бридж такой текст отвергнет — режем на входе


def insert_approved(conn: sqlite3.Connection, chat_id: int, text: str,
                    now: int | None = None) -> int:
    if len(text) > DRAFT_TEXT_LIMIT:
        raise ValueError(f"текст длиннее {DRAFT_TEXT_LIMIT} символов — Telegram не примет")
    cur = conn.execute(
        "INSERT INTO drafts (chat_id, text, status, created_ts) VALUES (?, ?, 'approved', ?)",
        (chat_id, text, int(now if now is not None else time.time())),
    )
    conn.commit()
    return cur.lastrowid


async def wait_draft_result(
    conn: sqlite3.Connection, draft_id: int,
    timeout_sec: float = 30, poll_sec: float = 1.0,
) -> tuple[str, str | None]:
    """('sent'|'failed'|'timeout', error). Терминальные статусы бриджа: sent, failed."""
    deadline = asyncio.get_event_loop().time() + timeout_sec
    while True:
        row = conn.execute("SELECT status, error FROM drafts WHERE id=?", (draft_id,)).fetchone()
        if row and row["status"] in ("sent", "failed"):
            return row["status"], row["error"]
        if asyncio.get_event_loop().time() >= deadline:
            return "timeout", "бридж не отправил за отведённое время (демон бриджа жив?)"
        await asyncio.sleep(poll_sec)
