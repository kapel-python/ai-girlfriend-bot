"""Middleware: ошибка одного пользователя не роняет бота (п. 23 ТЗ)."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

logger = logging.getLogger(__name__)


class ErrorLoggingMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception:
            user = data.get("event_from_user")
            logger.exception(
                "user_id=%s event=handler_error", getattr(user, "id", "unknown")
            )
            return None
