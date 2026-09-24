"""Handler обычных сообщений — максимально тонкий (п. 22 ТЗ).

Вся логика debounce, генерации, typing и отправки живёт в ConversationManager.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from app.conversation.manager import ConversationManager

router = Router(name="chat")

_PRIVATE_ONLY_TEXT = "работаю только в личных сообщениях — напиши мне сюда, в личный чат"


def _is_private(message: Message) -> bool:
    return getattr(getattr(message, "chat", None), "type", None) == "private"


async def _reject_non_private(message: Message) -> None:
    """Refuse a group/channel update without touching the conversation FSM."""
    try:
        await message.answer(_PRIVATE_ONLY_TEXT)
    except TelegramAPIError:
        # In a channel the bot may not have permission to answer.  The update
        # is still rejected; there is no reason to fail the whole update.
        pass


@router.message(F.text)
async def on_text(message: Message, manager: ConversationManager) -> None:
    if not _is_private(message):
        await _reject_non_private(message)
        return
    # ``from_user`` is present for private text messages.  Keeping the guard
    # above also prevents a group message from entering the per-user session.
    await manager.handle_message(
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        text=message.text,
    )


@router.message(~F.text)
async def on_non_text(message: Message) -> None:
    if not _is_private(message):
        await _reject_non_private(message)
        return
    # стикеры/фото/голосовые: первая версия работает только с текстом
    await message.answer("я пока понимаю только текстовые сообщения 🙈")
