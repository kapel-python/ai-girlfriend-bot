"""Отправка ответов в Telegram с typing-индикатором (п. 6, 25 ТЗ).

- typing поддерживается до момента отправки (Telegram сбрасывает его
  примерно через 5 секунд, поэтому обновляем каждые 4);
- отмена задачи мгновенно прекращает typing;
- слишком длинные тексты режутся по логическим границам.

Каждый chunk считается подтверждённым только после успешного ответа
Telegram. ``progress_callback`` вызывается сразу после подтверждения, поэтому
менеджер может сохранить частичную отправку даже если следующий await был
отменён или Telegram завершил вызов ошибкой.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.conversation.typing_simulator import (
    calculate_pause_between_messages,
    calculate_typing_duration,
)

logger = logging.getLogger(__name__)


class NonPrivateChatError(RuntimeError):
    """The bot is configured to operate only in private Telegram chats."""


TELEGRAM_MAX_LENGTH = 4096
_TYPING_REFRESH_INTERVAL = 4.0


@dataclass(frozen=True, slots=True)
class SendProgress:
    """Одно подтверждённое Telegram сообщение и состояние на момент ответа."""

    user_id: int
    chat_id: int
    message_index: int
    chunk_index: int
    chunk: str
    message_id: int | None
    confirmed_chunks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SendResult:
    """Результат отправки; безопасные для отмены подтверждённые chunks."""

    confirmed_chunks: tuple[str, ...]
    message_ids: tuple[int | None, ...] = ()

    @property
    def message_id(self) -> int | None:
        """ID последнего подтверждённого сообщения (``None`` для fake sender)."""
        return self.message_ids[-1] if self.message_ids else None

    # ``SendResult`` намеренно остаётся list-like для старых fake sender/ callers.
    def __iter__(self) -> Iterator[str]:
        return iter(self.confirmed_chunks)

    def __len__(self) -> int:
        return len(self.confirmed_chunks)

    def __getitem__(self, index):
        return self.confirmed_chunks[index]

    def __eq__(self, other) -> bool:
        if isinstance(other, SendResult):
            return (
                self.confirmed_chunks == other.confirmed_chunks
                and self.message_ids == other.message_ids
            )
        if isinstance(other, (list, tuple)):
            return self.confirmed_chunks == tuple(other)
        return NotImplemented


ProgressCallback = Callable[[SendProgress], Awaitable[None] | None]


def split_long_text(text: str, limit: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Делит длинный текст по абзацам/предложениям, не разрывая слова без нужды."""
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    remaining = text
    while len(remaining) > limit:
        chunk = remaining[:limit]
        # ищем логическую границу: конец абзаца → конец предложения → пробел
        cut = max(chunk.rfind("\n\n"), chunk.rfind("\n"))
        if cut < limit // 2:
            cut = max(chunk.rfind(". "), chunk.rfind("! "), chunk.rfind("? "))
            cut = cut + 1 if cut != -1 else -1
        if cut < limit // 2:
            cut = chunk.rfind(" ")
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return parts


class TelegramSender:
    def __init__(self, bot: Bot, *, private_only: bool = True):
        self._bot = bot
        self._private_only = private_only
        self._chat_types: dict[int, str | None] = {}

    async def _ensure_private(self, chat_id: int) -> None:
        if not self._private_only:
            return
        if chat_id in self._chat_types:
            chat_type = self._chat_types[chat_id]
        else:
            get_chat = getattr(self._bot, "get_chat", None)
            if not callable(get_chat):
                # Lightweight fake bots used by tests/embedders may not expose
                # get_chat; they are trusted to represent a private sender.
                return
            chat = await get_chat(chat_id)
            chat_type = getattr(chat, "type", None)
            self._chat_types[chat_id] = chat_type
        if chat_type is not None and chat_type != "private":
            raise NonPrivateChatError("non-private Telegram chat rejected")

    async def typing_keepalive(self, chat_id: int) -> None:
        """Поддерживает индикатор «печатает…» до отмены задачи."""
        try:
            await self._ensure_private(chat_id)
            while True:
                await self._bot.send_chat_action(chat_id, "typing")
                await asyncio.sleep(_TYPING_REFRESH_INTERVAL)
        except asyncio.CancelledError:
            raise
        except NonPrivateChatError:
            logger.info("chat_id=%s event=non_private_chat_rejected", chat_id)
            return
        except TelegramAPIError as e:
            logger.warning("chat_id=%s event=typing_error error=%s", chat_id, type(e).__name__)

    async def send_messages(
        self,
        chat_id: int,
        user_id: int,
        messages: list[str],
        typing_enabled: bool,
        typing_task: asyncio.Task | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> SendResult:
        """Отправляет сообщения и возвращает только подтверждённые chunks.

        ``progress_callback`` совместим как с sync-, так и с async-функцией и
        вызывается после каждого успешного ``send_message``. Исключения и
        отмена не маскируются, но callback уже сохранил все предыдущие chunks.
        """
        sent: list[str] = []
        message_ids: list[int | None] = []

        await self._ensure_private(chat_id)
        for index, message in enumerate(messages):
            chunks = split_long_text(message)

            for chunk_index, chunk in enumerate(chunks):
                if typing_enabled:
                    duration = calculate_typing_duration(chunk)
                    logger.info(
                        "user_id=%s event=typing_started duration=%.1f", user_id, duration
                    )
                    own_task = None
                    if typing_task is None:
                        own_task = asyncio.create_task(self.typing_keepalive(chat_id))
                    try:
                        await asyncio.sleep(duration)
                    finally:
                        if own_task is not None:
                            own_task.cancel()
                            try:
                                await own_task
                            except asyncio.CancelledError:
                                pass

                response = await self._bot.send_message(chat_id, chunk)
                if isinstance(response, int):
                    message_id = response
                elif isinstance(response, dict):
                    message_id = response.get("message_id")
                else:
                    message_id = getattr(response, "message_id", None)
                sent.append(chunk)
                message_ids.append(message_id)
                if progress_callback is not None:
                    progress = SendProgress(
                        user_id=user_id,
                        chat_id=chat_id,
                        message_index=index,
                        chunk_index=chunk_index,
                        chunk=chunk,
                        message_id=message_id,
                        confirmed_chunks=tuple(sent),
                    )
                    callback_result = progress_callback(progress)
                    if inspect.isawaitable(callback_result):
                        await callback_result
                logger.info("user_id=%s event=message_sent", user_id)

            # естественная пауза между сообщениями (но не после последнего)
            if index < len(messages) - 1:
                await asyncio.sleep(calculate_pause_between_messages())

        return SendResult(tuple(sent), tuple(message_ids))
