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
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime

from app.ai.client import AIClient, AIClientError
from app.ai.prompts import (
    MORNING_PROMPT,
    OFFENDED_MOOD,
    PROACTIVE_STAGE_PROMPTS,
    build_system_prompt,
)
from app.ai.response_parser import parse_response
from app.config import MSK, Config
from app.conversation.memory import MemoryService
from app.conversation.sender import TelegramSender
from app.database.repository import HistoryRepository, UserSettingsRepository

logger = logging.getLogger(__name__)


@dataclass
class UserSession:
    buffer: list[str] = field(default_factory=list)
    generation_id: int = 0
    task: asyncio.Task | None = None
    last_activity: float = field(default_factory=time.monotonic)
    last_chat_id: int | None = None
    proactive_stage: int | None = None   # None = ещё не загружена из БД
    proactive_due_minutes: float = 0.0
    proactive_snooze_until: float = 0.0  # модель решила молчать — повторить позже
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
    ):
        self._config = config
        self._ai = ai_client
        self._sender = sender
        self._memory = memory
        self._settings_repo = settings_repo
        self._history = history_repo
        self._sessions: dict[int, UserSession] = {}
        self._proactive_task: asyncio.Task | None = None

    def _session(self, user_id: int) -> UserSession:
        if user_id not in self._sessions:
            self._sessions[user_id] = UserSession(
                proactive_due_minutes=self._due_for_stage(0)
            )
        return self._sessions[user_id]

    def _due_for_stage(self, stage: int) -> float:
        """Случайное окно молчания для стадии: 0 → первая попытка,
        1 → вторая попытка, 2 → ожидание перед обидой."""
        cfg = self._config
        if stage == 0:
            return random.uniform(cfg.proactive_stage1_min_minutes, cfg.proactive_stage1_max_minutes)
        if stage == 1:
            return random.uniform(cfg.proactive_stage2_min_minutes, cfg.proactive_stage2_max_minutes)
        return random.uniform(cfg.proactive_offense_min_minutes, cfg.proactive_offense_max_minutes)

    async def handle_message(self, user_id: int, chat_id: int, text: str) -> None:
        """Точка входа из Telegram handler. Handler остаётся тонким (п. 22 ТЗ)."""
        session = self._session(user_id)
        session.buffer.append(text)
        session.generation_id += 1
        session.last_activity = time.monotonic()
        session.last_chat_id = chat_id
        # пользователь вернулся — эскалация сбрасывается (настроение остаётся:
        # его сменит сама модель, когда «оттает»)
        session.proactive_stage = 0
        session.proactive_due_minutes = self._due_for_stage(0)
        session.proactive_snooze_until = 0.0
        await self._settings_repo.update(user_id, proactive_stage=0)
        generation = session.generation_id

        if session.task and not session.task.done():
            session.task.cancel()

        session.task = asyncio.create_task(
            self._pipeline(user_id, chat_id, generation)
        )

    async def cancel_active(self, user_id: int) -> None:
        """Отменяет активную генерацию (например, при очистке диалога)."""
        session = self._session(user_id)
        session.generation_id += 1
        session.buffer.clear()
        session.proactive_stage = 0  # сбрасываем кэш стадии при любой очистке
        if session.task and not session.task.done():
            session.task.cancel()
            try:
                await session.task
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

    async def _build_context(self, user_id: int, settings) -> list[dict]:
        system_prompt = build_system_prompt(
            settings.personality, settings.custom_prompt, settings.mood
        )
        context: list[dict] = [{"role": "system", "content": system_prompt}]

        # время суток и день недели — всегда по Москве
        now_msk = datetime.now(MSK)
        context.append({
            "role": "system",
            "content": (
                f"Сейчас {now_msk.strftime('%A, %H:%M')} по Москве. "
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
        taken: list[str] | None = None
        consumed = False  # сообщения сохранены в историю или отвечены
        typing_task: asyncio.Task | None = None

        try:
            settings = await self._settings_repo.get(user_id)
            typing_enabled = settings.typing_enabled and self._config.typing_simulation

            # --- debounce: ждём, пока пользователь допишет (п. 2 ТЗ) ---
            await asyncio.sleep(settings.debounce_seconds)
            if generation != session.generation_id or not session.buffer:
                return

            # забираем накопленные сообщения как единую реплику
            taken = session.buffer.copy()
            session.buffer.clear()
            user_text = "\n".join(taken)

            # --- typing начинается почти сразу, ещё до ответа модели ---
            if typing_enabled:
                typing_task = asyncio.create_task(self._sender.typing_keepalive(chat_id))

            # --- генерация ответа (максимально быстро, без sleep — п. 3 ТЗ) ---
            logger.info("user_id=%s event=generation_started parts=%d", user_id, len(taken))
            context = await self._build_context(user_id, settings)
            context.append({"role": "user", "content": user_text})

            raw = await self._ai.chat(
                model=settings.selected_model, messages=context, json_mode=True
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
                await self._history.add(user_id, "user", user_text)
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
                await self._history.add(user_id, "user", user_text)
                for s in sent:
                    await self._history.add(user_id, "assistant", s)
                raise

            consumed = True
            await self._history.add(user_id, "user", user_text)
            for s in sent:
                await self._history.add(user_id, "assistant", s)

            # уведомление о достижении лимита памяти (один раз при пересечении)
            await self._maybe_notify_memory_limit(user_id, chat_id, added=1 + len(sent))

            # долгосрочная память — фоном, не блокируя диалог
            asyncio.create_task(
                self._memory.maybe_extract_facts(
                    user_id, settings.selected_model, user_text, "\n".join(sent)
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

    def start_proactive_loop(self) -> None:
        if not self._config.proactive_enabled:
            logger.info("event=proactive_disabled")
            return
        self._proactive_task = asyncio.create_task(self._proactive_loop())
        logger.info(
            "event=proactive_started stage1=%.0f-%.0fm stage2=%.0f-%.0fm",
            self._config.proactive_stage1_min_minutes, self._config.proactive_stage1_max_minutes,
            self._config.proactive_stage2_min_minutes, self._config.proactive_stage2_max_minutes,
        )

    async def _proactive_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._config.proactive_check_interval)
                for user_id, session in list(self._sessions.items()):
                    try:
                        await self._maybe_proactive(user_id, session)
                    except Exception:
                        logger.exception("user_id=%s event=proactive_error", user_id)
        except asyncio.CancelledError:
            raise

    async def _maybe_good_morning(
        self, user_id: int, session: UserSession, idle_minutes: float
    ) -> bool:
        """«Доброе утро»: вчера попрощались, собеседник молчит с ночи.

        Возвращает True, если утренняя попытка была сделана (отправлена или
        модель решила молчать) — тогда стадийная логика в этот тик не работает.
        """
        cfg = self._config
        now_msk = datetime.now(MSK)
        if not (cfg.morning_start_hour <= now_msk.hour < cfg.morning_end_hour):
            return False
        if session.last_morning_date == now_msk.date():
            return False
        if idle_minutes < cfg.morning_min_idle_minutes:
            return False

        # одна попытка за утро (даже если модель решит молчать)
        session.last_morning_date = now_msk.date()
        session.generation_id += 1
        chat_id = session.last_chat_id

        settings = await self._settings_repo.get(user_id)
        typing_enabled = settings.typing_enabled and cfg.typing_simulation

        hours = idle_minutes / 60
        silence = f"{hours:.1f} часов" if hours >= 1 else f"{int(idle_minutes)} минут"

        logger.info("user_id=%s event=good_morning_started", user_id)
        context = await self._build_context(user_id, settings)
        context.append({
            "role": "user",
            "content": MORNING_PROMPT.format(
                time_msk=now_msk.strftime("%H:%M"), silence=silence
            ),
        })

        typing_task: asyncio.Task | None = None
        try:
            raw = await self._ai.chat(
                model=settings.selected_model, messages=context, json_mode=True
            )
            parsed = parse_response(raw)
            await self._save_mood(user_id, parsed.mood)

            if not parsed.should_reply:
                logger.info("user_id=%s event=good_morning_skipped", user_id)
                return True

            if typing_enabled:
                typing_task = asyncio.create_task(self._sender.typing_keepalive(chat_id))
            sent = await self._sender.send_messages(
                chat_id=chat_id,
                user_id=user_id,
                messages=parsed.messages,
                typing_enabled=typing_enabled,
                typing_task=typing_task,
            )
            for s in sent:
                await self._history.add(user_id, "assistant", s)
            session.last_activity = time.monotonic()  # отсчёт стадий — от её сообщения
            logger.info("user_id=%s event=good_morning_sent", user_id)
            return True
        except AIClientError as e:
            logger.warning("user_id=%s event=good_morning_api_error error=%s", user_id, e)
            return True
        finally:
            if typing_task is not None:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass

    async def _maybe_proactive(self, user_id: int, session: UserSession) -> None:
        if session.last_chat_id is None:
            return
        if session.proactive_stage is None:
            settings = await self._settings_repo.get(user_id)
            session.proactive_stage = settings.proactive_stage
        # стадия 3: обе попытки проигнорированы — она молчит до его возвращения
        if session.proactive_stage >= 3:
            return
        if time.monotonic() < session.proactive_snooze_until:
            return

        idle_minutes = (time.monotonic() - session.last_activity) / 60
        # не вмешиваемся в активный диалог
        if session.task and not session.task.done():
            return
        # пишем первой только тем, с кем уже был разговор
        short_memory = await self._memory.get_short_memory(user_id)
        if not short_memory:
            return

        # --- «доброе утро»: вчера попрощались, он молчит с ночи ---
        if await self._maybe_good_morning(user_id, session, idle_minutes):
            return

        if idle_minutes < session.proactive_due_minutes:
            return

        stage = session.proactive_stage

        # --- стадия 2: вторая попытка тоже проигнорирована → обида и молчание ---
        if stage == 2:
            await self._settings_repo.update(
                user_id, mood=OFFENDED_MOOD, proactive_stage=3
            )
            session.proactive_stage = 3
            logger.info("user_id=%s event=proactive_offended", user_id)
            return

        session.generation_id += 1  # инвалидируем всё ожидающее
        chat_id = session.last_chat_id

        settings = await self._settings_repo.get(user_id)
        typing_enabled = settings.typing_enabled and self._config.typing_simulation

        hours = idle_minutes / 60
        silence = f"{hours:.1f} часов" if hours >= 1 else f"{int(idle_minutes)} минут"

        logger.info("user_id=%s event=proactive_started stage=%d", user_id, stage + 1)
        context = await self._build_context(user_id, settings)
        context.append({
            "role": "user",
            "content": PROACTIVE_STAGE_PROMPTS[stage].format(silence=silence),
        })

        typing_task: asyncio.Task | None = None
        try:
            raw = await self._ai.chat(
                model=settings.selected_model, messages=context, json_mode=True
            )
            parsed = parse_response(raw)
            await self._save_mood(user_id, parsed.mood)

            if not parsed.should_reply:
                # модель решила молчать — попытка не засчитывается, повторим позже
                session.proactive_snooze_until = time.monotonic() + random.uniform(1800, 3600)
                logger.info("user_id=%s event=proactive_skipped stage=%d", user_id, stage + 1)
                return

            if typing_enabled:
                typing_task = asyncio.create_task(self._sender.typing_keepalive(chat_id))
            sent = await self._sender.send_messages(
                chat_id=chat_id,
                user_id=user_id,
                messages=parsed.messages,
                typing_enabled=typing_enabled,
                typing_task=typing_task,
            )
            for s in sent:
                await self._history.add(user_id, "assistant", s)

            # попытка засчитана: эскалация на следующую стадию
            new_stage = stage + 1
            session.proactive_stage = new_stage
            session.proactive_due_minutes = self._due_for_stage(new_stage)
            session.last_activity = time.monotonic()  # отсчёт следующей стадии — от её сообщения
            await self._settings_repo.update(user_id, proactive_stage=new_stage)
            logger.info("user_id=%s event=proactive_sent stage=%d", user_id, new_stage)
        except AIClientError as e:
            logger.warning("user_id=%s event=proactive_api_error error=%s", user_id, e)
        finally:
            if typing_task is not None:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass
