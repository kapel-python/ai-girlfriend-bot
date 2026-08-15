"""Модели данных (п. 18 ТЗ). Хранятся в SQLite через repository.py."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class UserSettings:
    user_id: int
    selected_model: str
    custom_prompt: str = ""
    personality: str = "default"          # ключ пресета характера
    mood: str = ""                        # текущее настроение персонажа
    proactive_stage: int = 0              # стадия проактивности (0-3)
    typing_enabled: bool = True
    debounce_seconds: float = 2.0
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class HistoryMessage:
    user_id: int
    role: str            # "user" | "assistant"
    content: str
    created_at: datetime = field(default_factory=datetime.utcnow)


@dataclass
class MemoryFact:
    user_id: int
    fact: str
    created_at: datetime = field(default_factory=datetime.utcnow)
