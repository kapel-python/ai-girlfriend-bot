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
from app.config import Config
from app.conversation.manager import ConversationManager
from app.database.repository import (
    GlobalSettingsRepository,
    HistoryRepository,
    MemoryRepository,
    UserSettingsRepository,
)

logger = logging.getLogger(__name__)

router = Router(name="menu")

MENU_TEXT = "🤖 твоя девушка\n\nвыбирай, что настроить — или просто пиши сообщение, я отвечу"


def _is_admin(config: Config, user_id: int) -> bool:
    return user_id in config.admin_ids


async def _admin_only(callback: CallbackQuery, config: Config) -> bool:
    """True — доступ разрешён. Иначе отвечает отказом и возвращает False."""
    if _is_admin(config, callback.from_user.id):
        return True
    await callback.answer("эта настройка доступна только администратору", show_alert=True)
    return False


# --- /start, /help, /reset ------------------------------------------------- #

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, config: Config) -> None:
    await state.clear()
    await message.answer(
        MENU_TEXT, reply_markup=kb.main_menu(_is_admin(config, message.from_user.id))
    )


@router.message(Command("help"))
async def cmd_help(message: Message, config: Config) -> None:
    await message.answer(
        "просто пиши сообщения — я отвечу как живая собеседница.\n\n"
        "/start — меню настроек\n"
        "/reset — очистить диалог\n"
        "/help — эта справка",
        reply_markup=kb.main_menu(_is_admin(config, message.from_user.id)),
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
    await message.answer("диалог очищен. начнём с чистого листа 🙂", reply_markup=kb.back_to_menu())


# --- главное меню ---------------------------------------------------------- #

@router.callback_query(F.data == "menu:back")
async def cb_back(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    await state.clear()
    await callback.message.edit_text(
        MENU_TEXT, reply_markup=kb.main_menu(_is_admin(config, callback.from_user.id))
    )
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
    history_repo: HistoryRepository,
    memory_repo: MemoryRepository,
    global_repo: GlobalSettingsRepository,
    ai_client: "AIClient",
    config: Config,
) -> None:
    from datetime import datetime

    from app.config import MSK

    user_id = callback.from_user.id
    settings = await settings_repo.get(user_id)
    if settings.personality == "custom":
        preset_title = "✍️ свой характер"
    else:
        preset_title = PERSONALITY_PRESETS.get(
            settings.personality, PERSONALITY_PRESETS["realistic"]
        )["title"]
    messages_count = await history_repo.count(user_id)
    facts_count = await memory_repo.count(user_id)
    now_msk = datetime.now(MSK)
    model = await global_repo.get_str("selected_model")

    text = (
        "📋 текущие настройки\n\n"
        f"🤖 модель: {model}\n"
        f"🎭 твой характер для неё: {preset_title}\n\n"
        "📊 статистика\n\n"
        f"💬 сообщений в диалоге: {messages_count}/{config.short_memory_limit}\n"
        f"🧠 фактов о тебе в памяти: {facts_count}\n"
        f"💭 её настроение: {settings.mood or 'нейтральное'}\n"
        f"🕐 время у неё: {now_msk.strftime('%H:%M')} (МСК)"
    )

    # админу — глобальные параметры и баланс
    if _is_admin(config, user_id):
        custom_prompt = await global_repo.get_str("custom_prompt")
        typing_glob = await global_repo.get_bool("typing_enabled")
        debounce = await global_repo.get_float("debounce_seconds")
        balance = await ai_client.get_balance()
        text += (
            "\n\n🔧 глобальные (для всех)\n\n"
            f"🧠 свой промт: {'задан' if custom_prompt else 'не задан'}\n"
            f"⌨️ симуляция набора: {'вкл' if typing_glob else 'выкл'}\n"
            f"⏱ debounce: {debounce:.1f} сек\n"
            f"💰 баланс API: {balance} ₽"
        )

    await callback.message.edit_text(text, reply_markup=kb.status_menu())
    await callback.answer()


@router.callback_query(F.data == "status:facts")
async def cb_status_facts(
    callback: CallbackQuery,
    memory_repo: MemoryRepository,
) -> None:
    facts = await memory_repo.get_facts(callback.from_user.id)
    if facts:
        text = "🧠 что она о тебе помнит:\n\n" + "\n".join(f"• {f}" for f in facts)
    else:
        text = "🧠 она пока ничего о тебе не записала — пообщайтесь подольше"
    await callback.message.edit_text(text[:4000], reply_markup=kb.back_to_status())
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
    global_repo: GlobalSettingsRepository,
    config: Config,
) -> None:
    if not await _admin_only(callback, config):
        return
    current = await global_repo.get_str("custom_prompt") or "не задан"
    await state.set_state(SettingsStates.waiting_custom_prompt)
    await callback.message.edit_text(
        f"🧠 текущий дополнительный промт (глобальный, для всех):\n«{current}»\n\n"
        "отправь новый текст одним сообщением.\n"
        "слово «сбросить» — убрать промт.\n\n"
        "технические инструкции и характер это не сломает — "
        "промт хранится отдельно",
        reply_markup=kb.cancel_prompt(),
    )
    await callback.answer()


@router.message(SettingsStates.waiting_custom_prompt)
async def msg_new_prompt(
    message: Message,
    state: FSMContext,
    global_repo: GlobalSettingsRepository,
    config: Config,
) -> None:
    if not _is_admin(config, message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("нужен текстовый промт, попробуй ещё раз")
        return
    new_prompt = "" if text.lower() == "сбросить" else text[:2000]
    await global_repo.set("custom_prompt", new_prompt)
    await state.clear()
    logger.info("user_id=%s event=global_prompt_updated", message.from_user.id)
    answer = "глобальный промт сброшен" if not new_prompt else "глобальный промт сохранён"
    await message.answer(f"🧠 {answer}", reply_markup=kb.back_to_menu())


# --- характер (п. 10 ТЗ) ---------------------------------------------------- #

@router.callback_query(F.data == "menu:personality")
async def cb_personality(
    callback: CallbackQuery,
    settings_repo: UserSettingsRepository,
) -> None:
    settings = await settings_repo.get(callback.from_user.id)
    await callback.message.edit_text(
        "🎭 выбери характер (только для тебя):",
        reply_markup=kb.personality_menu(
            settings.personality, bool(settings.custom_personality.strip())
        ),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("personality:"))
async def cb_personality_set(
    callback: CallbackQuery,
    state: FSMContext,
    settings_repo: UserSettingsRepository,
) -> None:
    key = callback.data.split(":", 1)[1]

    # свой характер — FSM: ждём текстовое описание
    if key == "custom":
        settings = await settings_repo.get(callback.from_user.id)
        current = settings.custom_personality.strip() or "не задан"
        await state.set_state(SettingsStates.waiting_custom_personality)
        await callback.message.edit_text(
            f"✍️ твой характер для неё сейчас:\n«{current[:500]}»\n\n"
            "опиши одним сообщением, какой она должна быть — "
            "это заменит пресет и будет действовать только у тебя.\n"
            "слово «сбросить» — вернуться к пресету «реалистичный»",
            reply_markup=kb.cancel_prompt(),
        )
        await callback.answer()
        return

    if key not in PERSONALITY_PRESETS:
        await callback.answer("неизвестный характер", show_alert=True)
        return
    await settings_repo.update(callback.from_user.id, personality=key)
    logger.info("user_id=%s event=personality_changed", callback.from_user.id)
    await callback.message.edit_text(
        "🎭 выбери характер (только для тебя):",
        reply_markup=kb.personality_menu(key),
    )
    await callback.answer("характер обновлён")


@router.message(SettingsStates.waiting_custom_personality)
async def msg_custom_personality(
    message: Message,
    state: FSMContext,
    settings_repo: UserSettingsRepository,
) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("нужно текстовое описание, попробуй ещё раз")
        return
    await state.clear()
    if text.lower() == "сбросить":
        await settings_repo.update(
            message.from_user.id, personality="realistic", custom_personality=""
        )
        logger.info("user_id=%s event=custom_personality_cleared", message.from_user.id)
        await message.answer("✍️ свой характер сброшен, снова пресет «реалистичный»",
                             reply_markup=kb.back_to_menu())
        return
    await settings_repo.update(
        message.from_user.id, personality="custom", custom_personality=text[:3000]
    )
    logger.info("user_id=%s event=custom_personality_set", message.from_user.id)
    await message.answer("✍️ характер сохранён — теперь она такая только у тебя",
                         reply_markup=kb.back_to_menu())


# --- выбор модели (п. 16 ТЗ) ------------------------------------------------ #

@router.callback_query(F.data == "menu:model")
async def cb_model(
    callback: CallbackQuery,
    global_repo: GlobalSettingsRepository,
    model_registry: ModelRegistry,
    config: Config,
) -> None:
    if not await _admin_only(callback, config):
        return
    current = await global_repo.get_str("selected_model")
    models = await model_registry.get_models()
    await callback.message.edit_text(
        "🤖 выбери модель (глобально, для всех):",
        reply_markup=kb.models_menu(models, current, page=0),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("models_page:"))
async def cb_models_page(
    callback: CallbackQuery,
    global_repo: GlobalSettingsRepository,
    model_registry: ModelRegistry,
    config: Config,
) -> None:
    if not await _admin_only(callback, config):
        return
    page = int(callback.data.split(":", 1)[1])
    current = await global_repo.get_str("selected_model")
    models = await model_registry.get_models()
    await callback.message.edit_text(
        "🤖 выбери модель (глобально, для всех):",
        reply_markup=kb.models_menu(models, current, page=page),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("model:"))
async def cb_model_set(
    callback: CallbackQuery,
    global_repo: GlobalSettingsRepository,
    model_registry: ModelRegistry,
    config: Config,
) -> None:
    if not await _admin_only(callback, config):
        return
    model = callback.data.split(":", 1)[1]
    if not await model_registry.is_valid(model):
        await callback.answer("модель недоступна", show_alert=True)
        return
    await global_repo.set("selected_model", model)
    logger.info("user_id=%s event=global_model_changed", callback.from_user.id)
    await callback.message.edit_text(
        f"🤖 модель для всех изменена на {model}", reply_markup=kb.back_to_menu()
    )
    await callback.answer()


# --- параметры typing/debounce (п. 28 ТЗ) ----------------------------------- #

@router.callback_query(F.data == "menu:params")
async def cb_params(
    callback: CallbackQuery,
    global_repo: GlobalSettingsRepository,
    config: Config,
) -> None:
    if not await _admin_only(callback, config):
        return
    typing_glob = await global_repo.get_bool("typing_enabled")
    debounce = await global_repo.get_float("debounce_seconds")
    await callback.message.edit_text(
        "⚙️ параметры поведения (глобально, для всех):",
        reply_markup=kb.params_menu(typing_glob, debounce),
    )
    await callback.answer()


@router.callback_query(F.data == "params:typing")
async def cb_params_typing(
    callback: CallbackQuery,
    global_repo: GlobalSettingsRepository,
    config: Config,
) -> None:
    if not await _admin_only(callback, config):
        return
    new_value = not await global_repo.get_bool("typing_enabled")
    await global_repo.set("typing_enabled", new_value)
    debounce = await global_repo.get_float("debounce_seconds")
    logger.info("user_id=%s event=global_typing_toggled enabled=%s", callback.from_user.id, new_value)
    await callback.message.edit_text(
        "⚙️ параметры поведения (глобально, для всех):",
        reply_markup=kb.params_menu(new_value, debounce),
    )
    await callback.answer()


@router.callback_query(F.data == "params:debounce")
async def cb_params_debounce(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    if not await _admin_only(callback, config):
        return
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
    global_repo: GlobalSettingsRepository,
    config: Config,
) -> None:
    if not _is_admin(config, message.from_user.id):
        await state.clear()
        return
    try:
        value = float((message.text or "").replace(",", ".").strip())
        if not 0.5 <= value <= 10:
            raise ValueError
    except ValueError:
        await message.answer("нужно число от 0.5 до 10, попробуй ещё раз")
        return
    await global_repo.set("debounce_seconds", value)
    await state.clear()
    logger.info("user_id=%s event=global_debounce_changed", message.from_user.id)
    await message.answer(f"⏱ debounce для всех: {value:.1f} сек", reply_markup=kb.back_to_menu())
