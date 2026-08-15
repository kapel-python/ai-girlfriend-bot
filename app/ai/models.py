"""Управление списком моделей (п. 16 ТЗ).

Список подтягивается из API и кэшируется; при недоступности API
используется небольшой fallback-список.
"""

from __future__ import annotations

import asyncio
import logging
import time

from app.ai.client import AIClient

logger = logging.getLogger(__name__)

FALLBACK_MODELS = [
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "gpt-5-mini",
    "claude-4.5-haiku",
    "gemini-3-flash",
    "kimi-k2.6",
    "grok-4.3",
    "qwen3.7-plus",
]

_CACHE_TTL = 3600  # секунд


class ModelRegistry:
    def __init__(self, client: AIClient, default_model: str):
        self._client = client
        self._default = default_model
        self._models: list[str] = []
        self._fetched_at: float = 0.0
        self._lock = asyncio.Lock()

    async def get_models(self) -> list[str]:
        async with self._lock:
            if self._models and time.monotonic() - self._fetched_at < _CACHE_TTL:
                return self._models
            models = await self._client.list_models()
            if models:
                self._models = models
                self._fetched_at = time.monotonic()
            elif not self._models:
                self._models = list(FALLBACK_MODELS)
            if self._default not in self._models:
                self._models.insert(0, self._default)
            return self._models

    async def is_valid(self, model: str) -> bool:
        return model in await self.get_models()
