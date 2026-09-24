"""Точка входа: сборка зависимостей и запуск polling."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
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
    GlobalSettingsRepository,
    HistoryRepository,
    MemoryRepository,
    PersonalityRepository,
    UserSettingsRepository,
)
from app.logging_config import setup_logging

logger = logging.getLogger(__name__)


async def main() -> None:
    setup_logging()
    config = load_config()

    db = None
    ai_client = None
    bot = None
    manager = None
    try:
        db = await init_db(config.database_path)

        settings_repo = UserSettingsRepository(db, config.default_model, config.message_debounce)
        history_repo = HistoryRepository(db)
        memory_repo = MemoryRepository(db)
        personality_repo = PersonalityRepository(db)
        global_repo = GlobalSettingsRepository(db, config.default_model, config.message_debounce)

        ai_client = AIClient(config)
        model_registry = ModelRegistry(ai_client, config.default_model)

        # Telegram text is plain by default. Dynamic values in the menu are
        # escaped explicitly where they are inserted into messages, so no global
        # HTML interpretation is required (or desirable) for the bot.
        bot = Bot(
            token=config.bot_token,
            default=DefaultBotProperties(parse_mode=None),
        )
        sender = TelegramSender(bot, private_only=True)
        memory = MemoryService(ai_client, history_repo, memory_repo, config.short_memory_limit)
        manager = ConversationManager(
            config=config,
            ai_client=ai_client,
            sender=sender,
            memory=memory,
            settings_repo=settings_repo,
            history_repo=history_repo,
            global_repo=global_repo,
            personality_repo=personality_repo,
        )

        dp = Dispatcher(storage=MemoryStorage())
        dp.update.middleware(ErrorLoggingMiddleware())

        # зависимости для handlers
        dp["manager"] = manager
        dp["settings_repo"] = settings_repo
        dp["history_repo"] = history_repo
        dp["memory_repo"] = memory_repo
        dp["model_registry"] = model_registry
        dp["ai_client"] = ai_client
        dp["global_repo"] = global_repo
        dp["personality_repo"] = personality_repo
        dp["config"] = config

        dp.include_router(build_router())

        logger.info("event=bot_starting model=%s", config.default_model)
        await manager.restore_sessions()
        manager.start_proactive_loop()
        # Do not discard Telegram updates that arrived while the process was
        # down. Durable pending history is now the recovery mechanism.
        await bot.delete_webhook(drop_pending_updates=False)
        await dp.start_polling(bot)
    finally:
        logger.info("event=bot_stopping")
        if manager is not None:
            try:
                await manager.shutdown()
            except Exception:
                logger.exception("event=manager_shutdown_failed")
        if ai_client is not None:
            try:
                await ai_client.close()
            except Exception:
                logger.exception("event=ai_client_close_failed")
        if bot is not None:
            try:
                await bot.session.close()
            except Exception:
                logger.exception("event=bot_close_failed")
        if db is not None:
            try:
                await db.close()
            except Exception:
                logger.exception("event=db_close_failed")


if __name__ == "__main__":
    asyncio.run(main())
