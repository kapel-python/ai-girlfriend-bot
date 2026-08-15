"""Память диалога (п. 13 ТЗ).

Краткосрочная — последние N сообщений из истории (HistoryRepository).
Долгосрочная — факты, извлекаемые отдельным LLM-вызовом (MemoryRepository).
"""

from __future__ import annotations

import logging

from app.ai.client import AIClient, AIClientError
from app.ai.prompts import FACT_EXTRACTION_PROMPT
from app.ai.response_parser import parse_facts
from app.database.repository import HistoryRepository, MemoryRepository

logger = logging.getLogger(__name__)

# как часто запускать извлечение фактов (каждый N-й завершённый обмен)
EXTRACT_EVERY_N_EXCHANGES = 3


class MemoryService:
    def __init__(
        self,
        ai_client: AIClient,
        history_repo: HistoryRepository,
        memory_repo: MemoryRepository,
        short_limit: int,
    ):
        self._ai = ai_client
        self._history = history_repo
        self._memory = memory_repo
        self._short_limit = short_limit
        self._exchange_counters: dict[int, int] = {}

    async def get_short_memory(self, user_id: int) -> list[dict]:
        recent = await self._history.get_recent(user_id, self._short_limit)
        return [{"role": m.role, "content": m.content} for m in recent]

    async def get_long_memory(self, user_id: int) -> list[str]:
        return await self._memory.get_facts(user_id)

    async def maybe_extract_facts(
        self, user_id: int, model: str, user_text: str, assistant_text: str
    ) -> None:
        """Раз в N обменов просит модель обновить список фактов."""
        counter = self._exchange_counters.get(user_id, 0) + 1
        self._exchange_counters[user_id] = counter
        if counter % EXTRACT_EVERY_N_EXCHANGES != 0:
            return

        try:
            existing = await self._memory.get_facts(user_id)
            prompt = FACT_EXTRACTION_PROMPT.format(
                existing_facts="\n".join(f"- {f}" for f in existing) or "пока пусто",
                dialog_fragment=f"пользователь: {user_text}\nсобеседница: {assistant_text}",
            )
            raw = await self._ai.chat(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=1200,
                temperature=0.3,
                json_mode=True,
            )
            facts = parse_facts(raw)
            if facts is not None:
                await self._memory.replace_facts(user_id, facts)
                logger.info("user_id=%s event=facts_updated count=%d", user_id, len(facts))
        except AIClientError as e:
            logger.warning("user_id=%s event=facts_extraction_failed error=%s", user_id, e)
        except Exception:
            logger.exception("user_id=%s event=facts_extraction_failed", user_id)
