from aiogram import Router

from app.bot.handlers import chat, menu


def build_router() -> Router:
    router = Router(name="root")
    router.include_router(menu.router)
    router.include_router(chat.router)  # chat последним — ловит остальной текст
    return router
