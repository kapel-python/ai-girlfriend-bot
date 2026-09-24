"""Conversation Manager — центральный сервис состояния диалога (п. 22 ТЗ).

Модель конкурентности:

- generation захватывается синхронно, до первого ``await``;
- handle/cancel/clear сериализованы per-user ``asyncio.Lock``;
- новая реплика cancel+await предыдущую pipeline task до запуска новой;
- ``NEW_MESSAGE`` возвращает незавершённый turn в следующую обработку, а
  ``CLEAR``/``SHUTDOWN`` отбрасывают его;
- каждый chunk фиксируется только после подтверждённого Telegram-ответа;
- mood/history/proactive side effects защищены generation guard;
- входящие turn хранятся в durable FIFO до завершения обработки.

Typing запускается после debounce ещё до генерации и живёт до конца send.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

from aiogram.exceptions import TelegramAPIError

from app.ai.client import AIClient, AIClientError
from app.ai.prompts import (
    PROACTIVE_DECISION_PROMPT,
    PROACTIVE_MESSAGE_PROMPT,
    build_system_prompt,
)
from app.ai.response_parser import parse_initiative, parse_response
from app.config import MSK, Config
from app.conversation.memory import MemoryService
from app.conversation.sender import SendProgress, SendResult, TelegramSender
from app.database.models import PendingMessageRecord
from app.database.repository import (
    AIRequestRepository,
    HistoryRepository,
    PendingMessageRepository,
    PersonalityRepository,
    UserSettingsRepository,
)
from app.time_context import TIMEZONE_NAME, elapsed, iso, now

logger = logging.getLogger(__name__)


class CancelReason(str, Enum):
    """Явная причина invalidation pipeline для корректного cancel cleanup."""

    NEW_MESSAGE = "new_message"
    MOOD_ONLY = "mood_only"
    CLEAR = "clear"
    SHUTDOWN = "shutdown"

    @property
    def returns_taken(self) -> bool:
        return self in (CancelReason.NEW_MESSAGE, CancelReason.MOOD_ONLY)


_REASON_PRIORITY = {
    # A new message must be able to supersede a mood-only invalidation so the
    # cancelled turn is restored into the new generation.  Explicit clear and
    # shutdown remain stronger than a new message.
    CancelReason.MOOD_ONLY: 20,
    CancelReason.NEW_MESSAGE: 30,
    CancelReason.CLEAR: 40,
    CancelReason.SHUTDOWN: 50,
}


@dataclass
class PendingMessage:
    text: str
    created_at: datetime
    chat_id: int | None = None
    durable_id: int | None = None


@dataclass
class UserSession:
    buffer: list[PendingMessage] = field(default_factory=list)
    generation_id: int = 0
    task: asyncio.Task | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    last_activity: float = field(default_factory=time.monotonic)
    last_chat_id: int | None = None
    proactive_stage: int | None = None   # legacy DB field; scheduler bookkeeping only
    proactive_due_at: float = 0.0
    proactive_count_since_user: int = 0
    last_proactive_at: float = 0.0
    proactive_waiting_to_send: bool = False
    proactive_task: asyncio.Task | None = None
    last_morning_date: object = None     # дата последнего «доброго утра» (МСК)
    cancel_reason: CancelReason = CancelReason.NEW_MESSAGE
    rate_limit_retry_task: asyncio.Task | None = None
    rate_limit_notified_until: float = 0.0
    durable_warning_sent: bool = False
    request_times: deque[float] = field(default_factory=deque, repr=False)


class ConversationManager:
    def __init__(
        self,
        config: Config,
        ai_client: AIClient,
        sender: TelegramSender,
        memory: MemoryService,
        settings_repo: UserSettingsRepository,
        history_repo: HistoryRepository,
        global_repo=None,
        personality_repo: PersonalityRepository | None = None,
        pending_repo: PendingMessageRepository | None = None,
        request_repo: AIRequestRepository | None = None,
    ):
        self._config = config
        self._ai = ai_client
        self._sender = sender
        self._memory = memory
        self._settings_repo = settings_repo
        self._history = history_repo
        self._global = global_repo
        self._personalities = personality_repo
        self._sessions: dict[int, UserSession] = {}
        self._proactive_task: asyncio.Task | None = None
        self._task_reasons: dict[asyncio.Task, CancelReason] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._request_locks: dict[int, asyncio.Lock] = {}
        self._shutdown_lock = asyncio.Lock()
        self._closing = False
        self._closed = False

        # main.py не обязан знать о новых репозиториях: они используют то же
        # SQLite connection. Явные аргументы оставлены для DI и regression tests.
        db = getattr(history_repo, "_db", None)
        self._pending = pending_repo or (PendingMessageRepository(db) if db is not None else None)
        self._requests = request_repo or (AIRequestRepository(db) if db is not None else None)
        set_memory_limiter = getattr(self._memory, "set_request_limiter", None)
        if set_memory_limiter is not None:
            set_memory_limiter(self._allow_memory_request)

    # ------------------------------------------------------------------ #
    # sessions, cancellation and durable queue                          #
    # ------------------------------------------------------------------ #

    def _session(self, user_id: int) -> UserSession:
        if user_id not in self._sessions:
            self._sessions[user_id] = UserSession(
                proactive_due_at=time.monotonic() + self._proactive_delay_seconds()
            )
        return self._sessions[user_id]

    def _request_cancel(self, task: asyncio.Task | None, reason: CancelReason) -> None:
        if task is None or task.done():
            return
        old = self._task_reasons.get(task)
        if old is None or _REASON_PRIORITY[reason] >= _REASON_PRIORITY[old]:
            self._task_reasons[task] = reason
            task.cancel(reason.value)

    def _reason_for_task(self, task: asyncio.Task | None) -> CancelReason:
        # Unknown external cancellation should preserve an unfinished turn;
        # explicit CLEAR/SHUTDOWN paths always install their reason first.
        return self._task_reasons.get(task, CancelReason.NEW_MESSAGE)

    async def _wait_task(self, task: asyncio.Task | None) -> None:
        """Забирает результат task; не даёт повторный cancel оставить cleanup."""
        if task is None or task is asyncio.current_task():
            return
        caller_cancelled: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                if not task.done():
                    caller_cancelled = exc
                    continue
            except Exception:
                # Точная ошибка будет прочитана через task.result() ниже.
                pass
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception(
                "event=background_task_failed task=%s", getattr(task, "__qualname__", type(task).__name__)
            )
        finally:
            self._task_reasons.pop(task, None)
        if caller_cancelled is not None:
            raise caller_cancelled

    async def _finish_cleanup(self, awaitable) -> tuple[Exception | None, asyncio.CancelledError | None]:
        """Завершает cleanup даже при повторной отмене parent task."""
        task = asyncio.create_task(awaitable)
        repeated_cancel: asyncio.CancelledError | None = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                repeated_cancel = exc
            except Exception:
                break
        error: Exception | None = None
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            error = exc
        return error, repeated_cancel

    def _is_current(self, user_id: int, generation: int) -> bool:
        return (
            not self._closing
            and self._sessions.get(user_id) is not None
            and self._sessions[user_id].generation_id == generation
        )

    @staticmethod
    def _record_to_pending(record: PendingMessageRecord) -> PendingMessage:
        return PendingMessage(
            text=record.content,
            created_at=record.created_at,
            chat_id=record.chat_id,
            durable_id=record.id,
        )

    def _dedupe_and_sort_buffer(self, session: UserSession) -> None:
        unique: list[PendingMessage] = []
        ids: set[int] = set()
        for item in session.buffer:
            if item.durable_id is not None:
                if item.durable_id in ids:
                    continue
                ids.add(item.durable_id)
            unique.append(item)
        unique.sort(key=lambda item: (item.created_at, item.durable_id or 0))
        session.buffer = unique

    def _restore_taken(self, session: UserSession, taken: list[PendingMessage]) -> None:
        if not taken:
            return
        session.buffer = taken + session.buffer
        self._dedupe_and_sort_buffer(session)

    def _remove_pending(self, session: UserSession, item: PendingMessage) -> None:
        session.buffer = [
            existing for existing in session.buffer
            if existing is not item
            and not (
                existing.durable_id is not None
                and existing.durable_id == item.durable_id
            )
        ]

    async def _load_pending_locked(self, user_id: int, session: UserSession) -> None:
        if self._pending is None:
            self._dedupe_and_sort_buffer(session)
            return
        records = await self._pending.list_pending(user_id)
        known_ids = {
            item.durable_id for item in session.buffer if item.durable_id is not None
        }
        for record in records:
            if record.id not in known_ids:
                session.buffer.append(self._record_to_pending(record))
                known_ids.add(record.id)
        self._dedupe_and_sort_buffer(session)

    async def _persist_incoming(
        self, user_id: int, session: UserSession, item: PendingMessage,
    ) -> bool:
        if self._pending is None:
            return False
        try:
            record = await self._pending.enqueue(
                user_id, int(item.chat_id or session.last_chat_id or user_id),
                item.text, item.created_at,
            )
            item.durable_id = record.id
            item.chat_id = record.chat_id
            return True
        except Exception:
            logger.exception("user_id=%s event=pending_persist_failed", user_id)
            if not session.durable_warning_sent:
                session.durable_warning_sent = True
                try:
                    await self._sender.send_messages(
                        chat_id=int(item.chat_id or session.last_chat_id or user_id),
                        user_id=user_id,
                        messages=[
                            "Не смогла надёжно сохранить это сообщение в очереди. "
                            "Я оставлю его в текущем диалоге и попробую обработать сейчас."
                        ],
                        typing_enabled=False,
                    )
                except Exception:
                    logger.exception("user_id=%s event=pending_failure_notice_failed", user_id)
            return False

    async def _clear_pending(self, user_id: int) -> None:
        if self._pending is None:
            return
        try:
            await self._pending.clear(user_id)
        except Exception:
            logger.exception("user_id=%s event=pending_clear_failed", user_id)

    async def _invalidate_active(
        self, user_id: int, reason: CancelReason, *, clear_buffer: bool,
        clear_pending: bool,
    ) -> None:
        session = self._session(user_id)
        # Generation и intent меняются до первого await — pipeline/proactive,
        # уже находящиеся в другом await, сразу становятся stale.
        session.generation_id += 1
        session.cancel_reason = reason
        if clear_buffer:
            session.buffer.clear()
        if reason is CancelReason.CLEAR:
            session.proactive_stage = 0
        self._cancel_proactive_plan(session)
        old_pipeline = session.task
        old_proactive = session.proactive_task
        old_retry = session.rate_limit_retry_task
        self._request_cancel(old_pipeline, reason)
        self._request_cancel(old_proactive, reason)
        self._request_cancel(old_retry, reason)

        async with session.lock:
            await self._wait_task(old_pipeline)
            await self._wait_task(old_proactive)
            await self._wait_task(old_retry)
            if session.task is old_pipeline:
                session.task = None
            if session.proactive_task is old_proactive:
                session.proactive_task = None
            if session.rate_limit_retry_task is old_retry:
                session.rate_limit_retry_task = None
            if reason in (CancelReason.CLEAR, CancelReason.SHUTDOWN):
                await self._memory.cancel_user(
                    user_id, reset_counter=reason is CancelReason.CLEAR
                )
            if clear_pending:
                await self._clear_pending(user_id)

    async def handle_message(self, user_id: int, chat_id: int, text: str) -> None:
        """Точка входа из Telegram handler.

        Generation, FIFO item и intent фиксируются до первого await. Поэтому
        даже back-to-back вызовы не могут запустить pipeline без предыдущей
        реплики или без cancel+await старого task.
        """
        session = self._session(user_id)
        received_at = now()
        incoming = PendingMessage(text=text, created_at=received_at, chat_id=chat_id)
        session.buffer.append(incoming)
        session.generation_id += 1
        generation = session.generation_id
        session.cancel_reason = CancelReason.NEW_MESSAGE
        session.last_activity = time.monotonic()
        session.last_chat_id = chat_id
        session.proactive_stage = 0
        session.proactive_count_since_user = 0
        session.last_proactive_at = 0.0
        self._cancel_proactive_plan(session)
        self._schedule_proactive(session)

        old_pipeline = session.task
        old_proactive = session.proactive_task
        old_retry = session.rate_limit_retry_task
        self._request_cancel(old_pipeline, CancelReason.NEW_MESSAGE)
        self._request_cancel(old_proactive, CancelReason.NEW_MESSAGE)
        self._request_cancel(old_retry, CancelReason.NEW_MESSAGE)

        async with session.lock:
            # Сначала фиксируем новую реплику durable: даже если старый AI
            # не сотрудничает с cancellation, процесс уже не потеряет вход.
            if session.cancel_reason in (CancelReason.CLEAR, CancelReason.SHUTDOWN):
                self._remove_pending(session, incoming)
                return
            await self._persist_incoming(user_id, session, incoming)
            if session.cancel_reason in (CancelReason.CLEAR, CancelReason.SHUTDOWN):
                self._remove_pending(session, incoming)
                if incoming.durable_id is not None and self._pending is not None:
                    try:
                        await self._pending.delete_pending((incoming.durable_id,))
                    except Exception:
                        logger.exception("user_id=%s event=pending_delete_failed", user_id)
                return
            await self._load_pending_locked(user_id, session)

            # Только после durable enqueue ждём старый pipeline. Это сохраняет
            # вход даже для AI, который на время игнорирует отмену.
            await self._wait_task(old_pipeline)
            await self._wait_task(old_proactive)
            await self._wait_task(old_retry)
            if session.task is old_pipeline:
                session.task = None
            if session.proactive_task is old_proactive:
                session.proactive_task = None
            if session.rate_limit_retry_task is old_retry:
                session.rate_limit_retry_task = None

            if generation != session.generation_id:
                return
            try:
                await self._settings_repo.update(
                    user_id,
                    proactive_stage=0,
                    last_activity_ts=received_at.timestamp(),
                    last_chat_id=chat_id,
                    last_user_message_ts=received_at.timestamp(),
                )
            except Exception:
                # Реплика уже durable; временная ошибка настроек не должна её
                # удалить или отменять обработку.
                logger.exception("user_id=%s event=activity_persist_failed", user_id)
            if not self._is_current(user_id, generation):
                return
            if session.task is None or session.task.done():
                session.task = asyncio.create_task(
                    self._pipeline(user_id, chat_id, generation)
                )

    async def cancel_active(
        self, user_id: int, reason: CancelReason = CancelReason.CLEAR,
    ) -> None:
        """Отменяет turn с явной причиной.

        ``CLEAR`` дополнительно очищает in-memory/durable FIFO. ``SHUTDOWN``
        сбрасывает только runtime state и сохраняет pending для restart.
        Handler может вызвать метод без аргумента перед ``history_repo.clear``.
        """
        if not isinstance(reason, CancelReason):
            reason = CancelReason(str(reason))
        if reason is CancelReason.MOOD_ONLY:
            raise ValueError("используйте invalidate_mood_only для mood-only сброса")
        await self._invalidate_active(
            user_id, reason,
            clear_buffer=reason in (CancelReason.CLEAR, CancelReason.SHUTDOWN),
            clear_pending=reason is CancelReason.CLEAR,
        )

    async def invalidate_mood_only(self, user_id: int) -> None:
        """Останавливает генерации, которые могут позже перезаписать mood.

        Публичный метод для mood-only handler. Входящие реплики не удаляются;
        ``taken`` возвращается в FIFO, а proactive plan инвалидируется.
        """
        await self._invalidate_active(
            user_id, CancelReason.MOOD_ONLY,
            clear_buffer=False, clear_pending=False,
        )

    # Public alias with an intentionally shorter name for handler authors.
    cancel_for_mood_reset = invalidate_mood_only

    async def shutdown(self) -> None:
        """Idempotent lifecycle stop. Durable pending остаётся для restart."""
        async with self._shutdown_lock:
            if self._closed:
                return
            self._closing = True
            loop_task = self._proactive_task
            self._request_cancel(loop_task, CancelReason.SHUTDOWN)
            await self._wait_task(loop_task)
            if self._proactive_task is loop_task:
                self._proactive_task = None

            for user_id in list(self._sessions):
                session = self._sessions[user_id]
                session.generation_id += 1
                session.cancel_reason = CancelReason.SHUTDOWN
                session.buffer.clear()
                self._cancel_proactive_plan(session)
                old_pipeline = session.task
                old_proactive = session.proactive_task
                old_retry = session.rate_limit_retry_task
                self._request_cancel(old_pipeline, CancelReason.SHUTDOWN)
                self._request_cancel(old_proactive, CancelReason.SHUTDOWN)
                self._request_cancel(old_retry, CancelReason.SHUTDOWN)
                async with session.lock:
                    await self._wait_task(old_pipeline)
                    await self._wait_task(old_proactive)
                    await self._wait_task(old_retry)
                    session.task = None
                    session.proactive_task = None
                    session.rate_limit_retry_task = None
                    await self._memory.cancel_user(user_id, reset_counter=False)
            fallback_tasks = list(self._background_tasks)
            for task in fallback_tasks:
                self._request_cancel(task, CancelReason.SHUTDOWN)
            for task in fallback_tasks:
                await self._wait_task(task)
            await self._memory.shutdown()
            self._closed = True

    # ------------------------------------------------------------------ #
    # limits, context and request throttling                             #
    # ------------------------------------------------------------------ #

    def _int_limit(self, name: str, default: int, minimum: int = 1) -> int:
        try:
            return max(minimum, int(getattr(self._config, name, default)))
        except (TypeError, ValueError):
            return default

    def _max_buffer_messages(self) -> int:
        return self._int_limit("ai_max_buffer_messages", 20)

    def _max_buffer_chars(self) -> int:
        return self._int_limit("ai_max_buffer_chars", 12_000)

    def _max_context_chars(self) -> int:
        return self._int_limit("ai_max_context_chars", 24_000)

    def _max_requests_per_hour(self) -> int:
        try:
            return max(0, int(getattr(self._config, "ai_max_requests_per_user_per_hour", 60)))
        except (TypeError, ValueError):
            return 60

    @staticmethod
    def _message_cost(message: dict) -> int:
        return len(str(message.get("role", ""))) + len(str(message.get("content", "")))

    @staticmethod
    def _truncate_for_context(text: str, budget: int) -> str:
        if budget <= 0:
            return ""
        if len(text) <= budget:
            return text
        marker = "\n[…сообщение обрезано лимитом ai_max_context_chars…]"
        if budget <= len(marker):
            return text[:budget]
        available = budget - len(marker)
        head = available * 2 // 3
        tail = available - head
        return text[:head].rstrip() + marker + (text[-tail:] if tail else "")

    def _fit_context(self, context: list[dict], max_chars: int) -> list[dict]:
        if max_chars <= 0:
            return []
        if sum(self._message_cost(message) for message in context) <= max_chars:
            return context
        if not context:
            return []

        marker = {
            "role": "system",
            "content": "Часть старого контекста скрыта лимитом ai_max_context_chars.",
        }
        first = dict(context[0])
        first_budget = max(1, min(len(str(first.get("content", ""))), max_chars // 3))
        first["content"] = self._truncate_for_context(str(first.get("content", "")), first_budget)
        result = [first]
        used = self._message_cost(first)
        marker_cost = self._message_cost(marker)
        if used + marker_cost <= max_chars:
            result.append(marker)
            used += marker_cost
            remaining = max_chars - used
        else:
            remaining = max(0, max_chars - used)

        selected: list[dict] = []
        for original in reversed(context[1:]):
            message = dict(original)
            cost = self._message_cost(message)
            if cost <= remaining:
                selected.append(message)
                remaining -= cost
                continue
            if not selected and remaining > 0:
                content = str(message.get("content", ""))
                message["content"] = self._truncate_for_context(
                    content, max(0, remaining - len(str(message.get("role", ""))))
                )
                selected.append(message)
                remaining = 0
            if remaining <= 0:
                break
        result.extend(reversed(selected))
        return result

    def _request_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._request_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._request_locks[user_id] = lock
        return lock

    async def _allow_memory_request(self, user_id: int) -> bool:
        session = self._session(user_id)
        allowed, _retry_at = await self._reserve_request(
            user_id, session, session.generation_id
        )
        return allowed

    async def _reserve_request(
        self, user_id: int, session: UserSession, generation: int,
    ) -> tuple[bool, float | None]:
        if not self._is_current(user_id, generation):
            return False, None
        limit = self._max_requests_per_hour()
        now_ts = time.time()
        async with self._request_lock(user_id):
            if not self._is_current(user_id, generation):
                return False, None
            if self._requests is not None:
                try:
                    allowed, retry_at = await self._requests.reserve(user_id, limit, now_ts)
                except Exception:
                    logger.exception("user_id=%s event=request_limit_store_failed", user_id)
                    allowed, retry_at = self._reserve_request_in_memory(
                        user_id, session, limit, now_ts
                    )
            else:
                allowed, retry_at = self._reserve_request_in_memory(
                    user_id, session, limit, now_ts
                )
            if allowed and self._is_current(user_id, generation):
                session.rate_limit_notified_until = 0.0
            return allowed, retry_at

    def _reserve_request_in_memory(
        self, user_id: int, session: UserSession, limit: int, now_ts: float,
    ) -> tuple[bool, float | None]:
        del user_id
        cutoff = now_ts - 3600.0
        while session.request_times and session.request_times[0] <= cutoff:
            session.request_times.popleft()
        if len(session.request_times) >= limit:
            retry_at = session.request_times[0] + 3600.0 if session.request_times else now_ts + 3600.0
            return False, max(now_ts + 0.05, retry_at)
        session.request_times.append(now_ts)
        return True, None

    def _schedule_rate_retry_locked(
        self, user_id: int, session: UserSession, retry_at: float,
    ) -> None:
        current = session.rate_limit_retry_task
        if current is not None and not current.done():
            return
        delay = max(0.05, retry_at - time.time())
        generation = session.generation_id

        async def retry() -> None:
            try:
                await asyncio.sleep(delay)
                async with session.lock:
                    if self._closing or not self._is_current(user_id, generation):
                        return
                    if not session.buffer or session.last_chat_id is None:
                        return
                    if session.task is not None and not session.task.done():
                        return
                    session.cancel_reason = CancelReason.NEW_MESSAGE
                    session.task = asyncio.create_task(
                        self._pipeline(user_id, session.last_chat_id, generation)
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("user_id=%s event=rate_limit_retry_failed", user_id)
            finally:
                if session.rate_limit_retry_task is asyncio.current_task():
                    session.rate_limit_retry_task = None

        session.rate_limit_retry_task = asyncio.create_task(retry())

    async def _notify_rate_limit(
        self, user_id: int, chat_id: int, generation: int, retry_at: float,
    ) -> None:
        session = self._sessions.get(user_id)
        if session is None or not self._is_current(user_id, generation):
            return
        if session.rate_limit_notified_until >= retry_at:
            return
        session.rate_limit_notified_until = retry_at
        await self._sender.send_messages(
            chat_id=chat_id,
            user_id=user_id,
            messages=[
                "Я сохранила сообщение, но сейчас достигнут лимит запросов. "
                "Попробую ответить, когда окно сбросится; можешь написать ещё."
            ],
            typing_enabled=False,
        )
        logger.warning(
            "user_id=%s event=ai_request_limit retry_at=%.0f", user_id, retry_at
        )

    # ------------------------------------------------------------------ #
    # runtime/context and idempotent history persistence                  #
    # ------------------------------------------------------------------ #

    async def _runtime(self, settings) -> tuple[str, str, bool, float]:
        """Глобальные параметры (модель, промт, typing, debounce) — одни на всех.

        Характер, настроение, история и факты остаются per-user.
        Fallback на per-user значения, если глобальный репозиторий не подключён.
        """
        if self._global is None:
            return (
                settings.selected_model,
                settings.custom_prompt,
                settings.typing_enabled,
                settings.debounce_seconds,
            )
        return (
            await self._global.get_str("selected_model"),
            await self._global.get_str("custom_prompt"),
            await self._global.get_bool("typing_enabled"),
            await self._global.get_float("debounce_seconds"),
        )

    async def _build_context(
        self, user_id: int, settings, custom_prompt: str = "",
        last_user_at: datetime | None = None,
        last_ai_at: datetime | None = None,
    ) -> list[dict]:
        personality_prompt = ""
        if self._personalities is not None and settings.personality != "custom":
            try:
                preset = await self._personalities.get(settings.personality)
                if preset is None:
                    settings.personality = "realistic"
                else:
                    personality_prompt = preset.prompt
            except Exception:
                logger.exception(
                    "user_id=%s event=personality_load_failed fallback=realistic", user_id
                )
                settings.personality = "realistic"
        system_prompt = build_system_prompt(
            settings.personality,
            custom_prompt or settings.custom_prompt,
            settings.mood,
            getattr(settings, "custom_personality", ""),
            personality_prompt=personality_prompt,
        )
        context: list[dict] = [{"role": "system", "content": system_prompt}]

        now_msk = now()
        latest = await self._history.get_last_timestamps(user_id)
        last_user_at = last_user_at or latest["user"]
        last_ai_at = last_ai_at or latest["assistant"]
        if last_user_at is None and getattr(settings, "last_user_message_ts", 0):
            last_user_at = datetime.fromtimestamp(settings.last_user_message_ts, MSK)
        if last_ai_at is None and getattr(settings, "last_ai_message_ts", 0):
            last_ai_at = datetime.fromtimestamp(settings.last_ai_message_ts, MSK)
        elapsed_user = elapsed(now_msk, last_user_at)
        elapsed_ai = elapsed(now_msk, last_ai_at)
        context.append({
            "role": "system",
            "content": (
                "АКТУАЛЬНЫЙ КОНТЕКСТ ВРЕМЕНИ (только для обработки, не показывай его пользователю):\n"
                + json.dumps({
                    "current_datetime": iso(now_msk),
                    "timezone": TIMEZONE_NAME,
                    "last_user_message_timestamp": iso(last_user_at),
                    "last_ai_message_timestamp": iso(last_ai_at),
                    "elapsed_time_since_last_user_message": elapsed_user,
                    "elapsed_time_since_last_ai_message": elapsed_ai,
                }, ensure_ascii=False, separators=(",", ":")) + "\n"
                f"Сейчас {now_msk.strftime('%A, %H:%M:%S')} по Москве. "
                "Учитывай время суток и день недели в тоне и темах: ночью ты сонная, "
                "утром бодрее, будни и выходные ощущаются по-разному."
            ),
        })

        facts = await self._memory.get_long_memory(user_id)
        if facts:
            context.append({
                "role": "system",
                "content": "Что ты помнишь о собеседнике:\n"
                           + "\n".join(f"- {fact}" for fact in facts),
            })

        context.extend(await self._memory.get_short_memory(user_id))
        return context

    async def _save_mood(self, user_id: int, generation: int, mood: str) -> None:
        if not mood or not self._is_current(user_id, generation):
            return
        await self._settings_repo.update(user_id, mood=mood)
        if self._is_current(user_id, generation):
            logger.info("user_id=%s event=mood_updated", user_id)

    def _incoming_source_key(self, item: PendingMessage) -> str:
        if item.durable_id is not None:
            return f"incoming:{item.durable_id}"
        digest = hashlib.sha256(
            f"{item.created_at.isoformat()}\0{item.text}".encode("utf-8")
        ).hexdigest()[:24]
        return f"incoming:{digest}"

    def _assistant_source_key(
        self, taken: list[PendingMessage], index: int, chunk: str,
        message_id: int | None,
    ) -> str:
        ids = ",".join(str(item.durable_id or self._incoming_source_key(item)) for item in taken)
        digest = hashlib.sha256(
            f"{ids}\0{index}\0{message_id}\0{chunk}".encode("utf-8")
        ).hexdigest()
        return f"assistant:{digest}"

    async def _delete_pending_for_turn(self, taken: list[PendingMessage]) -> None:
        if self._pending is None:
            return
        ids = [item.durable_id for item in taken if item.durable_id is not None]
        if not ids:
            return
        try:
            await self._pending.delete_pending(ids)
        except Exception:
            # History имеет source_key: повтор после restart не создаст дубль.
            logger.exception("event=pending_turn_delete_failed ids=%s", ids)

    async def _persist_turn(
        self, user_id: int, generation: int, taken: list[PendingMessage],
        sent: list[str], message_ids: list[int | None], *, force: bool = False,
    ) -> bool:
        if not force and not self._is_current(user_id, generation):
            return False
        entries: list[tuple[str, str, datetime | None, str | None]] = [
            ("user", item.text, item.created_at, self._incoming_source_key(item))
            for item in taken
        ]
        last_ai_at: datetime | None = None
        for index, chunk in enumerate(sent):
            last_ai_at = now()
            message_id = message_ids[index] if index < len(message_ids) else None
            entries.append((
                "assistant", chunk, last_ai_at,
                self._assistant_source_key(taken, index, chunk, message_id),
            ))
        try:
            await self._history.add_many(user_id, entries)
            await self._delete_pending_for_turn(taken)
            return True
        except Exception:
            logger.exception("user_id=%s event=turn_persist_failed", user_id)
            return False

    async def _persist_proactive(
        self, user_id: int, generation: int, messages: list[str],
        message_ids: list[int | None], chat_id: int, *, force: bool = False,
    ) -> datetime | None:
        if not force and not self._is_current(user_id, generation):
            return None
        entries: list[tuple[str, str, datetime | None, str | None]] = []
        last_ai_at: datetime | None = None
        for index, message in enumerate(messages):
            last_ai_at = now()
            message_id = message_ids[index] if index < len(message_ids) else None
            digest = hashlib.sha256(
                f"{user_id}\0{chat_id}\0{generation}\0{message_id}\0{message}".encode("utf-8")
            ).hexdigest()
            entries.append(("assistant", message, last_ai_at, f"proactive:{digest}"))
        if not entries:
            return None
        try:
            await self._history.add_many(user_id, entries)
            return last_ai_at
        except Exception:
            logger.exception("user_id=%s event=proactive_persist_failed", user_id)
            return None

    # ------------------------------------------------------------------ #
    # main pipeline                                                       #
    # ------------------------------------------------------------------ #

    async def _pipeline(self, user_id: int, chat_id: int, generation: int) -> None:
        session = self._session(user_id)

        while True:
            taken: list[PendingMessage] | None = None
            consumed = False
            confirmed: list[str] = []
            confirmed_ids: list[int | None] = []
            typing_task: asyncio.Task | None = None
            rate_limited_until: float | None = None

            try:
                settings = await self._settings_repo.get(user_id)
                if not self._is_current(user_id, generation):
                    return
                model, custom_prompt, typing_glob, debounce = await self._runtime(settings)
                if not self._is_current(user_id, generation):
                    return
                typing_enabled = typing_glob and self._config.typing_simulation

                await asyncio.sleep(max(0.0, float(debounce)))
                if not self._is_current(user_id, generation):
                    return

                async with session.lock:
                    if not self._is_current(user_id, generation) or not session.buffer:
                        return
                    allowed, retry_at = await self._reserve_request(
                        user_id, session, generation
                    )
                    if not self._is_current(user_id, generation):
                        return
                    if not allowed:
                        if retry_at is not None:
                            self._schedule_rate_retry_locked(user_id, session, retry_at)
                            rate_limited_until = retry_at
                        if rate_limited_until is None:
                            return
                    else:
                        max_messages = self._max_buffer_messages()
                        max_chars = self._max_buffer_chars()
                        taken = []
                        chars = 0
                        for item in session.buffer[:max_messages]:
                            item_chars = len(item.text)
                            if taken and chars + item_chars > max_chars:
                                break
                            taken.append(item)
                            chars += item_chars
                        if not taken and session.buffer:
                            # Single oversized message is represented in context
                            # with an explicit truncation marker, never dropped.
                            taken = [session.buffer[0]]
                        selected_ids = {id(item) for item in taken}
                        session.buffer = [
                            item for item in session.buffer if id(item) not in selected_ids
                        ]
                        self._dedupe_and_sort_buffer(session)

                if rate_limited_until is not None:
                    await self._notify_rate_limit(
                        user_id, chat_id, generation, rate_limited_until
                    )
                    return

                assert taken is not None
                user_text = "\n".join(item.text for item in taken)
                if typing_enabled:
                    typing_task = asyncio.create_task(
                        self._sender.typing_keepalive(chat_id)
                    )

                logger.info(
                    "user_id=%s event=generation_started parts=%d", user_id, len(taken)
                )
                context = await self._build_context(
                    user_id, settings, custom_prompt,
                    last_user_at=taken[-1].created_at,
                )
                if not self._is_current(user_id, generation):
                    return
                context.extend(
                    {
                        "role": "user",
                        "content": f'<message role="user" timestamp="{iso(item.created_at)}">\n'
                                   f"{item.text}\n</message>",
                    }
                    for item in taken
                )
                context = self._fit_context(context, self._max_context_chars())

                raw = await self._ai.chat(
                    model=model, messages=context, json_mode=True
                )
                if not self._is_current(user_id, generation):
                    return
                logger.info("user_id=%s event=generation_completed", user_id)
                parsed = parse_response(raw)

                if not parsed.should_reply:
                    persisted = await self._persist_turn(
                        user_id, generation, taken, [], []
                    )
                    if not persisted:
                        return
                    consumed = True
                    await self._save_mood(user_id, generation, parsed.mood)
                    if not self._is_current(user_id, generation):
                        return
                    if session.buffer:
                        continue
                    return

                def on_progress(progress: SendProgress) -> None:
                    # Sync callback: ни один await не может возникнуть между
                    # подтверждением Telegram и обновлением confirmed state.
                    # Одинаковые тексты допустимы и считаются отдельными chunks.
                    confirmed.append(progress.chunk)
                    confirmed_ids.append(progress.message_id)

                sent_result = await self._send_messages(
                    chat_id=chat_id,
                    user_id=user_id,
                    messages=parsed.messages,
                    typing_enabled=typing_enabled,
                    typing_task=typing_task,
                    progress_callback=on_progress,
                )
                normalized_chunks, normalized_ids = self._normalize_send_result(sent_result)
                if len(normalized_chunks) > len(confirmed):
                    confirmed = list(normalized_chunks)
                    confirmed_ids = list(normalized_ids) + [
                        None
                    ] * (len(confirmed) - len(normalized_ids))
                elif len(confirmed_ids) < len(confirmed):
                    confirmed_ids.extend([None] * (len(confirmed) - len(confirmed_ids)))

                if not self._is_current(user_id, generation):
                    # A user-requested clear must not resurrect a turn into
                    # history after the clear operation.  New-message and
                    # mood-only invalidations still preserve confirmed chunks.
                    reason = self._reason_for_task(asyncio.current_task())
                    if reason is CancelReason.CLEAR:
                        consumed = True
                    else:
                        persisted = await self._persist_turn(
                            user_id, generation, taken, confirmed, confirmed_ids, force=True
                        )
                        consumed = persisted or bool(confirmed)
                    return

                persisted = await self._persist_turn(
                    user_id, generation, taken, confirmed, confirmed_ids
                )
                if not persisted:
                    return
                consumed = True
                last_ai_at = now() if confirmed else None
                await self._save_mood(user_id, generation, parsed.mood)
                await self._maybe_notify_memory_limit(
                    user_id, chat_id, generation, added=len(taken) + len(confirmed)
                )
                if not self._is_current(user_id, generation):
                    return

                self._schedule_memory_extraction(
                    user_id=user_id,
                    model=model,
                    user_text=user_text,
                    assistant_text="\n".join(confirmed),
                    user_at=taken[-1].created_at,
                    assistant_at=last_ai_at,
                )
                if session.buffer and self._is_current(user_id, generation):
                    continue
                return

            except asyncio.CancelledError:
                reason = self._reason_for_task(asyncio.current_task())
                if confirmed:
                    if reason is CancelReason.CLEAR:
                        # The user explicitly removed the dialog.  Do not
                        # recreate it from chunks that were already in flight.
                        consumed = True
                    else:
                        cleanup = self._persist_turn(
                            user_id, generation, taken or [], confirmed, confirmed_ids,
                            force=True,
                        )
                        error, _repeat = await self._finish_cleanup(cleanup)
                        if error is not None:
                            logger.error(
                                "user_id=%s event=partial_send_persist_failed error=%s",
                                user_id, error,
                            )
                        consumed = True
                if taken is not None and not consumed and reason.returns_taken:
                    self._restore_taken(session, taken)
                elif taken is not None and not consumed:
                    logger.info(
                        "user_id=%s event=incomplete_turn_dropped reason=%s",
                        user_id, reason.value,
                    )
                raise
            except (AIClientError, TelegramAPIError) as exc:
                reason = self._reason_for_task(asyncio.current_task())
                if confirmed:
                    if reason is CancelReason.CLEAR:
                        consumed = True
                    else:
                        cleanup = self._persist_turn(
                            user_id, generation, taken or [], confirmed, confirmed_ids,
                            force=True,
                        )
                        error, _repeat = await self._finish_cleanup(cleanup)
                        if error is not None:
                            logger.error(
                                "user_id=%s event=partial_send_persist_failed error=%s",
                                user_id, error,
                            )
                        consumed = True
                if taken is not None and not consumed:
                    if reason is not CancelReason.CLEAR:
                        if self._is_current(user_id, generation):
                            self._restore_taken(session, taken)
                        elif self._sessions[user_id].cancel_reason.returns_taken:
                            self._restore_taken(session, taken)
                logger.warning(
                    "user_id=%s event=conversation_api_error error=%s", user_id, exc
                )
                return
            except Exception:
                reason = self._reason_for_task(asyncio.current_task())
                if confirmed:
                    if reason is CancelReason.CLEAR:
                        consumed = True
                    else:
                        cleanup = self._persist_turn(
                            user_id, generation, taken or [], confirmed, confirmed_ids,
                            force=True,
                        )
                        error, _repeat = await self._finish_cleanup(cleanup)
                        if error is not None:
                            logger.error(
                                "user_id=%s event=partial_send_persist_failed error=%s",
                                user_id, error,
                            )
                        consumed = True
                if taken is not None and not consumed and self._is_current(user_id, generation):
                    self._restore_taken(session, taken)
                logger.exception("user_id=%s event=pipeline_error", user_id)
                return
            finally:
                if typing_task is not None:
                    typing_task.cancel()
                    try:
                        await asyncio.shield(typing_task)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        logger.exception("user_id=%s event=typing_cleanup_failed", user_id)

    # ------------------------------------------------------------------ #
    # sender compatibility and progress                                  #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _normalize_send_result(result) -> tuple[list[str], list[int | None]]:
        if result is None:
            return [], []
        if isinstance(result, SendResult):
            return list(result.confirmed_chunks), list(result.message_ids)
        if isinstance(result, (list, tuple)):
            chunks = [str(item) for item in result]
            return chunks, [None] * len(chunks)
        if isinstance(result, str):
            return [result], [None]
        try:
            chunks = [str(item) for item in result]
        except TypeError:
            return [], []
        return chunks, [None] * len(chunks)

    def _sender_supports_progress(self) -> bool:
        try:
            signature = inspect.signature(self._sender.send_messages)
        except (TypeError, ValueError):
            return True
        parameters = signature.parameters
        return "progress_callback" in parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )

    async def _send_messages(
        self, *, chat_id: int, user_id: int, messages: list[str],
        typing_enabled: bool, typing_task: asyncio.Task | None = None,
        progress_callback=None,
    ):
        kwargs = {
            "chat_id": chat_id,
            "user_id": user_id,
            "messages": messages,
            "typing_enabled": typing_enabled,
            "typing_task": typing_task,
        }
        if self._sender_supports_progress():
            kwargs["progress_callback"] = progress_callback
        return await self._sender.send_messages(**kwargs)

    def _schedule_memory_extraction(self, **kwargs) -> None:
        scheduler = getattr(self._memory, "schedule_extraction", None)
        if scheduler is not None:
            scheduler(**kwargs)
            return
        # Совместимость с custom MemoryService без lifecycle scheduler.
        task = asyncio.create_task(self._memory.maybe_extract_facts(**kwargs))
        self._background_tasks.add(task)
        self._request_cancel(task, CancelReason.SHUTDOWN)

        def _discard(done: asyncio.Task) -> None:
            self._background_tasks.discard(done)
            self._task_reasons.pop(done, None)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("event=memory_fallback_task_failed")

        task.add_done_callback(_discard)

    async def _maybe_notify_memory_limit(
        self, user_id: int, chat_id: int, generation: int, added: int,
    ) -> None:
        """После пересечения лимита памяти один раз сообщает пользователю."""
        limit = self._config.short_memory_limit
        total = await self._history.count(user_id)
        if not self._is_current(user_id, generation):
            return
        before = total - added
        if before <= limit < total:
            await self._send_messages(
                chat_id=chat_id,
                user_id=user_id,
                messages=[
                    f"кстати, мы уже настрочили больше {limit} сообщений 🙈 "
                    "я начинаю забывать самые первые. если хочешь начать с чистого "
                    "листа — /start → 🧹 очистить диалог"
                ],
                typing_enabled=False,
            )
            if self._is_current(user_id, generation):
                logger.info("user_id=%s event=memory_limit_notified total=%d", user_id, total)

    # ------------------------------------------------------------------ #
    # proactive: idempotent loop, guarded side effects                     #
    # ------------------------------------------------------------------ #

    def _proactive_delay_seconds(self) -> float:
        """Broad random delay for the next eligibility check."""
        return self._sample_proactive_delay()

    def _sample_proactive_delay(self) -> float:
        """Weighted random delay: minutes are possible, hours are common too."""
        cfg = self._config
        low = max(1.0, cfg.proactive_min_delay_minutes * 60)
        high = max(low, cfg.proactive_max_delay_minutes * 60)
        return math.exp(random.uniform(math.log(low), math.log(high)))

    def _schedule_proactive(self, session: UserSession) -> None:
        session.proactive_due_at = time.monotonic() + self._sample_proactive_delay()

    def _cancel_proactive_plan(self, session: UserSession) -> None:
        """Invalidate a due/approved plan without touching mood state."""
        session.proactive_waiting_to_send = False
        session.proactive_due_at = float("inf")

    def _initiative_probability(self, initiative: str, settings) -> float:
        """Code-side stochastic gate; mood itself is owned by the existing AI algorithm."""
        value = {"NO": 0.0, "MAYBE": 0.18, "YES": 0.68}.get(initiative, 0.0)
        mood = (getattr(settings, "mood", "") or "").lower()
        if any(word in mood for word in ("обид", "зл", "раздраж", "недоволь", "ссор", "конфликт")):
            value *= 0.18
        elif any(word in mood for word in ("устав", "груст", "тревож", "плох")):
            value *= 0.45
        return max(0.0, min(1.0, value))

    async def restore_sessions(self, within_hours: float = 48.0) -> None:
        """Восстанавливает runtime-сессии и durable pending FIFO после restart."""
        active = await self._settings_repo.get_recently_active(within_hours * 3600)
        now_mono = time.monotonic()
        now_ts = time.time()
        records: list[PendingMessageRecord] = []
        if self._pending is not None:
            try:
                records = await self._pending.list_all_pending()
            except Exception:
                logger.exception("event=pending_restore_failed")

        active_by_user = {settings.user_id: settings for settings in active}
        pending_by_user: dict[int, list[PendingMessageRecord]] = {}
        for record in records:
            pending_by_user.setdefault(record.user_id, []).append(record)

        for user_id in set(active_by_user) | set(pending_by_user):
            session = self._session(user_id)
            async with session.lock:
                # Повторный restore во время активного turn не должен инвалидировать
                # его generation или возвращать уже взятые FIFO items во второй раз.
                if session.task is not None and not session.task.done():
                    continue
                if (
                    session.rate_limit_retry_task is not None
                    and not session.rate_limit_retry_task.done()
                ):
                    continue
                settings = active_by_user.get(user_id)
                if settings is not None:
                    session.last_chat_id = settings.last_chat_id
                    session.last_activity = now_mono - max(
                        0.0, now_ts - settings.last_activity_ts
                    )
                user_records = pending_by_user.get(user_id, [])
                if user_records:
                    for record in user_records:
                        session.buffer.append(self._record_to_pending(record))
                    session.last_chat_id = user_records[-1].chat_id
                    latest_pending_ts = max(
                        record.created_at.timestamp() for record in user_records
                    )
                    session.last_activity = now_mono - max(
                        0.0, now_ts - latest_pending_ts
                    )
                    session.generation_id += 1
                    self._cancel_proactive_plan(session)
                    self._schedule_proactive(session)
                    self._dedupe_and_sort_buffer(session)
                    if session.task is None or session.task.done():
                        session.task = asyncio.create_task(
                            self._pipeline(
                                user_id,
                                session.last_chat_id,
                                session.generation_id,
                            )
                        )
        if active:
            logger.info(
                "event=sessions_restored active=%d pending=%d",
                len(active), len(records),
            )

    def start_proactive_loop(self) -> None:
        """Idempotent: повторные вызовы не создают второй scheduler."""
        if self._closing or not self._config.proactive_enabled:
            logger.info("event=proactive_disabled")
            return
        if self._proactive_task is not None and not self._proactive_task.done():
            return
        self._proactive_task = asyncio.create_task(self._proactive_loop())
        logger.info(
            "event=proactive_started delay=%.0f-%.0fm max_messages=%d",
            self._config.proactive_min_delay_minutes,
            self._config.proactive_max_delay_minutes,
            self._config.proactive_max_messages,
        )

    async def _proactive_loop(self) -> None:
        current_task = asyncio.current_task()
        try:
            while True:
                await asyncio.sleep(self._config.proactive_check_interval)
                tasks: list[asyncio.Task] = []
                for user_id, session in list(self._sessions.items()):
                    if self._closing:
                        return
                    if session.proactive_task and not session.proactive_task.done():
                        continue
                    task = asyncio.create_task(self._maybe_proactive(user_id, session))
                    session.proactive_task = task
                    tasks.append(task)
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        if isinstance(result, Exception) and not isinstance(
                            result, asyncio.CancelledError
                        ):
                            logger.error("event=proactive_error error=%s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("event=proactive_loop_failed")
        finally:
            if self._proactive_task is current_task:
                self._proactive_task = None

    async def _maybe_proactive(self, user_id: int, session: UserSession) -> None:
        """Один code-controlled TIMING/DECISION cycle."""
        generation = session.generation_id
        if session.last_chat_id is None:
            return
        if session.proactive_count_since_user >= max(
            1, self._config.proactive_max_messages
        ):
            return
        if session.task and not session.task.done():
            return
        if time.monotonic() < session.proactive_due_at:
            return
        if session.last_proactive_at and (
            time.monotonic() - session.last_proactive_at
            < self._config.proactive_cooldown_minutes * 60
        ):
            if self._is_current(user_id, generation):
                session.proactive_waiting_to_send = False
                self._schedule_proactive(session)
            return
        if not await self._memory.get_short_memory(user_id):
            if self._is_current(user_id, generation):
                session.proactive_waiting_to_send = False
                self._schedule_proactive(session)
            return
        if not self._is_current(user_id, generation):
            return

        if session.proactive_waiting_to_send:
            await self._send_proactive_message(user_id, session, generation)
        else:
            await self._run_proactive_decision(user_id, session, generation)

    async def _run_proactive_decision(
        self, user_id: int, session: UserSession, generation: int,
    ) -> None:
        settings = await self._settings_repo.get(user_id)
        if not self._is_current(user_id, generation):
            return
        model, custom_prompt, _, _ = await self._runtime(settings)
        allowed_request, retry_at = await self._reserve_request(
            user_id, session, generation
        )
        if not self._is_current(user_id, generation):
            return
        if not allowed_request:
            if retry_at is not None:
                session.proactive_due_at = time.monotonic() + max(
                    0.05, retry_at - time.time()
                )
            return

        idle_minutes = max(0.0, (time.monotonic() - session.last_activity) / 60)
        silence = (
            f"{idle_minutes:.1f} часов"
            if idle_minutes >= 60
            else f"{int(idle_minutes)} минут"
        )
        logger.info(
            "user_id=%s event=proactive_decision_started sent=%d mood=%s",
            user_id, session.proactive_count_since_user, settings.mood,
        )
        context = await self._build_context(user_id, settings, custom_prompt)
        if not self._is_current(user_id, generation):
            return
        context = self._fit_context(context, self._max_context_chars())
        decision_timestamp = now()
        context.append({
            "role": "user",
            "content": (
                f'<message role="system" timestamp="{iso(decision_timestamp)}">\n'
                f"{PROACTIVE_DECISION_PROMPT.format(silence=silence)}\n"
                "</message>"
            ),
        })
        context = self._fit_context(context, self._max_context_chars())
        try:
            raw = await self._ai.chat(model=model, messages=context, json_mode=True)
            if not self._is_current(user_id, generation):
                return
            initiative = parse_initiative(raw)
            probability = self._initiative_probability(initiative, settings)
            allowed = random.random() < probability
            logger.info(
                "user_id=%s event=proactive_decision result=%s probability=%.2f allowed=%s",
                user_id, initiative, probability, allowed,
            )
            if not allowed:
                session.proactive_waiting_to_send = False
                self._schedule_proactive(session)
                return

            session.proactive_waiting_to_send = True
            self._schedule_proactive(session)
            logger.info(
                "user_id=%s event=proactive_timing_scheduled due=%.0f initiative=%s",
                user_id, session.proactive_due_at, initiative,
            )
        except asyncio.CancelledError:
            raise
        except AIClientError as exc:
            if self._is_current(user_id, generation):
                session.proactive_waiting_to_send = False
                self._schedule_proactive(session)
            logger.warning(
                "user_id=%s event=proactive_decision_api_error error=%s", user_id, exc
            )
        except Exception:
            if self._is_current(user_id, generation):
                session.proactive_waiting_to_send = False
                self._schedule_proactive(session)
            logger.exception("user_id=%s event=proactive_decision_error", user_id)

    async def _send_proactive_message(
        self, user_id: int, session: UserSession, generation: int,
    ) -> None:
        """Send one already-timed proactive turn, then forget the plan."""
        chat_id = session.last_chat_id
        if chat_id is None:
            session.proactive_waiting_to_send = False
            return
        settings = await self._settings_repo.get(user_id)
        if not self._is_current(user_id, generation):
            return
        model, custom_prompt, typing_glob, _ = await self._runtime(settings)
        allowed_request, retry_at = await self._reserve_request(
            user_id, session, generation
        )
        if not self._is_current(user_id, generation):
            return
        if not allowed_request:
            if retry_at is not None:
                session.proactive_due_at = time.monotonic() + max(
                    0.05, retry_at - time.time()
                )
            return

        typing_enabled = typing_glob and self._config.typing_simulation
        message_context = await self._build_context(user_id, settings, custom_prompt)
        if not self._is_current(user_id, generation):
            return
        message_context = self._fit_context(message_context, self._max_context_chars())
        message_timestamp = now()
        message_context.append({
            "role": "user",
            "content": (
                f'<message role="system" timestamp="{iso(message_timestamp)}">\n'
                f"{PROACTIVE_MESSAGE_PROMPT}\n"
                "</message>"
            ),
        })
        message_context = self._fit_context(message_context, self._max_context_chars())
        typing_task: asyncio.Task | None = None
        confirmed: list[str] = []
        confirmed_ids: list[int | None] = []
        try:
            raw = await self._ai.chat(model=model, messages=message_context, json_mode=True)
            if not self._is_current(user_id, generation):
                return
            parsed = parse_response(raw)
            if not parsed.should_reply or not parsed.messages:
                session.proactive_waiting_to_send = False
                self._schedule_proactive(session)
                return
            parsed.messages = parsed.messages[:1]
            if typing_enabled:
                typing_task = asyncio.create_task(
                    self._sender.typing_keepalive(chat_id)
                )

            def on_progress(progress: SendProgress) -> None:
                confirmed.append(progress.chunk)
                confirmed_ids.append(progress.message_id)

            result = await self._send_messages(
                chat_id=chat_id,
                user_id=user_id,
                messages=parsed.messages,
                typing_enabled=typing_enabled,
                typing_task=typing_task,
                progress_callback=on_progress,
            )
            chunks, message_ids = self._normalize_send_result(result)
            if len(chunks) > len(confirmed):
                confirmed = list(chunks)
                confirmed_ids = list(message_ids) + [
                    None
                ] * (len(confirmed) - len(message_ids))
            elif len(confirmed_ids) < len(confirmed):
                confirmed_ids.extend([None] * (len(confirmed) - len(confirmed_ids)))

            if not self._is_current(user_id, generation):
                reason = self._reason_for_task(asyncio.current_task())
                if reason is not CancelReason.CLEAR:
                    await self._persist_proactive(
                        user_id, generation, confirmed, confirmed_ids, chat_id,
                        force=True,
                    )
                return
            last_ai_ts = await self._persist_proactive(
                user_id, generation, confirmed, confirmed_ids, chat_id
            )
            if last_ai_ts is None:
                return
            if not self._is_current(user_id, generation):
                return
            session.proactive_waiting_to_send = False
            session.proactive_count_since_user += 1
            session.last_proactive_at = time.monotonic()
            session.last_activity = session.last_proactive_at
            await self._save_mood(user_id, generation, parsed.mood)
            if not self._is_current(user_id, generation):
                return
            await self._settings_repo.update(
                user_id,
                last_activity_ts=last_ai_ts.timestamp(),
                last_ai_message_ts=last_ai_ts.timestamp(),
                proactive_stage=session.proactive_count_since_user,
            )
            if not self._is_current(user_id, generation):
                return
            self._schedule_proactive(session)
            logger.info(
                "user_id=%s event=proactive_sent messages=%d",
                user_id, len(confirmed),
            )
        except asyncio.CancelledError:
            reason = self._reason_for_task(asyncio.current_task())
            if confirmed and reason is not CancelReason.CLEAR:
                await self._persist_proactive(
                    user_id, generation, confirmed, confirmed_ids, chat_id,
                    force=True,
                )
            raise
        except (AIClientError, TelegramAPIError) as exc:
            reason = self._reason_for_task(asyncio.current_task())
            if confirmed and reason is not CancelReason.CLEAR:
                await self._persist_proactive(
                    user_id, generation, confirmed, confirmed_ids, chat_id,
                    force=True,
                )
            if self._is_current(user_id, generation):
                session.proactive_waiting_to_send = False
                self._schedule_proactive(session)
            logger.warning(
                "user_id=%s event=proactive_message_api_error error=%s", user_id, exc
            )
        except Exception:
            reason = self._reason_for_task(asyncio.current_task())
            if confirmed and reason is not CancelReason.CLEAR:
                await self._persist_proactive(
                    user_id, generation, confirmed, confirmed_ids, chat_id,
                    force=True,
                )
            if self._is_current(user_id, generation):
                session.proactive_waiting_to_send = False
                self._schedule_proactive(session)
            logger.exception("user_id=%s event=proactive_message_error", user_id)
        finally:
            if typing_task is not None:
                typing_task.cancel()
                try:
                    await asyncio.shield(typing_task)
                except asyncio.CancelledError:
                    pass
                except Exception:
                    logger.exception("user_id=%s event=typing_cleanup_failed", user_id)
