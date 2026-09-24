"""Управление списком моделей (п. 16 ТЗ).

Список, полученный от API, и список-кандидат на экране намеренно различаются.
Fallback/default никогда не считается проверенным: старые методы
:meth:`ModelRegistry.get_models` и :meth:`ModelRegistry.is_valid` сохранены для
совместимого интерфейса, а прикладной код может запросить безопасный статус.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True)
class ModelStatus:
    """Безопасное описание происхождения модели.

    ``available`` означает, что модель показана как кандидат.  Только
    ``verified=True`` (источник ``api``) означает, что модель подтверждена
    ответом provider API.  Fallback и явно добавленная default-модель имеют
    ``verified=False``.
    """

    model: str
    available: bool
    verified: bool
    source: str
    reason: str = ""
    fetched_at: float | None = None
    stale: bool = False

    @property
    def id(self) -> str:
        return self.model

    @property
    def name(self) -> str:
        return self.model

    @property
    def is_verified(self) -> bool:
        return self.verified

    @property
    def valid(self) -> bool:
        return self.verified

    @property
    def is_fallback(self) -> bool:
        return self.source in {"fallback", "default"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "id": self.model,
            "available": self.available,
            "verified": self.verified,
            "is_verified": self.verified,
            "valid": self.verified,
            "source": self.source,
            "reason": self.reason,
            "fetched_at": self.fetched_at,
            "stale": self.stale,
        }

    as_dict = to_dict

    def __getitem__(self, key: str) -> Any:
        # Небольшое удобство для callers, которые раньше ожидали dict-подобный
        # статус; основной API при этом остаётся типизированным dataclass.
        return self.to_dict()[key]


class ModelRegistry:
    def __init__(self, client: AIClient, default_model: str):
        self._client = client
        self._default = default_model.strip() if isinstance(default_model, str) else ""
        self._models: list[str] = []
        self._verified: set[str] = set()
        self._sources: dict[str, str] = {}
        self._reasons: dict[str, str] = {}
        self._fetched_at: float = 0.0
        self._last_fetch_ok = False
        self._lock = asyncio.Lock()

    async def get_models(self) -> list[str]:
        """Return UI candidates, preserving the historical list return type.

        The returned list can contain an unverified fallback/default.  Use
        :meth:`get_model_status` or :meth:`get_verified_models` before treating
        a candidate as a provider-confirmed model.
        """

        async with self._lock:
            if self._models and time.monotonic() - self._fetched_at < _CACHE_TTL:
                return list(self._models)

            fetched: list[str]
            try:
                raw_models = await self._client.list_models()
                fetched = self._normalize_models(raw_models)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # Model discovery is advisory.  Keep the registry usable and do
                # not log a provider exception message.
                logger.warning("event=model_discovery_failed error_type=%s", type(exc).__name__)
                fetched = []

            now = time.monotonic()
            self._fetched_at = now
            if fetched:
                self._last_fetch_ok = True
                self._verified = set(fetched)
                self._models = list(fetched)
                self._sources = {model: "api" for model in fetched}
                self._reasons = {model: "verified_by_provider" for model in fetched}
            else:
                self._last_fetch_ok = False
                if self._verified:
                    # Keep the last verified list during a temporary outage,
                    # but make its stale status explicit to callers.
                    self._models = list(self._verified)
                    for model in self._verified:
                        self._sources[model] = "api"
                        self._reasons[model] = "provider_unavailable_stale"
                else:
                    self._models = list(dict.fromkeys(FALLBACK_MODELS))
                    self._sources = {model: "fallback" for model in self._models}
                    self._reasons = {
                        model: "fallback_not_verified" for model in self._models
                    }

            if self._default and self._default not in self._models:
                self._models.insert(0, self._default)
                self._sources[self._default] = "default"
                self._reasons[self._default] = "configured_default_not_verified"
            elif self._default in self._sources and self._sources[self._default] == "fallback":
                # A configured default which happens to be in the static list
                # is still only a fallback, never an API confirmation.
                self._sources[self._default] = "default"
                self._reasons[self._default] = "fallback_not_verified"

            return list(self._models)

    async def get_model_status(self, model: str) -> ModelStatus:
        """Return provenance and verification state for *model*."""

        if not isinstance(model, str) or not model:
            return ModelStatus(
                model=model if isinstance(model, str) else "",
                available=False,
                verified=False,
                source="unknown",
                reason="invalid_model",
            )
        await self.get_models()
        present = model in self._models
        if not present:
            return ModelStatus(
                model=model,
                available=False,
                verified=False,
                source="unknown",
                reason="not_listed",
                fetched_at=self._fetched_at or None,
            )
        source = self._sources.get(model, "unknown")
        verified = model in self._verified and source == "api"
        return ModelStatus(
            model=model,
            available=True,
            verified=verified,
            source=source,
            reason=self._reasons.get(model, ""),
            fetched_at=self._fetched_at or None,
            stale=bool(self._verified and not self._last_fetch_ok),
        )

    async def get_status(self, model: str) -> ModelStatus:
        """Alias with a concise name for safe UI/API callers."""

        return await self.get_model_status(model)

    async def status_for(self, model: str) -> ModelStatus:
        return await self.get_model_status(model)

    async def get_models_status(self) -> list[ModelStatus]:
        models = await self.get_models()
        return [await self.get_model_status(model) for model in models]

    async def get_models_with_status(self) -> list[ModelStatus]:
        return await self.get_models_status()

    async def get_verified_models(self) -> list[str]:
        """Return only models confirmed by the provider during a fetch."""

        await self.get_models()
        return [model for model in self._models if model in self._verified]

    async def is_verified(self, model: str) -> bool:
        return (await self.get_model_status(model)).verified

    async def get_verified(self) -> list[str]:
        return await self.get_verified_models()

    async def is_valid(self, model: str) -> bool:
        """Return whether *model* is provider-verified.

        This remains an async bool API for existing handlers, but membership in
        a fallback list alone is deliberately insufficient.
        """

        if not isinstance(model, str) or not model:
            return False
        return await self.is_verified(model)

    async def is_candidate(self, model: str) -> bool:
        """Whether a model is present in the compatibility/UI candidate list."""

        if not isinstance(model, str) or not model:
            return False
        return model in await self.get_models()

    def invalidate(self) -> None:
        """Force the next :meth:`get_models` call to refresh discovery."""

        self._fetched_at = 0.0

    @staticmethod
    def _normalize_models(models: Any) -> list[str]:
        if not isinstance(models, (list, tuple, set, frozenset)):
            return []
        result: list[str] = []
        seen: set[str] = set()
        for model in models:
            if not isinstance(model, str):
                continue
            value = model.strip()
            if not value or value in seen:
                continue
            seen.add(value)
            result.append(value)
        return result
