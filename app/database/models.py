"""Модели данных (п. 18 ТЗ). Хранятся в SQLite через repository.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class UserSettings:
    user_id: int
    selected_model: str
    custom_prompt: str = ""
    personality: str = "realistic"        # ключ пресета характера ("custom" — свой текст)
    custom_personality: str = ""          # свой характер, заданный пользователем
    mood: str = ""                        # текущее настроение персонажа
    proactive_stage: int = 0              # стадия проактивности (0-3)
    last_activity_ts: float = 0.0         # время последней активности (epoch), для восстановления сессий
    last_chat_id: int | None = None       # чат для проактивных сообщений после перезапуска
    last_user_message_ts: float = 0.0
    last_ai_message_ts: float = 0.0
    typing_enabled: bool = True
    debounce_seconds: float = 2.0
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class HistoryMessage:
    user_id: int
    role: str            # "user" | "assistant"
    content: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MemoryFact:
    user_id: int
    fact: str
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
