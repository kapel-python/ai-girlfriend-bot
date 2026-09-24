"""Конфигурация приложения.

Секреты читаются из окружения (``.env`` загружается только при вызове
:func:`load_config`) и никогда не включаются в ``repr`` объекта конфигурации.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from datetime import timedelta, timezone
from numbers import Integral, Real
from urllib.parse import urlsplit

from dotenv import load_dotenv

# часовой пояс проекта — всегда Москва
try:
    from zoneinfo import ZoneInfo

    MSK = ZoneInfo("Europe/Moscow")
except Exception:  # на случай отсутствия tzdata
    MSK = timezone(timedelta(hours=3), name="MSK")


_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})

# Значения ограничений намеренно хранятся рядом с полями Config.  Они являются
# только безопасными верхними границами: пользователь не может случайно получить
# бесконечный semaphore или бесконечный контекст из-за опечатки в .env.
_MAX_CONCURRENCY = 1024
_MAX_REQUESTS_PER_USER_PER_HOUR = 1_000_000
_MAX_BUFFER_MESSAGES = 100_000
_MAX_BUFFER_CHARS = 100_000_000
_MAX_CONTEXT_CHARS = 100_000_000

_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _env_error(name: str, detail: str) -> ValueError:
    """Ошибка конфигурации без значения переменной (в нём может быть секрет)."""

    return ValueError(f"{name}: {detail}")


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise _env_error(name, "ожидается true/false (или 1/0, yes/no, on/off)")


def _finite_float(name: str, raw: str) -> float:
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        raise _env_error(name, "ожидается число") from None
    if not math.isfinite(value):
        raise _env_error(name, "значение должно быть finite")
    return value


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        value = float(default)
    else:
        value = _finite_float(name, raw)
    if not math.isfinite(value):
        raise _env_error(name, "значение должно быть finite")
    return value


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        value = int(default)
    else:
        text = raw.strip()
        if not text:
            raise _env_error(name, "ожидается целое число")
        try:
            # int() намеренно не принимает «1.0» и другие нецелые записи.
            if not re.fullmatch(r"[+-]?[0-9]+", text):
                raise ValueError
            value = int(text, 10)
        except (TypeError, ValueError):
            raise _env_error(name, "ожидается целое число") from None
    if isinstance(value, bool):
        raise _env_error(name, "ожидается целое число")
    return value


def _get_text(name: str, default: str) -> str:
    raw = os.getenv(name)
    return default if raw is None else raw.strip()


def _parse_admin_ids(raw: str | None) -> frozenset[int]:
    if raw is None or not raw.strip():
        return frozenset()
    result: set[int] = set()
    for token in raw.split(","):
        value = token.strip()
        if not value:
            # Empty list members/trailing commas are harmless and were ignored
            # by the legacy parser.
            continue
        if not value.isdigit():
            raise _env_error("ADMIN_IDS", "ожидаются положительные целые ID через запятую")
        try:
            admin_id = int(value, 10)
        except ValueError:
            raise _env_error("ADMIN_IDS", "ID должен быть целым числом") from None
        if admin_id <= 0:
            raise _env_error("ADMIN_IDS", "ID должен быть положительным")
        result.add(admin_id)
    return frozenset(result)


def _validate_text(name: str, value: str, *, allow_empty: bool = False) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{name}: ожидается строка")
    if not allow_empty and not value.strip():
        raise ValueError(f"{name}: значение не должно быть пустым")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{name}: управляющие символы запрещены")


def _validate_url(name: str, value: str) -> None:
    _validate_text(name, value)
    if value != value.strip() or any(char.isspace() for char in value) or "\\" in value:
        raise ValueError(f"{name}: URL не должен содержать пробелы или обратные слэши")
    try:
        parsed = urlsplit(value)
        # Accessing hostname/port also validates malformed IPv6/port values.
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError(f"{name}: некорректный URL") from None
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise ValueError(f"{name}: URL должен быть абсолютным http(s)-адресом")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name}: credentials в URL запрещены")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{name}: query и fragment в базовом URL запрещены")
    # ``port`` is intentionally read above to force validation of e.g. :99999.
    _ = port


def _validate_path(name: str, value: str) -> None:
    _validate_text(name, value)
    if value != value.strip():
        raise ValueError(f"{name}: путь не должен содержать пробельные края")
    if "\x00" in value:
        raise ValueError(f"{name}: NUL в пути запрещён")
    if len(value) > 4096:
        raise ValueError(f"{name}: путь слишком длинный")


def _validate_model(name: str, value: str) -> None:
    _validate_text(name, value)
    if value != value.strip() or not _MODEL_RE.fullmatch(value):
        raise ValueError(f"{name}: некорректный идентификатор модели")


def _validate_int(name: str, value: int, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise ValueError(f"{name}: ожидается целое число")
    if not minimum <= int(value) <= maximum:
        raise ValueError(f"{name}: значение должно быть в диапазоне [{minimum}, {maximum}]")


def _validate_float(name: str, value: float, minimum: float, maximum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f"{name}: ожидается число")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name}: значение должно быть finite")
    if not minimum <= converted <= maximum:
        raise ValueError(f"{name}: значение должно быть в диапазоне [{minimum}, {maximum}]")


def _validate_config_values(config: "Config") -> None:
    """Проверяет значения как для load_config, так и для прямого Config(...).

    Прямой конструктор исторически используется тестами и небольшими
    интеграционными скриптами, поэтому поля с новыми лимитами имеют defaults и
    не ломают старые вызовы.  Проверка при этом остаётся одинаковой для обоих
    путей создания объекта.
    """

    _validate_text("bot_token", config.bot_token)
    _validate_text("ai_api_key", config.ai_api_key)
    _validate_url("ai_base_url", config.ai_base_url)
    _validate_model("default_model", config.default_model)
    _validate_path("database_path", config.database_path)

    _validate_float("message_debounce", config.message_debounce, 0.0, 86_400.0)
    if not isinstance(config.typing_simulation, bool):
        raise ValueError("typing_simulation: ожидается bool")
    _validate_int("short_memory_limit", config.short_memory_limit, 1, 100_000)
    if not isinstance(config.proactive_enabled, bool):
        raise ValueError("proactive_enabled: ожидается bool")

    _validate_float(
        "proactive_stage1_min_minutes", config.proactive_stage1_min_minutes, 0.0, 1_000_000.0
    )
    _validate_float(
        "proactive_stage1_max_minutes", config.proactive_stage1_max_minutes, 0.0, 1_000_000.0
    )
    _validate_float(
        "proactive_stage2_min_minutes", config.proactive_stage2_min_minutes, 0.0, 1_000_000.0
    )
    _validate_float(
        "proactive_stage2_max_minutes", config.proactive_stage2_max_minutes, 0.0, 1_000_000.0
    )
    _validate_float(
        "proactive_offense_min_minutes", config.proactive_offense_min_minutes, 0.0, 1_000_000.0
    )
    _validate_float(
        "proactive_offense_max_minutes", config.proactive_offense_max_minutes, 0.0, 1_000_000.0
    )
    _validate_float(
        "proactive_check_interval", config.proactive_check_interval, 0.0, 1_000_000.0
    )
    _validate_int("morning_start_hour", config.morning_start_hour, 0, 23)
    _validate_int("morning_end_hour", config.morning_end_hour, 0, 23)
    _validate_float(
        "morning_min_idle_minutes", config.morning_min_idle_minutes, 0.0, 1_000_000.0
    )

    if not isinstance(config.admin_ids, frozenset):
        try:
            normalized = frozenset(config.admin_ids)
            object.__setattr__(config, "admin_ids", normalized)
        except (TypeError, ValueError):
            raise ValueError("admin_ids: ожидается множество ID") from None
    for admin_id in config.admin_ids:
        if isinstance(admin_id, bool) or not isinstance(admin_id, Integral) or int(admin_id) <= 0:
            raise ValueError("admin_ids: ID должен быть положительным целым")

    _validate_float(
        "proactive_min_delay_minutes", config.proactive_min_delay_minutes, 0.0, 1_000_000.0
    )
    _validate_float(
        "proactive_max_delay_minutes", config.proactive_max_delay_minutes, 0.0, 1_000_000.0
    )
    _validate_int("proactive_max_messages", config.proactive_max_messages, 0, 100_000)
    _validate_float(
        "proactive_cooldown_minutes", config.proactive_cooldown_minutes, 0.0, 1_000_000.0
    )

    _validate_int("ai_max_concurrency", config.ai_max_concurrency, 1, _MAX_CONCURRENCY)
    _validate_int(
        "ai_max_requests_per_user_per_hour",
        config.ai_max_requests_per_user_per_hour,
        1,
        _MAX_REQUESTS_PER_USER_PER_HOUR,
    )
    _validate_int("ai_max_buffer_messages", config.ai_max_buffer_messages, 1, _MAX_BUFFER_MESSAGES)
    _validate_int("ai_max_buffer_chars", config.ai_max_buffer_chars, 1, _MAX_BUFFER_CHARS)
    _validate_int("ai_max_context_chars", config.ai_max_context_chars, 1, _MAX_CONTEXT_CHARS)


@dataclass(frozen=True)
class Config:
    bot_token: str = field(repr=False)
    ai_api_key: str = field(repr=False)
    ai_base_url: str
    default_model: str
    message_debounce: float
    typing_simulation: bool
    short_memory_limit: int
    database_path: str
    proactive_enabled: bool
    # Legacy fields are retained for .env/database compatibility but are not
    # used by the DECISION/TIMING scheduler.
    proactive_stage1_min_minutes: float
    proactive_stage1_max_minutes: float
    proactive_stage2_min_minutes: float
    proactive_stage2_max_minutes: float
    proactive_offense_min_minutes: float
    proactive_offense_max_minutes: float
    proactive_check_interval: float
    morning_start_hour: int
    morning_end_hour: int
    morning_min_idle_minutes: float
    admin_ids: frozenset[int]
    # Proactive timing is intentionally broad and sampled anew after every
    # decision; the legacy stage ranges remain accepted for compatibility.
    proactive_min_delay_minutes: float = 3.0
    proactive_max_delay_minutes: float = 720.0
    proactive_max_messages: int = 4
    proactive_cooldown_minutes: float = 20.0

    # Resource limits.  They are intentionally last so existing positional and
    # keyword-based Config(...) callers remain source-compatible.
    ai_max_concurrency: int = 4
    ai_max_requests_per_user_per_hour: int = 60
    ai_max_buffer_messages: int = 20
    ai_max_buffer_chars: int = 12_000
    ai_max_context_chars: int = 24_000

    def __post_init__(self) -> None:
        _validate_config_values(self)

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
    """Load and strictly validate process configuration.

    Importing this module has no filesystem side effect.  This is useful for
    tests and for callers that construct :class:`Config` directly; the dotenv
    file is loaded only at the application configuration boundary.
    """

    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    ai_api_key = os.getenv("AI_API_KEY", "").strip()

    if not bot_token:
        raise RuntimeError("BOT_TOKEN не задан. Заполните .env (см. .env.example)")
    if not ai_api_key:
        raise RuntimeError("AI_API_KEY не задан. Заполните .env (см. .env.example)")

    return Config(
        bot_token=bot_token,
        ai_api_key=ai_api_key,
        ai_base_url=_get_text("AI_BASE_URL", "https://gptunnel.ru/v1"),
        default_model=_get_text("DEFAULT_MODEL", "deepseek-v4-flash"),
        message_debounce=_get_float("MESSAGE_DEBOUNCE", 2.0),
        typing_simulation=_get_bool("TYPING_SIMULATION", True),
        short_memory_limit=_get_int("SHORT_MEMORY_LIMIT", 100),
        database_path=_get_text("DATABASE_PATH", "bot.db"),
        proactive_enabled=_get_bool("PROACTIVE_ENABLED", True),
        proactive_stage1_min_minutes=_get_float("PROACTIVE_STAGE1_MIN_MINUTES", 20.0),
        proactive_stage1_max_minutes=_get_float("PROACTIVE_STAGE1_MAX_MINUTES", 45.0),
        proactive_stage2_min_minutes=_get_float("PROACTIVE_STAGE2_MIN_MINUTES", 180.0),
        proactive_stage2_max_minutes=_get_float("PROACTIVE_STAGE2_MAX_MINUTES", 360.0),
        proactive_offense_min_minutes=_get_float("PROACTIVE_OFFENSE_MIN_MINUTES", 60.0),
        proactive_offense_max_minutes=_get_float("PROACTIVE_OFFENSE_MAX_MINUTES", 120.0),
        proactive_check_interval=_get_float("PROACTIVE_CHECK_INTERVAL", 60.0),
        morning_start_hour=_get_int("MORNING_START_HOUR", 7),
        morning_end_hour=_get_int("MORNING_END_HOUR", 11),
        morning_min_idle_minutes=_get_float("MORNING_MIN_IDLE_MINUTES", 240.0),
        admin_ids=_parse_admin_ids(os.getenv("ADMIN_IDS")),
        proactive_min_delay_minutes=_get_float("PROACTIVE_MIN_DELAY_MINUTES", 3.0),
        proactive_max_delay_minutes=_get_float("PROACTIVE_MAX_DELAY_MINUTES", 720.0),
        proactive_max_messages=_get_int("PROACTIVE_MAX_MESSAGES", 4),
        proactive_cooldown_minutes=_get_float("PROACTIVE_COOLDOWN_MINUTES", 20.0),
        ai_max_concurrency=_get_int("AI_MAX_CONCURRENCY", 4),
        ai_max_requests_per_user_per_hour=_get_int(
            "AI_MAX_REQUESTS_PER_USER_PER_HOUR", 60
        ),
        ai_max_buffer_messages=_get_int("AI_MAX_BUFFER_MESSAGES", 20),
        ai_max_buffer_chars=_get_int("AI_MAX_BUFFER_CHARS", 12_000),
        ai_max_context_chars=_get_int("AI_MAX_CONTEXT_CHARS", 24_000),
    )
