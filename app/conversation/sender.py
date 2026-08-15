"""Отправка ответов в Telegram с typing-индикатором (п. 6, 25 ТЗ).

- typing поддерживается до момента отправки (Telegram сбрасывает его
  примерно через 5 секунд, поэтому обновляем каждые 4);
- отмена задачи мгновенно прекращает typing;
- слишком длинные тексты режутся по логическим границам.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

from app.conversation.typing_simulator import (
    calculate_pause_between_messages,
    calculate_typing_duration,
)

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LENGTH = 4096
_TYPING_REFRESH_INTERVAL = 4.0


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
    def __init__(self, bot: Bot):
        self._bot = bot

    async def typing_keepalive(self, chat_id: int) -> None:
        """Поддерживает индикатор «печатает…» до отмены задачи."""
        try:
            while True:
                await self._bot.send_chat_action(chat_id, "typing")
                await asyncio.sleep(_TYPING_REFRESH_INTERVAL)
        except asyncio.CancelledError:
            raise
        except TelegramAPIError as e:
            logger.warning("chat_id=%s event=typing_error error=%s", chat_id, type(e).__name__)

    async def send_messages(
        self,
        chat_id: int,
        user_id: int,
        messages: list[str],
        typing_enabled: bool,
        typing_task: asyncio.Task | None = None,
    ) -> list[str]:
        """Отправляет сообщения с естественным временем набора.

        Если передан внешний typing_task (запущенный менеджером ещё до
        генерации — улучшение 6), свой индикатор не создаётся.
        Возвращает список фактически отправленных текстов
        (при отмене — только те, что успели уйти).
        """
        sent: list[str] = []

        for index, message in enumerate(messages):
            chunks = split_long_text(message)

            for chunk in chunks:
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

                await self._bot.send_message(chat_id, chunk)
                sent.append(chunk)
                logger.info("user_id=%s event=message_sent", user_id)

            # естественная пауза между сообщениями (но не после последнего)
            if index < len(messages) - 1:
                await asyncio.sleep(calculate_pause_between_messages())

        return sent
