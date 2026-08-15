"""Точка входа: сборка зависимостей и запуск polling."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from app.ai.client import AIClient
from app.ai.models import ModelRegistry
from app.bot.handlers import build_router
from app.bot.middlewares.error_logging import ErrorLoggingMiddleware
from app.config import load_config
from app.conversation.manager import ConversationManager
from app.conversation.memory import MemoryService
from app.conversation.sender import TelegramSender
from app.database.database import init_db
from app.database.repository import (
    HistoryRepository,
    MemoryRepository,
    UserSettingsRepository,
)
from app.logging_config import setup_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    config = load_config()

    db = await init_db(config.database_path)

    settings_repo = UserSettingsRepository(db, config.default_model, config.message_debounce)
    history_repo = HistoryRepository(db)
    memory_repo = MemoryRepository(db)

    ai_client = AIClient(config)
    model_registry = ModelRegistry(ai_client, config.default_model)

    bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    sender = TelegramSender(bot)
    memory = MemoryService(ai_client, history_repo, memory_repo, config.short_memory_limit)
    manager = ConversationManager(
        config=config,
        ai_client=ai_client,
        sender=sender,
        memory=memory,
        settings_repo=settings_repo,
        history_repo=history_repo,
    )

    dp = Dispatcher(storage=MemoryStorage())
    dp.update.middleware(ErrorLoggingMiddleware())

    # зависимости для handlers
    dp["manager"] = manager
    dp["settings_repo"] = settings_repo
    dp["history_repo"] = history_repo
    dp["memory_repo"] = memory_repo
    dp["model_registry"] = model_registry

    dp.include_router(build_router())

    logger.info("event=bot_starting model=%s", config.default_model)
    manager.start_proactive_loop()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        logger.info("event=bot_stopping")
        await manager.shutdown()
        await ai_client.close()
        await bot.session.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
