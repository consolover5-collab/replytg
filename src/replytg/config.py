import logging
import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REPLYTG_", env_file=".env", extra="ignore")

    bot_token: str
    owner_id: int
    bridge_db_path: Path
    data_dir: Path = Path("./data")

    llm_base_url: str
    llm_api_key: str
    llm_model: str = "mimo-v2.5-pro"
    llm_timeout_sec: float = 60.0

    wave_window_sec: int = 600        # окно накопления волны
    used_silence_sec: int = 3600      # тишина после использования подсказки
    repeat_after_sec: int = 7200      # повтор неиспользованной карточки (один раз)
    poll_interval_sec: float = 5.0    # период сканирования bridge.db
    draft_wait_timeout_sec: int = 30  # ожидание, пока бридж отправит драфт
    history_limit: int = 30           # сообщений контекста для LLM
    chat_blocklist: list[int] = []

    @property
    def db_path(self) -> Path:
        return self.data_dir / "replytg.db"

    @property
    def style_profile_path(self) -> Path:
        return self.data_dir / "style-profile.md"


def _secure_data_dir(data_dir: Path) -> None:
    """Каталог с личными данными доступен только владельцу (0700)."""
    if data_dir.exists():
        try:
            os.chmod(data_dir, 0o700)
        except OSError as e:
            log.warning("failed to chmod data dir %s: %s", data_dir, e)
    else:
        data_dir.mkdir(parents=True, mode=0o700, exist_ok=True)


def assert_data_dir_safe(settings: Settings) -> None:
    """БД состояния и стиль-профиль не должны попасть в git (паттерн бриджа)."""
    _secure_data_dir(settings.data_dir)
    git_dir = settings.data_dir.resolve()
    for parent in [git_dir, *git_dir.parents]:
        if (parent / ".git").exists():
            gitignore = parent / ".gitignore"
            rel = str(git_dir.relative_to(parent))
            if not gitignore.exists() or rel.split("/")[0] not in gitignore.read_text():
                raise SystemExit(
                    f"ОТКАЗ ЗАПУСКА: {git_dir} лежит в git-репозитории {parent}, "
                    f"но не покрыт .gitignore. Добавь '{rel}/' в {gitignore}."
                )
            break
