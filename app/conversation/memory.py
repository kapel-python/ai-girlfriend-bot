"""Память диалога (п. 13 ТЗ).

Краткосрочная — последние N сообщений из истории (HistoryRepository).
Долгосрочная — факты, извлекаемые отдельным LLM-вызовом (MemoryRepository).

Фоновые extraction-задачи регистрируются на пользователя. ``invalidate_user``
синхронно поднимает version (до любого await), поэтому даже уже начатый AI-вызов
не сможет записать факты после reset; ``cancel_user`` дополнительно cancel+await
все зарегистрированные задачи.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.ai.client import AIClient, AIClientError
from app.ai.prompts import FACT_EXTRACTION_PROMPT
from app.ai.response_parser import parse_facts
from app.database.repository import HistoryRepository, MemoryRepository
from app.time_context import TIMEZONE_NAME, elapsed, iso, model_message, now

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
        self._versions: dict[int, int] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._tasks: dict[int, set[asyncio.Task]] = {}
        self._request_limiter = None

    def set_request_limiter(self, limiter) -> None:
        """Optional async ``limiter(user_id) -> bool`` from ConversationManager."""
        self._request_limiter = limiter

    def _version(self, user_id: int) -> int:
        return self._versions.get(user_id, 0)

    def _lock(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    async def get_short_memory(self, user_id: int) -> list[dict]:
        recent = await self._history.get_recent(user_id, self._short_limit)
        return [
            {"role": m.role, "content": model_message(m.role, m.content, m.created_at)}
            for m in recent
        ]

    async def get_long_memory(self, user_id: int) -> list[str]:
        return await self._memory.get_facts(user_id)

    def schedule_extraction(
        self, user_id: int, model: str, user_text: str, assistant_text: str,
        user_at: datetime | None = None, assistant_at: datetime | None = None,
    ) -> asyncio.Task:
        """Регистрирует extraction до первого await и возвращает task.

        Регистрация синхронная: clear/shutdown, вызванные сразу после этого
        вызова, уже видят и могут отменить задачу.
        """
        version = self._version(user_id)
        task = asyncio.create_task(
            self._maybe_extract_facts(
                user_id=user_id,
                model=model,
                user_text=user_text,
                assistant_text=assistant_text,
                user_at=user_at,
                assistant_at=assistant_at,
                version=version,
            )
        )
        self._tasks.setdefault(user_id, set()).add(task)

        def _discard(done: asyncio.Task) -> None:
            tasks = self._tasks.get(user_id)
            if tasks is not None:
                tasks.discard(done)
                if not tasks:
                    self._tasks.pop(user_id, None)
            # Done callback обязательно забирает исключение task. Сама
            # extraction логирует свои ошибки, но cancellation тоже читается.
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("user_id=%s event=facts_task_unhandled", user_id)

        task.add_done_callback(_discard)
        return task

    async def maybe_extract_facts(
        self, user_id: int, model: str, user_text: str, assistant_text: str,
        user_at: datetime | None = None, assistant_at: datetime | None = None,
    ) -> None:
        """Совместимый прямой entry point; scheduler должен использовать
        :meth:`schedule_extraction`, чтобы задача была видна lifecycle API."""
        await self._maybe_extract_facts(
            user_id=user_id,
            model=model,
            user_text=user_text,
            assistant_text=assistant_text,
            user_at=user_at,
            assistant_at=assistant_at,
            version=self._version(user_id),
        )

    async def _maybe_extract_facts(
        self, user_id: int, model: str, user_text: str, assistant_text: str,
        user_at: datetime | None = None, assistant_at: datetime | None = None,
        version: int | None = None,
    ) -> None:
        """Раз в N обменов просит модель обновить список фактов.

        Один per-user lock удерживается на всём extraction. Поэтому две
        фоновые задачи не могут читать одну версию facts и записывать ответы
        в обратном порядке; reset по-прежнему может отменить lock holder.
        """
        captured_version = self._version(user_id) if version is None else version
        lock = self._lock(user_id)
        async with lock:
            if captured_version != self._version(user_id):
                return
            counter = self._exchange_counters.get(user_id, 0) + 1
            self._exchange_counters[user_id] = counter
            if counter % EXTRACT_EVERY_N_EXCHANGES != 0:
                return

            if self._request_limiter is not None:
                try:
                    allowed = await self._request_limiter(user_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("user_id=%s event=facts_request_limit_failed", user_id)
                    return
                if not allowed:
                    logger.info(
                        "user_id=%s event=facts_extraction_skipped reason=request_limit",
                        user_id,
                    )
                    return

            try:
                existing = await self._memory.get_facts(user_id)
                prompt = FACT_EXTRACTION_PROMPT.format(
                    existing_facts="\n".join(f"- {f}" for f in existing) or "пока пусто",
                    dialog_fragment=(
                        f"текущее время: {iso(now())}; timezone: {TIMEZONE_NAME}\n"
                        f"пользователь [{iso(user_at)}]: {user_text}\n"
                        f"собеседница [{iso(assistant_at)}]: {assistant_text}\n"
                        f"elapsed_since_user={elapsed(now(), user_at)}; "
                        f"elapsed_since_ai={elapsed(now(), assistant_at)}"
                    ),
                )
                raw = await self._ai.chat(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    max_tokens=1200,
                    temperature=0.3,
                    json_mode=True,
                )
                facts = parse_facts(raw)
                if facts is None:
                    return
                if captured_version != self._version(user_id):
                    logger.info(
                        "user_id=%s event=facts_update_skipped reason=stale_version",
                        user_id,
                    )
                    return
                await self._memory.replace_facts(user_id, facts)
                logger.info("user_id=%s event=facts_updated count=%d", user_id, len(facts))

            except asyncio.CancelledError:
                raise
            except AIClientError as e:
                logger.warning("user_id=%s event=facts_extraction_failed error=%s", user_id, e)
            except Exception:
                logger.exception("user_id=%s event=facts_extraction_failed", user_id)

    def invalidate_user(self, user_id: int) -> None:
        """Синхронно инвалидирует extraction и отменяет зарегистрированные task."""
        self._versions[user_id] = self._version(user_id) + 1
        for task in list(self._tasks.get(user_id, ())):
            if not task.done():
                task.cancel()

    async def cancel_user(self, user_id: int, *, reset_counter: bool = True) -> None:
        """Invalidate + cancel/await всех extraction task пользователя."""
        self.invalidate_user(user_id)
        # Снимок сделан после invalidate; новые корректные задачи могут быть
        # запущены позже и уже принадлежат новой версии.
        tasks = list(self._tasks.get(user_id, ()))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if reset_counter:
            async with self._lock(user_id):
                self._exchange_counters.pop(user_id, None)

    async def cancel_all(self) -> None:
        users = set(self._versions) | set(self._tasks) | set(self._exchange_counters)
        await asyncio.gather(
            *(self.cancel_user(user_id) for user_id in users),
            return_exceptions=True,
        )

    async def shutdown(self) -> None:
        await self.cancel_all()
