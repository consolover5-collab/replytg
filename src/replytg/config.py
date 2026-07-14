import logging
import os
from pathlib import Path

from pydantic import Field, model_validator
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
    llm_timeout_sec: float = Field(default=60.0, gt=0)

    wave_window_sec: int = Field(default=600, gt=0)              # окно накопления волны
    used_silence_sec: int = Field(default=3600, gt=0)            # тишина после использования подсказки
    repeat_after_sec: int = Field(default=7200, gt=0)            # задержка перед повтором неиспользованной карточки
    repeat_max_count: int = Field(default=1, ge=0)               # сколько раз повторять карточку (0 — выключить)
    poll_interval_sec: float = Field(default=5.0, gt=0)          # период сканирования bridge.db
    draft_wait_timeout_sec: int = Field(default=30, gt=0)        # ожидание, пока бридж отправит драфт
    history_limit: int = Field(default=30, gt=0)                 # сообщений контекста для LLM
    variant_count: int = Field(default=2, ge=1, le=5)            # число вариантов ответа в карточке
    max_variant_len: int = Field(default=1000, ge=100, le=1500)  # лимит длины одного варианта
    chat_blocklist: list[int] = []

    @model_validator(mode="after")
    def variants_fit_card(self) -> "Settings":
        # CARD_LIMIT карточки — 3500; 500 символов оставляем под заголовки и входящие
        if self.variant_count * self.max_variant_len > 3000:
            raise ValueError(
                "варианты не помещаются в карточку: уменьши "
                "REPLYTG_VARIANT_COUNT или REPLYTG_MAX_VARIANT_LEN"
            )
        return self

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
            lines = {
                line.strip().rstrip("/")
                for line in gitignore.read_text().splitlines()
                if line.strip() and not line.lstrip().startswith("#")
            } if gitignore.exists() else set()
            rel_path = str(git_dir.relative_to(parent))
            covered = rel_path in lines or rel_path.split("/")[0] in lines
            if not covered:
                raise SystemExit(
                    f"ОТКАЗ ЗАПУСКА: {git_dir} лежит в git-репозитории {parent}, "
                    f"но не покрыт .gitignore. Добавь '{rel_path}/' в {gitignore}."
                )
            break
