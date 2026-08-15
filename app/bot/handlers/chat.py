"""Handler обычных сообщений — максимально тонкий (п. 22 ТЗ).

Вся логика debounce, генерации, typing и отправки живёт в ConversationManager.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message

from app.conversation.manager import ConversationManager

router = Router(name="chat")


@router.message(F.text)
async def on_text(message: Message, manager: ConversationManager) -> None:
    await manager.handle_message(
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        text=message.text,
    )


@router.message(~F.text)
async def on_non_text(message: Message) -> None:
    # стикеры/фото/голосовые: первая версия работает только с текстом
    await message.answer("я пока понимаю только текстовые сообщения 🙈")
