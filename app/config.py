"""Конфигурация приложения. Все секреты — только из .env (п. 20 ТЗ)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Config:
    bot_token: str
    ai_api_key: str
    ai_base_url: str
    default_model: str
    message_debounce: float
    typing_simulation: bool
    short_memory_limit: int
    database_path: str
    proactive_enabled: bool
    proactive_stage1_min_minutes: float
    proactive_stage1_max_minutes: float
    proactive_stage2_min_minutes: float
    proactive_stage2_max_minutes: float
    proactive_offense_min_minutes: float
    proactive_offense_max_minutes: float
    proactive_check_interval: float

    @property
    def chat_completions_url(self) -> str:
        return f"{self.ai_base_url.rstrip('/')}/chat/completions"

    @property
    def models_url(self) -> str:
        return f"{self.ai_base_url.rstrip('/')}/models"

    @property
    def balance_url(self) -> str:
        return f"{self.ai_base_url.rstrip('/')}/balance"


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    ai_api_key = os.getenv("AI_API_KEY", "").strip()

    if not bot_token:
        raise RuntimeError("BOT_TOKEN не задан. Заполните .env (см. .env.example)")
    if not ai_api_key:
        raise RuntimeError("AI_API_KEY не задан. Заполните .env (см. .env.example)")

    return Config(
        bot_token=bot_token,
        ai_api_key=ai_api_key,
        ai_base_url=os.getenv("AI_BASE_URL", "https://gptunnel.ru/v1").strip(),
        default_model=os.getenv("DEFAULT_MODEL", "deepseek-v4-flash").strip(),
        message_debounce=_get_float("MESSAGE_DEBOUNCE", 2.0),
        typing_simulation=_get_bool("TYPING_SIMULATION", True),
        short_memory_limit=_get_int("SHORT_MEMORY_LIMIT", 100),
        database_path=os.getenv("DATABASE_PATH", "bot.db").strip(),
        proactive_enabled=_get_bool("PROACTIVE_ENABLED", True),
        proactive_stage1_min_minutes=_get_float("PROACTIVE_STAGE1_MIN_MINUTES", 20.0),
        proactive_stage1_max_minutes=_get_float("PROACTIVE_STAGE1_MAX_MINUTES", 45.0),
        proactive_stage2_min_minutes=_get_float("PROACTIVE_STAGE2_MIN_MINUTES", 180.0),
        proactive_stage2_max_minutes=_get_float("PROACTIVE_STAGE2_MAX_MINUTES", 360.0),
        proactive_offense_min_minutes=_get_float("PROACTIVE_OFFENSE_MIN_MINUTES", 60.0),
        proactive_offense_max_minutes=_get_float("PROACTIVE_OFFENSE_MAX_MINUTES", 120.0),
        proactive_check_interval=_get_float("PROACTIVE_CHECK_INTERVAL", 60.0),
    )
