"""Conversation Manager — центральный сервис состояния диалога (п. 22 ТЗ).

Знает: буфер входящих сообщений, debounce-таймер, активную генерацию,
актуальность ответа (generation id), typing и отправку, настроение,
проактивные сообщения после долгого молчания.

Модель конкурентности (п. 7 ТЗ):
- каждое новое сообщение пользователя увеличивает generation_id;
- предыдущая задача (debounce / генерация / ожидание перед отправкой)
  отменяется, её сообщения возвращаются в буфер и попадут в новый ответ;
- если отправка уже началась — отправленные сообщения сохраняются в историю,
  чтобы новая генерация видела их в контексте;
- устаревший ответ никогда не отправляется.

Typing (улучшение 6): индикатор «печатает…» запускается сразу после
debounce — ещё до ответа модели — и держится до отправки, как у человека,
который уже начал отвечать.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime

from app.ai.client import AIClient, AIClientError
from app.ai.prompts import (
    PROACTIVE_DECISION_PROMPT,
    PROACTIVE_MESSAGE_PROMPT,
    build_system_prompt,
)
from app.ai.response_parser import parse_initiative, parse_response
from app.config import MSK, Config
from app.conversation.memory import MemoryService
from app.conversation.sender import TelegramSender
from app.database.repository import HistoryRepository, UserSettingsRepository
from app.time_context import TIMEZONE_NAME, elapsed, iso, now

logger = logging.getLogger(__name__)


@dataclass
class PendingMessage:
    text: str
    created_at: datetime


@dataclass
class UserSession:
    buffer: list[PendingMessage] = field(default_factory=list)
    generation_id: int = 0
    task: asyncio.Task | None = None
    last_activity: float = field(default_factory=time.monotonic)
    last_chat_id: int | None = None
    proactive_stage: int | None = None   # legacy DB field; scheduler bookkeeping only
    proactive_due_at: float = 0.0
    proactive_count_since_user: int = 0
    last_proactive_at: float = 0.0
    proactive_waiting_to_send: bool = False
    proactive_task: asyncio.Task | None = None
    last_morning_date: object = None     # дата последнего «доброго утра» (МСК)


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
    ):
        self._config = config
        self._ai = ai_client
        self._sender = sender
        self._memory = memory
        self._settings_repo = settings_repo
        self._history = history_repo
        self._global = global_repo
        self._sessions: dict[int, UserSession] = {}
        self._proactive_task: asyncio.Task | None = None

    def _session(self, user_id: int) -> UserSession:
        if user_id not in self._sessions:
            self._sessions[user_id] = UserSession(
                proactive_due_at=time.monotonic() + self._proactive_delay_seconds()
            )
        return self._sessions[user_id]

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
        # Existing mood is a factor, not a new mood system and is never
        # rewritten here.  Negative mood strongly suppresses initiative.
        mood = (getattr(settings, "mood", "") or "").lower()
        if any(word in mood for word in ("обид", "зл", "раздраж", "недоволь", "ссор", "конфликт")):
            value *= 0.18
        elif any(word in mood for word in ("устав", "груст", "тревож", "плох")):
            value *= 0.45
        return max(0.0, min(1.0, value))

    async def handle_message(self, user_id: int, chat_id: int, text: str) -> None:
        """Точка входа из Telegram handler. Handler остаётся тонким (п. 22 ТЗ)."""
        session = self._session(user_id)
        received_at = now()
        session.buffer.append(PendingMessage(text=text, created_at=received_at))
        session.generation_id += 1
        session.last_activity = time.monotonic()
        session.last_chat_id = chat_id
        # пользователь вернулся — эскалация сбрасывается (настроение остаётся:
        # его сменит сама модель, когда «оттает»)
        session.proactive_stage = 0
        session.proactive_count_since_user = 0
        session.last_proactive_at = 0.0
        self._cancel_proactive_plan(session)
        self._schedule_proactive(session)
        await self._settings_repo.update(
            user_id,
            proactive_stage=0,
            last_activity_ts=received_at.timestamp(),
            last_chat_id=chat_id,
            last_user_message_ts=received_at.timestamp(),
        )
        generation = session.generation_id

        if session.task and not session.task.done():
            session.task.cancel()
        if session.proactive_task and not session.proactive_task.done():
            session.proactive_task.cancel()

        session.task = asyncio.create_task(
            self._pipeline(user_id, chat_id, generation)
        )

    async def cancel_active(self, user_id: int) -> None:
        """Отменяет активную генерацию (например, при очистке диалога)."""
        session = self._session(user_id)
        session.generation_id += 1
        session.buffer.clear()
        session.proactive_stage = 0  # сбрасываем кэш стадии при любой очистке
        self._cancel_proactive_plan(session)
        if session.task and not session.task.done():
            session.task.cancel()
            try:
                await session.task
            except asyncio.CancelledError:
                pass
        if session.proactive_task and not session.proactive_task.done():
            session.proactive_task.cancel()
            try:
                await session.proactive_task
            except asyncio.CancelledError:
                pass

    async def shutdown(self) -> None:
        if self._proactive_task and not self._proactive_task.done():
            self._proactive_task.cancel()
            try:
                await self._proactive_task
            except asyncio.CancelledError:
                pass
        for user_id in list(self._sessions):
            await self.cancel_active(user_id)

    # ------------------------------------------------------------------ #
    # основной pipeline                                                   #
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
        system_prompt = build_system_prompt(
            settings.personality,
            custom_prompt or settings.custom_prompt,
            settings.mood,
            getattr(settings, "custom_personality", ""),
        )
        context: list[dict] = [{"role": "system", "content": system_prompt}]

        # Rebuilt for every request: the model must not infer current time from
        # a previous request.
        now_msk = now()
        latest = await self._history.get_last_timestamps(user_id)
        last_user_at = last_user_at or latest["user"]
        last_ai_at = last_ai_at or latest["assistant"]
        # History may have been cleared; settings still retain the latest
        # event timestamps so the model receives a truthful time context.
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
                "content": "Что ты помнишь о собеседнике:\n" + "\n".join(f"- {f}" for f in facts),
            })

        context.extend(await self._memory.get_short_memory(user_id))
        return context

    async def _save_mood(self, user_id: int, mood: str) -> None:
        if mood:
            await self._settings_repo.update(user_id, mood=mood)
            logger.info("user_id=%s event=mood_updated", user_id)

    async def _pipeline(self, user_id: int, chat_id: int, generation: int) -> None:
        session = self._session(user_id)
        taken: list[PendingMessage] | None = None
        consumed = False  # сообщения сохранены в историю или отвечены
        typing_task: asyncio.Task | None = None

        try:
            settings = await self._settings_repo.get(user_id)
            model, custom_prompt, typing_glob, debounce = await self._runtime(settings)
            typing_enabled = typing_glob and self._config.typing_simulation

            # --- debounce: ждём, пока пользователь допишет (п. 2 ТЗ) ---
            await asyncio.sleep(debounce)
            if generation != session.generation_id or not session.buffer:
                return

            # забираем накопленные сообщения как единую реплику
            taken = session.buffer.copy()
            session.buffer.clear()
            user_text = "\n".join(item.text for item in taken)

            # --- typing начинается почти сразу, ещё до ответа модели ---
            if typing_enabled:
                typing_task = asyncio.create_task(self._sender.typing_keepalive(chat_id))

            # --- генерация ответа (максимально быстро, без sleep — п. 3 ТЗ) ---
            logger.info("user_id=%s event=generation_started parts=%d", user_id, len(taken))
            context = await self._build_context(
                user_id, settings, custom_prompt,
                last_user_at=taken[-1].created_at,
            )
            context.extend({
                "role": "user",
                "content": f'<message role="user" timestamp="{iso(item.created_at)}">\n'
                            f"{item.text}\n</message>",
            } for item in taken)

            raw = await self._ai.chat(
                model=model, messages=context, json_mode=True
            )
            logger.info("user_id=%s event=generation_completed", user_id)

            # контекст мог измениться, пока модель думала
            if generation != session.generation_id:
                session.buffer = taken + session.buffer
                return

            parsed = parse_response(raw)
            await self._save_mood(user_id, parsed.mood)

            # --- решение «отвечать или не отвечать» приняла модель (п. 8 ТЗ) ---
            if not parsed.should_reply:
                consumed = True
                for item in taken:
                    await self._history.add(user_id, "user", item.text, item.created_at)
                logger.info("user_id=%s event=response_skipped", user_id)
                return

            # --- отправка с естественным временем набора (п. 4, 5, 6 ТЗ) ---
            sent: list[str] = []
            try:
                sent = await self._sender.send_messages(
                    chat_id=chat_id,
                    user_id=user_id,
                    messages=parsed.messages,
                    typing_enabled=typing_enabled,
                    typing_task=typing_task,
                )
            except asyncio.CancelledError:
                # пользователь написал, пока мы «печатали»: частично отправленное
                # сохраняем в историю, чтобы новая генерация видела контекст
                consumed = True
                for item in taken:
                    await self._history.add(user_id, "user", item.text, item.created_at)
                partial_ai_ts = None
                for s in sent:
                    partial_ai_ts = now()
                    await self._history.add(user_id, "assistant", s, partial_ai_ts)
                if partial_ai_ts:
                    await self._settings_repo.update(
                        user_id, last_ai_message_ts=partial_ai_ts.timestamp()
                    )
                raise

            consumed = True
            for item in taken:
                await self._history.add(user_id, "user", item.text, item.created_at)
            for s in sent:
                sent_at = now()
                await self._history.add(user_id, "assistant", s, sent_at)
            if sent:
                await self._settings_repo.update(
                    user_id, last_ai_message_ts=sent_at.timestamp(),
                )

            # уведомление о достижении лимита памяти (один раз при пересечении)
            await self._maybe_notify_memory_limit(user_id, chat_id, added=1 + len(sent))

            # долгосрочная память — фоном, не блокируя диалог
            asyncio.create_task(
                self._memory.maybe_extract_facts(
                    user_id, model, user_text, "\n".join(sent),
                    taken[-1].created_at if taken else None,
                    sent_at if sent else None,
                )
            )

        except asyncio.CancelledError:
            # сообщения не обработаны — возвращаем в буфер для новой генерации
            if taken is not None and not consumed:
                session.buffer = taken + session.buffer
            raise
        except AIClientError as e:
            logger.warning("user_id=%s event=api_error error=%s", user_id, e)
        except Exception:
            logger.exception("user_id=%s event=pipeline_error", user_id)
        finally:
            if typing_task is not None:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass

    async def _maybe_notify_memory_limit(self, user_id: int, chat_id: int, added: int) -> None:
        """После пересечения лимита памяти один раз сообщает об этом пользователю.

        История в БД хранится полностью, но в контекст модели уходят только
        последние short_memory_limit сообщений — о пересечении честно пишем.
        """
        limit = self._config.short_memory_limit
        total = await self._history.count(user_id)
        before = total - added
        if before <= limit < total:
            await self._sender.send_messages(
                chat_id=chat_id,
                user_id=user_id,
                messages=[
                    f"кстати, мы уже настрочили больше {limit} сообщений 🙈 "
                    "я начинаю забывать самые первые. если хочешь начать с чистого "
                    "листа — /start → 🧹 очистить диалог"
                ],
                typing_enabled=False,
            )
            logger.info("user_id=%s event=memory_limit_notified total=%d", user_id, total)

    # ------------------------------------------------------------------ #
    # проактивность: персонаж пишет первой после долгого молчания         #
    # ------------------------------------------------------------------ #

    async def restore_sessions(self, within_hours: float = 48.0) -> None:
        """Восстанавливает сессии из БД после перезапуска.

        Без этого проактивность и «доброе утро» работали только для тех,
        кто написал после старта бота: last_activity жил в памяти процесса.
        """
        active = await self._settings_repo.get_recently_active(within_hours * 3600)
        now_mono = time.monotonic()
        now_ts = time.time()
        for s in active:
            session = self._session(s.user_id)
            session.last_chat_id = s.last_chat_id
            # last_activity в сессии — monotonic; конвертируем из epoch,
            # чтобы молчание «до перезапуска» учитывалось
            session.last_activity = now_mono - max(0.0, now_ts - s.last_activity_ts)
            # The old staged scheduler is intentionally not restored as a
            # mandatory sequence.  Start a fresh contextual decision window.
            session.proactive_stage = 0
            session.proactive_count_since_user = 0
            self._cancel_proactive_plan(session)
            self._schedule_proactive(session)
        if active:
            logger.info("event=sessions_restored count=%d", len(active))

    def start_proactive_loop(self) -> None:
        if not self._config.proactive_enabled:
            logger.info("event=proactive_disabled")
            return
        self._proactive_task = asyncio.create_task(self._proactive_loop())
        logger.info(
            "event=proactive_started delay=%.0f-%.0fm max_messages=%d",
            self._config.proactive_min_delay_minutes,
            self._config.proactive_max_delay_minutes,
            self._config.proactive_max_messages,
        )

    async def _proactive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._config.proactive_check_interval)
                tasks: list[asyncio.Task] = []
                for user_id, session in list(self._sessions.items()):
                    if session.proactive_task and not session.proactive_task.done():
                        continue
                    task = asyncio.create_task(self._maybe_proactive(user_id, session))
                    session.proactive_task = task
                    tasks.append(task)
                if tasks:
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for result in results:
                        if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                            logger.error("event=proactive_error error=%s", result)
        except asyncio.CancelledError:
            raise

    async def _maybe_proactive(self, user_id: int, session: UserSession) -> None:
        """Run one code-controlled TIMING/DECISION cycle.

        A due time first permits a semantic DECISION.  Only after NO/MAYBE/YES
        has been evaluated does code sample a separate send time.  Therefore a
        decision never silently turns into an immediately scheduled message.
        """
        if session.last_chat_id is None:
            return
        if session.proactive_count_since_user >= max(1, self._config.proactive_max_messages):
            return
        if session.task and not session.task.done():
            return
        if time.monotonic() < session.proactive_due_at:
            return
        if session.last_proactive_at and (
            time.monotonic() - session.last_proactive_at
            < self._config.proactive_cooldown_minutes * 60
        ):
            session.proactive_waiting_to_send = False
            self._schedule_proactive(session)
            return
        if not await self._memory.get_short_memory(user_id):
            session.proactive_waiting_to_send = False
            self._schedule_proactive(session)
            return

        if session.proactive_waiting_to_send:
            await self._send_proactive_message(user_id, session)
            return

        await self._run_proactive_decision(user_id, session)

    async def _run_proactive_decision(self, user_id: int, session: UserSession) -> None:
        """DECISION: model supplies NO/MAYBE/YES; code applies probability."""
        generation = session.generation_id
        settings = await self._settings_repo.get(user_id)
        model, custom_prompt, _, _ = await self._runtime(settings)
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
        decision_timestamp = now()
        context.append({
            "role": "user",
            "content": (
                f'<message role="system" timestamp="{iso(decision_timestamp)}">\n'
                f"{PROACTIVE_DECISION_PROMPT.format(silence=silence)}\n"
                "</message>"
            ),
        })
        try:
            raw = await self._ai.chat(model=model, messages=context, json_mode=True)
            if generation != session.generation_id:
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

            # DECISION passed.  TIMING is sampled only now, and this cycle
            # returns without generating/sending anything yet.
            session.proactive_waiting_to_send = True
            self._schedule_proactive(session)
            logger.info(
                "user_id=%s event=proactive_timing_scheduled due=%.0f initiative=%s",
                user_id, session.proactive_due_at, initiative,
            )
        except asyncio.CancelledError:
            raise
        except AIClientError as e:
            session.proactive_waiting_to_send = False
            self._schedule_proactive(session)
            logger.warning("user_id=%s event=proactive_decision_api_error error=%s", user_id, e)
        except Exception:
            session.proactive_waiting_to_send = False
            self._schedule_proactive(session)
            logger.exception("user_id=%s event=proactive_decision_error", user_id)

    async def _send_proactive_message(self, user_id: int, session: UserSession) -> None:
        """Send one already-timed proactive turn, then forget the plan."""
        generation = session.generation_id
        chat_id = session.last_chat_id
        if chat_id is None:
            session.proactive_waiting_to_send = False
            return
        settings = await self._settings_repo.get(user_id)
        model, custom_prompt, typing_glob, _ = await self._runtime(settings)
        typing_enabled = typing_glob and self._config.typing_simulation
        message_context = await self._build_context(user_id, settings, custom_prompt)
        message_timestamp = now()
        message_context.append({
            "role": "user",
            "content": (
                f'<message role="system" timestamp="{iso(message_timestamp)}">\n'
                f"{PROACTIVE_MESSAGE_PROMPT}\n"
                "</message>"
            ),
        })
        typing_task: asyncio.Task | None = None
        sent: list[str] = []
        try:
            raw = await self._ai.chat(model=model, messages=message_context, json_mode=True)
            if generation != session.generation_id:
                return
            parsed = parse_response(raw)
            # This uses the existing mood algorithm unchanged.  The DECISION
            # response has no mood and never updates it.
            await self._save_mood(user_id, parsed.mood)
            if not parsed.should_reply or not parsed.messages:
                session.proactive_waiting_to_send = False
                self._schedule_proactive(session)
                return
            if generation != session.generation_id:
                return
            # One proactive message per cycle.  Any further message requires a
            # new DECISION/TIMING cycle, so a model response cannot create a
            # preplanned burst.
            parsed.messages = parsed.messages[:1]
            if typing_enabled:
                typing_task = asyncio.create_task(self._sender.typing_keepalive(chat_id))
            sent = await self._sender.send_messages(
                chat_id=chat_id, user_id=user_id, messages=parsed.messages,
                typing_enabled=typing_enabled, typing_task=typing_task,
            )
            if generation != session.generation_id:
                return
            last_ai_ts = None
            for message in sent:
                last_ai_ts = now()
                await self._history.add(user_id, "assistant", message, last_ai_ts)
            session.proactive_waiting_to_send = False
            if sent and last_ai_ts:
                session.proactive_count_since_user += 1
                session.last_proactive_at = time.monotonic()
                session.last_activity = session.last_proactive_at
                await self._settings_repo.update(
                    user_id,
                    last_activity_ts=last_ai_ts.timestamp(),
                    last_ai_message_ts=last_ai_ts.timestamp(),
                    proactive_stage=session.proactive_count_since_user,
                )
            self._schedule_proactive(session)
            logger.info("user_id=%s event=proactive_sent messages=%d", user_id, len(sent))
        except asyncio.CancelledError:
            # New user input invalidates this plan.  Do not reschedule here:
            # handle_message has already created a new decision timing.
            raise
        except AIClientError as e:
            session.proactive_waiting_to_send = False
            self._schedule_proactive(session)
            logger.warning("user_id=%s event=proactive_message_api_error error=%s", user_id, e)
        except Exception:
            session.proactive_waiting_to_send = False
            self._schedule_proactive(session)
            logger.exception("user_id=%s event=proactive_message_error", user_id)
        finally:
            if typing_task is not None:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass
