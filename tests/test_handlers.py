from replytg import db
from replytg.handlers import resolve_action
from replytg.waves import WaveConfig, WaveEngine

CFG = WaveConfig(600, 3600, 7200)


def make_engine(tmp_path):
    e = WaveEngine(db.connect(tmp_path / "r.db"), CFG)
    e.note_incoming(1, ts=0, now=0)
    e.tick(now=600)
    e.note_card_sent(1, gen_id=1, card_message_id=5, variants=["а", "б"],
                     allow_repeat=True, now=601)
    return e


def test_resolve_valid_variants(tmp_path):
    e = make_engine(tmp_path)
    assert resolve_action(e, "rt:1:1:v1") == ("v1", 1, 1, "а")
    assert resolve_action(e, "rt:1:1:v2") == ("v2", 1, 1, "б")


def test_resolve_stale_gen_id(tmp_path):
    e = make_engine(tmp_path)
    e.note_variants(1, ["н1", "н2"], expected_gen_id=1)   # gen_id стал 2
    assert resolve_action(e, "rt:1:1:v1") is None
    assert resolve_action(e, "rt:1:2:v1") == ("v1", 1, 2, "н1")


def test_resolve_bound_to_current_card(tmp_path):
    """Клик по чужой/старой карточке (например, оригинал после повтора) отбивается."""
    e = make_engine(tmp_path)
    assert resolve_action(e, "rt:1:1:v1", message_id=5) == ("v1", 1, 1, "а")
    assert resolve_action(e, "rt:1:1:v1", message_id=999) is None
    assert resolve_action(e, "rt:1:1:own", message_id=999) is None


def test_resolve_no_state_or_garbage(tmp_path):
    e = make_engine(tmp_path)
    assert resolve_action(e, "rt:999:1:v1") is None
    assert resolve_action(e, "draft:5:approve") is None
    assert resolve_action(e, "rt:1:1:hack") is None


def test_resolve_variant_after_wave_restart_rejected(tmp_path):
    """После рестарта волны variants очищены — клик по старому варианту не резолвится."""
    e = make_engine(tmp_path)
    e.note_incoming(1, ts=700, now=700)        # новая волна, gen_id ещё 1
    assert resolve_action(e, "rt:1:1:v1") is None


def test_resolve_non_variant_actions(tmp_path):
    e = make_engine(tmp_path)
    assert resolve_action(e, "rt:1:1:more") == ("more", 1, 1, None)
    assert resolve_action(e, "rt:1:1:own") == ("own", 1, 1, None)
    assert resolve_action(e, "rt:1:1:x") == ("x", 1, 1, None)
