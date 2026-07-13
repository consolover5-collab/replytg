import pytest

from replytg.drafts_writer import insert_approved, wait_draft_result


def test_insert_matches_bridge_contract(bridge_db):
    draft_id = insert_approved(bridge_db, chat_id=7, text="привет", now=1000)
    row = bridge_db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    assert row["status"] == "approved"
    assert row["chat_id"] == 7 and row["text"] == "привет"
    assert row["created_ts"] == 1000
    assert row["card_message_id"] is None    # бридж не тронет нашу карточку


def test_insert_rejects_overlong_text(bridge_db):
    with pytest.raises(ValueError, match="4096"):
        insert_approved(bridge_db, chat_id=7, text="х" * 4097, now=0)
    assert bridge_db.execute("SELECT COUNT(*) AS c FROM drafts").fetchone()["c"] == 0


async def test_wait_returns_sent(bridge_db):
    draft_id = insert_approved(bridge_db, 7, "т", now=0)
    bridge_db.execute("UPDATE drafts SET status='sent' WHERE id=?", (draft_id,))
    bridge_db.commit()
    status, error = await wait_draft_result(bridge_db, draft_id,
                                            timeout_sec=1, poll_sec=0.01)
    assert (status, error) == ("sent", None)


async def test_wait_returns_failed_with_error(bridge_db):
    draft_id = insert_approved(bridge_db, 7, "т", now=0)
    bridge_db.execute("UPDATE drafts SET status='failed', error='нет связи' WHERE id=?",
                      (draft_id,))
    bridge_db.commit()
    status, error = await wait_draft_result(bridge_db, draft_id,
                                            timeout_sec=1, poll_sec=0.01)
    assert status == "failed" and error == "нет связи"


async def test_wait_times_out(bridge_db):
    draft_id = insert_approved(bridge_db, 7, "т", now=0)
    status, error = await wait_draft_result(bridge_db, draft_id,
                                            timeout_sec=0.05, poll_sec=0.01)
    assert status == "timeout"
