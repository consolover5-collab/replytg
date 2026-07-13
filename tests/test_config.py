from pathlib import Path

import pytest

from replytg.config import Settings, assert_data_dir_safe


def make_settings(tmp_path: Path, **over) -> Settings:
    base = dict(
        bot_token="t", owner_id=1,
        bridge_db_path=tmp_path / "bridge.db",
        data_dir=tmp_path / "data",
        llm_base_url="http://x", llm_api_key="k",
        _env_file=None,
    )
    base.update(over)
    return Settings(**base)


def test_defaults(tmp_path):
    s = make_settings(tmp_path)
    assert s.llm_model == "mimo-v2.5-pro"
    assert s.wave_window_sec == 600
    assert s.used_silence_sec == 3600
    assert s.repeat_after_sec == 7200
    assert s.chat_blocklist == []
    assert s.db_path == s.data_dir / "replytg.db"
    assert s.style_profile_path == s.data_dir / "style-profile.md"


def test_env_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("REPLYTG_BOT_TOKEN", "tok")
    monkeypatch.setenv("REPLYTG_OWNER_ID", "42")
    monkeypatch.setenv("REPLYTG_BRIDGE_DB_PATH", str(tmp_path / "b.db"))
    monkeypatch.setenv("REPLYTG_LLM_BASE_URL", "http://y")
    monkeypatch.setenv("REPLYTG_LLM_API_KEY", "kk")
    s = Settings(_env_file=None)
    assert s.owner_id == 42 and s.bot_token == "tok"


def test_data_dir_gitignore_guard(tmp_path):
    (tmp_path / ".git").mkdir()  # tmp_path — «репозиторий» без .gitignore
    s = make_settings(tmp_path)
    with pytest.raises(SystemExit):
        assert_data_dir_safe(s)
    (tmp_path / ".gitignore").write_text("data/\n")
    assert_data_dir_safe(s)  # теперь не бросает
    assert (tmp_path / "data").stat().st_mode & 0o777 == 0o700
