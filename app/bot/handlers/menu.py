"""Handlers меню /start и настроек (п. 14–18, 26–28 ТЗ)."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.ai.models import ModelRegistry
from app.ai.prompts import PERSONALITY_PRESETS
from app.bot.keyboards import menu as kb
from app.bot.states.settings import SettingsStates
from app.conversation.manager import ConversationManager
from app.database.repository import (
    HistoryRepository,
    MemoryRepository,
    UserSettingsRepository,
)

logger = logging.getLogger(__name__)

router = Router(name="menu")

MENU_TEXT = "🤖 твоя девушка\n\nвыбирай, что настроить — или просто пиши сообщение, я отвечу"


# --- /start, /help, /reset ------------------------------------------------- #

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(MENU_TEXT, reply_markup=kb.main_menu())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(
        "просто пиши сообщения — я отвечу как живая собеседница.\n\n"
        "/start — меню настроек\n"
        "/reset — очистить диалог\n"
        "/help — эта справка",
        reply_markup=kb.main_menu(),
    )


@router.message(Command("reset"))
async def cmd_reset(
    message: Message,
    manager: ConversationManager,
    history_repo: HistoryRepository,
) -> None:
    await manager.cancel_active(message.from_user.id)
    await history_repo.clear(message.from_user.id)
    logger.info("user_id=%s event=dialog_cleared", message.from_user.id)
    await message.answer("диалог очищен. начнём с чистого листа 🙂", reply_markup=kb.main_menu())


# --- главное меню ---------------------------------------------------------- #

@router.callback_query(F.data == "menu:back")
async def cb_back(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(MENU_TEXT, reply_markup=kb.main_menu())
    await callback.answer()


@router.callback_query(F.data == "menu:continue")
async def cb_continue(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "просто пиши — я здесь 🙂", reply_markup=kb.back_to_menu()
    )
    await callback.answer()


@router.callback_query(F.data == "menu:status")
async def cb_status(
    callback: CallbackQuery,
    settings_repo: UserSettingsRepository,
) -> None:
    settings = await settings_repo.get(callback.from_user.id)
    preset = PERSONALITY_PRESETS.get(settings.personality, PERSONALITY_PRESETS["default"])
    text = (
        "📋 текущие настройки\n\n"
        f"🤖 модель: {settings.selected_model}\n"
        f"🎭 характер: {preset['title']}\n"
        f"🧠 свой промт: {'задан' if settings.custom_prompt else 'не задан'}\n"
        f"⌨️ симуляция набора: {'вкл' if settings.typing_enabled else 'выкл'}\n"
        f"⏱ debounce: {settings.debounce_seconds:.1f} сек"
    )
    await callback.message.edit_text(text, reply_markup=kb.back_to_menu())
    await callback.answer()


# --- очистка диалога (п. 17 ТЗ) -------------------------------------------- #

@router.callback_query(F.data == "menu:clear")
async def cb_clear(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.confirm_clear_dialog)
    await callback.message.edit_text(
        "🧹 что очищаем?\n\n"
        "настройки, промт и выбранная модель сохранятся в любом случае",
        reply_markup=kb.clear_confirm(),
    )
    await callback.answer()


@router.callback_query(F.data.in_({"clear:dialog", "clear:all", "clear:everything"}))
async def cb_clear_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    manager: ConversationManager,
    history_repo: HistoryRepository,
    memory_repo: MemoryRepository,
    settings_repo: UserSettingsRepository,
) -> None:
    user_id = callback.from_user.id
    await state.clear()
    await manager.cancel_active(user_id)
    await history_repo.clear(user_id)
    if callback.data == "clear:dialog":
        logger.info("user_id=%s event=dialog_cleared", user_id)
        text = "диалог очищен, память осталась"
    elif callback.data == "clear:all":
        await memory_repo.clear(user_id)
        logger.info("user_id=%s event=dialog_and_memory_cleared", user_id)
        text = "диалог и долгосрочная память очищены"
    else:
        # полный сброс состояния: история, факты, настроение, стадия обиды
        await memory_repo.clear(user_id)
        await settings_repo.update(user_id, mood="", proactive_stage=0)
        logger.info("user_id=%s event=everything_cleared", user_id)
        text = "всё очищено: диалог, память и настроение. начинаем с нуля ✨"
    await callback.message.edit_text(text, reply_markup=kb.back_to_menu())
    await callback.answer()


# --- изменение промта (п. 15 ТЗ) ------------------------------------------- #

@router.callback_query(F.data == "menu:prompt")
async def cb_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    settings_repo: UserSettingsRepository,
) -> None:
    settings = await settings_repo.get(callback.from_user.id)
    current = settings.custom_prompt or "не задан"
    await state.set_state(SettingsStates.waiting_custom_prompt)
    await callback.message.edit_text(
        f"🧠 текущий дополнительный промт:\n«{current}»\n\n"
        "отправь новый текст одним сообщением.\n"
        "слово «сбросить» — убрать свой промт.\n\n"
        "технические инструкции и характер это не сломает — "
        "твой промт хранится отдельно",
        reply_markup=kb.cancel_prompt(),
    )
    await callback.answer()


@router.message(SettingsStates.waiting_custom_prompt)
async def msg_new_prompt(
    message: Message,
    state: FSMContext,
    settings_repo: UserSettingsRepository,
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("нужен текстовый промт, попробуй ещё раз")
        return
    new_prompt = "" if text.lower() == "сбросить" else text[:2000]
    await settings_repo.update(message.from_user.id, custom_prompt=new_prompt)
    await state.clear()
    logger.info("user_id=%s event=prompt_updated", message.from_user.id)
    answer = "свой промт сброшен" if not new_prompt else "промт сохранён"
    await message.answer(f"🧠 {answer}", reply_markup=kb.back_to_menu())


# --- характер (п. 10 ТЗ) ---------------------------------------------------- #

@router.callback_query(F.data == "menu:personality")
async def cb_personality(
    callback: CallbackQuery,
    settings_repo: UserSettingsRepository,
) -> None:
    settings = await settings_repo.get(callback.from_user.id)
    await callback.message.edit_text(
        "🎭 выбери характер:",
        reply_markup=kb.personality_menu(settings.personality),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("personality:"))
async def cb_personality_set(
    callback: CallbackQuery,
    settings_repo: UserSettingsRepository,
) -> None:
    key = callback.data.split(":", 1)[1]
    if key not in PERSONALITY_PRESETS:
        await callback.answer("неизвестный характер", show_alert=True)
        return
    await settings_repo.update(callback.from_user.id, personality=key)
    logger.info("user_id=%s event=personality_changed", callback.from_user.id)
    await callback.message.edit_text(
        "🎭 выбери характер:", reply_markup=kb.personality_menu(key)
    )
    await callback.answer("характер обновлён")


# --- выбор модели (п. 16 ТЗ) ------------------------------------------------ #

@router.callback_query(F.data == "menu:model")
async def cb_model(
    callback: CallbackQuery,
    settings_repo: UserSettingsRepository,
    model_registry: ModelRegistry,
) -> None:
    settings = await settings_repo.get(callback.from_user.id)
    models = await model_registry.get_models()
    await callback.message.edit_text(
        "🤖 выбери модель:",
        reply_markup=kb.models_menu(models, settings.selected_model, page=0),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("models_page:"))
async def cb_models_page(
    callback: CallbackQuery,
    settings_repo: UserSettingsRepository,
    model_registry: ModelRegistry,
) -> None:
    page = int(callback.data.split(":", 1)[1])
    settings = await settings_repo.get(callback.from_user.id)
    models = await model_registry.get_models()
    await callback.message.edit_text(
        "🤖 выбери модель:",
        reply_markup=kb.models_menu(models, settings.selected_model, page=page),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("model:"))
async def cb_model_set(
    callback: CallbackQuery,
    settings_repo: UserSettingsRepository,
    model_registry: ModelRegistry,
) -> None:
    model = callback.data.split(":", 1)[1]
    if not await model_registry.is_valid(model):
        await callback.answer("модель недоступна", show_alert=True)
        return
    await settings_repo.update(callback.from_user.id, selected_model=model)
    logger.info("user_id=%s event=model_changed", callback.from_user.id)
    await callback.message.edit_text(
        f"🤖 модель изменена на {model}", reply_markup=kb.back_to_menu()
    )
    await callback.answer()


# --- параметры typing/debounce (п. 28 ТЗ) ----------------------------------- #

@router.callback_query(F.data == "menu:params")
async def cb_params(
    callback: CallbackQuery,
    settings_repo: UserSettingsRepository,
) -> None:
    settings = await settings_repo.get(callback.from_user.id)
    await callback.message.edit_text(
        "⚙️ параметры поведения:",
        reply_markup=kb.params_menu(settings.typing_enabled, settings.debounce_seconds),
    )
    await callback.answer()


@router.callback_query(F.data == "params:typing")
async def cb_params_typing(
    callback: CallbackQuery,
    settings_repo: UserSettingsRepository,
) -> None:
    settings = await settings_repo.get(callback.from_user.id)
    new_value = not settings.typing_enabled
    await settings_repo.update(callback.from_user.id, typing_enabled=new_value)
    logger.info("user_id=%s event=typing_toggled enabled=%s", callback.from_user.id, new_value)
    await callback.message.edit_text(
        "⚙️ параметры поведения:",
        reply_markup=kb.params_menu(new_value, settings.debounce_seconds),
    )
    await callback.answer()


@router.callback_query(F.data == "params:debounce")
async def cb_params_debounce(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(SettingsStates.waiting_debounce)
    await callback.message.edit_text(
        "⏱ отправь новое значение debounce в секундах (от 0.5 до 10):",
        reply_markup=kb.cancel_prompt(),
    )
    await callback.answer()


@router.message(SettingsStates.waiting_debounce)
async def msg_new_debounce(
    message: Message,
    state: FSMContext,
    settings_repo: UserSettingsRepository,
) -> None:
    try:
        value = float((message.text or "").replace(",", ".").strip())
        if not 0.5 <= value <= 10:
            raise ValueError
    except ValueError:
        await message.answer("нужно число от 0.5 до 10, попробуй ещё раз")
        return
    await settings_repo.update(message.from_user.id, debounce_seconds=value)
    await state.clear()
    logger.info("user_id=%s event=debounce_changed", message.from_user.id)
    await message.answer(f"⏱ debounce: {value:.1f} сек", reply_markup=kb.back_to_menu())
