"""Handlers меню /start и настроек (п. 14–18, 26–28 ТЗ)."""

from __future__ import annotations

import logging
import re
import secrets
import time
from uuid import uuid4

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramNotFound
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.ai.models import ModelRegistry
from app.bot.keyboards import menu as kb
from app.bot.states.settings import SettingsStates
from app.config import Config
from app.conversation.manager import ConversationManager
from app.database.repository import (
    GlobalSettingsRepository,
    HistoryRepository,
    MemoryRepository,
    PersonalityRepository,
    UserSettingsRepository,
)

logger = logging.getLogger(__name__)

router = Router(name="menu")

MENU_TEXT = "🤖 твоя девушка\n\nвыбирай, что настроить — или просто пиши сообщение, я отвечу"

_PRIVATE_ONLY_TEXT = (
    "работаю только в личных сообщениях — напиши мне сюда, в личный чат"
)
_PENDING_TTL_SECONDS = 15 * 60
_PENDING_EXPIRES_KEY = "_pending_expires_at"
_NONCE_RE = re.compile(r"^[0-9a-f]{8}$", re.IGNORECASE)
_CLEAR_KINDS = frozenset({"dialog", "all", "mood", "everything"})


def _safe_text(value: object, *, limit: int | None = None) -> str:
    """Render a dynamic menu value safely for a plain-text bot."""
    text = "" if value is None else str(value)
    if limit is not None:
        text = text[:limit]
    # Bot defaults deliberately have no HTML parse mode. Keep the value plain
    # so an ampersand or angle bracket is displayed literally; only remove NUL
    # which cannot be transported safely.
    return text.replace("\x00", "")


def _is_private_message(message: Message | None) -> bool:
    return getattr(getattr(message, "chat", None), "type", None) == "private"


def _is_private_callback(callback: CallbackQuery) -> bool:
    # Inline callbacks without an accessible source message have no safe
    # private-chat context, so they are rejected rather than allowed to touch
    # an FSM operation.
    return _is_private_message(getattr(callback, "message", None))


async def _reject_non_private_message(message: Message) -> None:
    try:
        await message.answer(_PRIVATE_ONLY_TEXT)
    except TelegramAPIError:
        # A bot may lack permission to speak in a channel.  The update is
        # rejected regardless; do not let the API error re-enter the FSM.
        logger.debug("event=private_only_message_not_sent chat_type=%s", _chat_type(message))


def _chat_type(message: Message | None) -> str:
    return str(getattr(getattr(message, "chat", None), "type", ""))


async def _reject_non_private_callback(callback: CallbackQuery) -> None:
    await _answer(callback, _PRIVATE_ONLY_TEXT, show_alert=True)


def _is_ignorable_callback_error(exc: BaseException) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if isinstance(exc, TelegramNotFound):
        return True
    if name in {"messagenotmodified", "messagenotfound"}:
        return True
    return any(
        marker in text
        for marker in (
            "message is not modified",
            "message to edit not found",
            "message to delete not found",
            "query is too old",
            "invalid query id",
        )
    )


async def _answer(
    callback: CallbackQuery,
    text: str | None = None,
    *,
    show_alert: bool = False,
) -> None:
    """Answer a callback without turning harmless stale-button errors into 500s."""
    try:
        if text is None:
            await callback.answer()
        else:
            await callback.answer(text, show_alert=show_alert)
    except Exception as exc:
        if _is_ignorable_callback_error(exc):
            logger.debug("event=callback_answer_ignored error=%s", exc)
            return
        if isinstance(exc, TelegramAPIError):
            # A callback can outlive its message or the bot can lose the right
            # to answer it.  There is no useful recovery in the handler.
            logger.debug("event=callback_answer_failed error=%s", exc)
            return
        raise


async def _safe_edit(
    callback: CallbackQuery,
    text: str,
    reply_markup=None,
) -> bool:
    """Edit a callback message, tolerating already-deleted/unchanged ones."""
    message = getattr(callback, "message", None)
    if message is None:
        return False
    try:
        await message.edit_text(text, reply_markup=reply_markup)
        return True
    except Exception as exc:
        if _is_ignorable_callback_error(exc):
            logger.debug("event=callback_edit_ignored error=%s", exc)
            return False
        raise


def _new_nonce() -> str:
    # Eight hexadecimal characters are short enough for every destructive
    # callback and still provide a useful one-time operation token.
    return secrets.token_hex(4)


def _valid_nonce(value: object) -> bool:
    return isinstance(value, str) and _NONCE_RE.fullmatch(value) is not None


def _same_nonce(left: object, right: object) -> bool:
    if not _valid_nonce(left) or not _valid_nonce(right):
        return False
    return secrets.compare_digest(str(left).lower(), str(right).lower())


async def _set_pending_state(state: FSMContext, target, **data) -> None:
    """Start a fresh, expiring input/operation state."""
    await state.clear()
    await state.set_state(target)
    await state.update_data(
        _PENDING_EXPIRES_KEY=time.monotonic() + _PENDING_TTL_SECONDS,
        **data,
    )


async def _fresh_pending_data(state: FSMContext, expected) -> dict | None:
    """Return state data while valid, clearing it after the TTL if needed."""
    if await state.get_state() != expected:
        return None
    data = await state.get_data()
    expires_at = data.get(_PENDING_EXPIRES_KEY)
    if expires_at is not None:
        try:
            expired = float(expires_at) <= time.monotonic()
        except (TypeError, ValueError):
            expired = True
        if expired:
            await state.clear()
            return None
    return data


async def _expired_input_reply(message: Message) -> None:
    await message.answer(
        "время ввода истекло — начни настройку заново",
        reply_markup=kb.back_to_menu(),
    )


def _resolve_key(raw: object, keys: list[str]) -> str | None:
    return kb.resolve_callback_value(raw, keys)


async def _resolve_personality_key(
    callback: CallbackQuery, personality_repo: PersonalityRepository
) -> str | None:
    raw = (callback.data or "").rsplit(":", 1)[-1]
    if not raw.startswith("~"):
        return raw
    personalities = await personality_repo.list()
    return _resolve_key(raw, [preset.key for preset in personalities])


async def _lookup_preset(
    personality_repo: PersonalityRepository, key: str | None
):
    if key is None:
        return None
    return await personality_repo.get(key)


def _is_admin(config: Config, user_id: int) -> bool:
    return user_id in config.admin_ids


async def _admin_only(callback: CallbackQuery, config: Config) -> bool:
    """True — доступ разрешён. Иначе отвечает отказом и возвращает False."""
    if _is_admin(config, callback.from_user.id):
        return True
    await _answer(callback, "эта настройка доступна только администратору", show_alert=True)
    return False


# --- /start, /help, /reset ------------------------------------------------- #

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, config: Config) -> None:
    if not _is_private_message(message):
        await _reject_non_private_message(message)
        return
    await state.clear()
    await message.answer(
        MENU_TEXT,
        reply_markup=kb.main_menu(_is_admin(config, message.from_user.id)),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext, config: Config) -> None:
    if not _is_private_message(message):
        await _reject_non_private_message(message)
        return
    # Help is an explicit escape hatch from every pending input/operation.
    await state.clear()
    await message.answer(
        "просто пиши сообщения — я отвечу как живая собеседница.\n\n"
        "/start — меню настроек\n"
        "/reset — очистить диалог\n"
        "/help — эта справка",
        reply_markup=kb.main_menu(_is_admin(config, message.from_user.id)),
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message, state: FSMContext) -> None:
    if not _is_private_message(message):
        await _reject_non_private_message(message)
        return
    # /reset is a real command transition: discard any older pending input and
    # create a fresh, expiring confirmation operation.
    nonce = _new_nonce()
    await _set_pending_state(
        state,
        SettingsStates.confirm_clear_dialog,
        operation="clear",
        clear_kind="dialog",
        operation_nonce=nonce,
    )
    await message.answer(
        "🧹 удалить только диалог?\n\n"
        "будут удалены все сообщения диалога. память и настроение останутся.",
        reply_markup=kb.clear_delete_confirm("dialog", nonce),
    )


# --- главное меню ---------------------------------------------------------- #

@router.callback_query(F.data == "menu:back")
async def cb_back(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    await state.clear()
    await _safe_edit(
        callback,
        MENU_TEXT,
        reply_markup=kb.main_menu(_is_admin(config, callback.from_user.id)),
    )
    await _answer(callback)


@router.callback_query(F.data == "menu:status")
async def cb_status(
    callback: CallbackQuery,
    state: FSMContext,
    settings_repo: UserSettingsRepository,
    history_repo: HistoryRepository,
    memory_repo: MemoryRepository,
    global_repo: GlobalSettingsRepository,
    personality_repo: PersonalityRepository,
    ai_client: "AIClient",
    config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    from datetime import datetime

    from app.config import MSK

    # Opening a status page cancels a pending setting/operation.
    await state.clear()
    user_id = callback.from_user.id
    settings = await settings_repo.get(user_id)
    if settings.personality == "custom":
        preset_title = "✍️ свой характер"
    else:
        preset = await personality_repo.get(settings.personality)
        preset_title = _safe_text(preset.title) if preset else "🎧 реалистичный"
    messages_count = await history_repo.count(user_id)
    facts_count = await memory_repo.count(user_id)
    now_msk = datetime.now(MSK)
    model = _safe_text(await global_repo.get_str("selected_model"))

    text = (
        "📋 текущие настройки\n\n"
        f"🤖 модель: {model}\n"
        f"🎭 твой характер для неё: {preset_title}\n\n"
        "📊 статистика\n\n"
        f"💬 сообщений в диалоге: {messages_count}/{config.short_memory_limit}\n"
        f"🧠 фактов о тебе в памяти: {facts_count}\n"
        f"💭 её настроение: {_safe_text(settings.mood or 'нейтральное')}\n"
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
            f"💰 баланс API: {_safe_text(balance)} ₽"
        )

    await _safe_edit(callback, text, reply_markup=kb.status_menu())
    await _answer(callback)


@router.callback_query(F.data == "status:facts")
async def cb_status_facts(
    callback: CallbackQuery,
    state: FSMContext,
    memory_repo: MemoryRepository,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    await state.clear()
    facts = await memory_repo.get_facts(callback.from_user.id)
    if facts:
        text = "🧠 что она о тебе помнит:\n\n" + "\n".join(
            f"• {_safe_text(fact)}" for fact in facts
        )
    else:
        text = "🧠 она пока ничего о тебе не записала — пообщайтесь подольше"
    await _safe_edit(callback, text[:4000], reply_markup=kb.back_to_status())
    await _answer(callback)


# --- очистка диалога (п. 17 ТЗ) -------------------------------------------- #

@router.callback_query(F.data == "menu:clear")
async def cb_clear(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    # The options screen is a menu transition, not an already-authorized
    # destructive operation.  Any previous confirmation is invalidated.
    await state.clear()
    await _safe_edit(
        callback,
        "🧹 что очищаем?\n\n"
        "настройки, промт и выбранная модель сохранятся в любом случае",
        reply_markup=kb.clear_options(),
    )
    await _answer(callback)


_CLEAR_INFO = {
    "dialog": "будут удалены все сообщения диалога. память и настроение останутся.",
    "all": "будут удалены все сообщения диалога и долгосрочная память. настроение останется.",
    "mood": "будет удалено только текущее настроение. диалог и память останутся.",
    "everything": "будут удалены все сообщения диалога, долгосрочная память и текущее настроение.",
}


async def _begin_clear_confirmation(
    callback: CallbackQuery, state: FSMContext, kind: str
) -> None:
    if kind not in _CLEAR_KINDS:
        await _answer(callback, "неизвестный вариант очистки", show_alert=True)
        return
    nonce = _new_nonce()
    await _set_pending_state(
        state,
        SettingsStates.confirm_clear_dialog,
        operation="clear",
        clear_kind=kind,
        operation_nonce=nonce,
    )
    await _safe_edit(
        callback,
        f"⚠️ подтвердить удаление?\n\n{_CLEAR_INFO[kind]}",
        reply_markup=kb.clear_delete_confirm(kind, nonce),
    )
    await _answer(callback)


@router.callback_query(F.data.regexp(r"^clear:request:(dialog|all|mood|everything)$"))
async def cb_clear_request(callback: CallbackQuery, state: FSMContext) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    kind = (callback.data or "").rsplit(":", 1)[-1]
    await _begin_clear_confirmation(callback, state, kind)


@router.callback_query(
    F.data.regexp(r"^clear:confirm:(dialog|all|mood|everything):[0-9a-fA-F]{8}$")
)
async def cb_clear_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    manager: ConversationManager,
    history_repo: HistoryRepository,
    memory_repo: MemoryRepository,
    settings_repo: UserSettingsRepository,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    data = await _fresh_pending_data(state, SettingsStates.confirm_clear_dialog)
    parts = (callback.data or "").split(":")
    kind = parts[2] if len(parts) == 4 else ""
    nonce = parts[3] if len(parts) == 4 else ""
    if (
        data is None
        or data.get("operation") != "clear"
        or data.get("clear_kind") != kind
        or not _same_nonce(data.get("operation_nonce"), nonce)
    ):
        # Do not clear a newer operation here: an old button must not cancel a
        # different pending confirmation.
        await _answer(callback, "кнопка устарела — начни очистку заново", show_alert=True)
        return

    user_id = callback.from_user.id
    # Consume the nonce/state before any repository mutation.  A replay after a
    # successful deletion therefore cannot execute the operation twice.
    await state.clear()
    if kind == "mood":
        # Newer managers expose a narrow invalidation hook so an in-flight
        # reply cannot immediately restore the mood we just cleared.  Keep a
        # compatibility fallback for older/custom managers.
        invalidate_mood = getattr(manager, "invalidate_mood_only", None)
        if invalidate_mood is None:
            invalidate_mood = getattr(manager, "cancel_for_mood_reset", None)
        if invalidate_mood is not None:
            await invalidate_mood(user_id)
        await settings_repo.update(user_id, mood="")
        logger.info("user_id=%s event=mood_cleared", user_id)
        text = "настроение удалено, диалог и память остались"
    else:
        await manager.cancel_active(user_id)
        await history_repo.clear(user_id)
    if kind == "dialog":
        logger.info("user_id=%s event=dialog_cleared", user_id)
        text = "диалог очищен, память осталась"
    elif kind == "all":
        await memory_repo.clear(user_id)
        logger.info("user_id=%s event=dialog_and_memory_cleared", user_id)
        text = "диалог и долгосрочная память очищены"
    elif kind == "everything":
        # полный сброс состояния: история, факты, настроение, служебный счётчик
        await memory_repo.clear(user_id)
        await settings_repo.update(user_id, mood="", proactive_stage=0)
        logger.info("user_id=%s event=everything_cleared", user_id)
        text = "всё очищено: диалог, память и настроение. начинаем с нуля ✨"
    await _safe_edit(callback, text, reply_markup=kb.back_to_menu())
    await _answer(callback)


# --- изменение промта (п. 15 ТЗ) ------------------------------------------- #

@router.callback_query(F.data == "menu:prompt")
async def cb_prompt(
    callback: CallbackQuery,
    state: FSMContext,
    global_repo: GlobalSettingsRepository,
    config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    current = await global_repo.get_str("custom_prompt") or "не задан"
    await _set_pending_state(state, SettingsStates.waiting_custom_prompt)
    await _safe_edit(
        callback,
        f"🧠 текущий дополнительный промт (глобальный, для всех):\n"
        f"«{_safe_text(current, limit=2000)}»\n\n"
        "отправь новый текст одним сообщением.\n"
        "слово «сбросить» — убрать промт.\n\n"
        "технические инструкции и характер это не сломает — "
        "промт хранится отдельно",
        reply_markup=kb.cancel_prompt(),
    )
    await _answer(callback)


@router.message(SettingsStates.waiting_custom_prompt)
async def msg_new_prompt(
    message: Message,
    state: FSMContext,
    global_repo: GlobalSettingsRepository,
    config: Config,
) -> None:
    if not _is_private_message(message):
        await _reject_non_private_message(message)
        return
    if (await _fresh_pending_data(state, SettingsStates.waiting_custom_prompt)) is None:
        await _expired_input_reply(message)
        return
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
    state: FSMContext,
    settings_repo: UserSettingsRepository,
    personality_repo: PersonalityRepository,
    config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    await state.clear()
    settings = await settings_repo.get(callback.from_user.id)
    personalities = await personality_repo.list()
    await _safe_edit(
        callback,
        "🎭 выбери характер (только для тебя):",
        reply_markup=kb.personality_menu(
            personalities, settings.personality,
            bool(settings.custom_personality.strip()), _is_admin(config, callback.from_user.id),
        ),
    )
    await _answer(callback)


# --- управление глобальными характерами (только разработчик) ------------- #

async def _show_personality_admin_list(
    callback: CallbackQuery, personality_repo: PersonalityRepository,
) -> None:
    personalities = await personality_repo.list()
    await _safe_edit(
        callback,
        "⚙️ настройки характера\n\nвыбери характер для управления:",
        reply_markup=kb.personality_admin_menu(personalities),
    )


@router.callback_query(F.data == "personality_admin:manage")
async def cb_personality_admin_manage(
    callback: CallbackQuery, state: FSMContext,
    personality_repo: PersonalityRepository, config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await state.clear()
    await _show_personality_admin_list(callback, personality_repo)
    await _answer(callback)


@router.callback_query(F.data == "personality_admin:add")
async def cb_personality_admin_add(
    callback: CallbackQuery, state: FSMContext, config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await _set_pending_state(state, SettingsStates.waiting_personality_create_title)
    await _safe_edit(
        callback,
        "➕ введи название нового характера одним сообщением.\n\n"
        "например: «умная» или «спокойная и заботливая».",
        reply_markup=kb.cancel_prompt(),
    )
    await _answer(callback)


@router.callback_query(F.data.regexp(r"^personality_admin:item:[^:]+$"))
async def cb_personality_admin_item(
    callback: CallbackQuery, state: FSMContext,
    personality_repo: PersonalityRepository, config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await state.clear()
    key = await _resolve_personality_key(callback, personality_repo)
    preset = await _lookup_preset(personality_repo, key)
    if preset is None:
        await _answer(callback, "характер уже удалён", show_alert=True)
        await _show_personality_admin_list(callback, personality_repo)
        return
    await _safe_edit(
        callback,
        f"🎭 {_safe_text(preset.title)}\n\nвыбери действие:",
        reply_markup=kb.personality_admin_item(preset),
    )
    await _answer(callback)


@router.callback_query(F.data.regexp(r"^personality_admin:view:[^:]+$"))
async def cb_personality_admin_view(
    callback: CallbackQuery, state: FSMContext,
    personality_repo: PersonalityRepository, config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await state.clear()
    key = await _resolve_personality_key(callback, personality_repo)
    preset = await _lookup_preset(personality_repo, key)
    if preset is None:
        await _answer(callback, "характер уже удалён", show_alert=True)
        await _show_personality_admin_list(callback, personality_repo)
        return
    await _safe_edit(
        callback,
        f"👁 {_safe_text(preset.title)}\n\n{_safe_text(preset.prompt[:3800])}",
        reply_markup=kb.personality_admin_item(preset),
    )
    await _answer(callback)


@router.callback_query(F.data.regexp(r"^personality_admin:stats:[^:]+$"))
async def cb_personality_admin_stats(
    callback: CallbackQuery, state: FSMContext,
    personality_repo: PersonalityRepository, config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await state.clear()
    key = await _resolve_personality_key(callback, personality_repo)
    preset = await _lookup_preset(personality_repo, key)
    if preset is None:
        await _answer(callback, "характер уже удалён", show_alert=True)
        await _show_personality_admin_list(callback, personality_repo)
        return
    count = await personality_repo.usage_count(key)
    await _safe_edit(
        callback,
        f"📊 {_safe_text(preset.title)}\n\nактивно выбрали: {count} пользователей",
        reply_markup=kb.personality_admin_item(preset),
    )
    await _answer(callback)


@router.callback_query(F.data.regexp(r"^personality_admin:edit:[^:]+$"))
async def cb_personality_admin_edit(
    callback: CallbackQuery, state: FSMContext,
    personality_repo: PersonalityRepository, config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await state.clear()
    key = await _resolve_personality_key(callback, personality_repo)
    preset = await _lookup_preset(personality_repo, key)
    if preset is None:
        await _answer(callback, "характер уже удалён", show_alert=True)
        await _show_personality_admin_list(callback, personality_repo)
        return
    await _safe_edit(
        callback,
        f"✏️ что изменить в характере «{_safe_text(preset.title)}»?",
        reply_markup=kb.personality_edit_menu(key),
    )
    await _answer(callback)


@router.callback_query(F.data.regexp(r"^personality_admin:delete:[^:]+$"))
async def cb_personality_admin_delete(
    callback: CallbackQuery, state: FSMContext,
    personality_repo: PersonalityRepository, config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await state.clear()
    key = await _resolve_personality_key(callback, personality_repo)
    preset = await _lookup_preset(personality_repo, key)
    if preset is None:
        await _answer(callback, "характер уже удалён", show_alert=True)
        await _show_personality_admin_list(callback, personality_repo)
        return
    nonce = _new_nonce()
    await _set_pending_state(
        state,
        SettingsStates.confirm_personality_delete,
        operation="personality_delete",
        personality_key=key,
        operation_nonce=nonce,
    )
    await _safe_edit(
        callback,
        f"🗑 удалить характер «{_safe_text(preset.title)}»?\n\n"
        "пользователи, которые его выбрали, будут переведены на другой характер.",
        reply_markup=kb.personality_delete_confirm(key, nonce),
    )
    await _answer(callback)


@router.callback_query(
    F.data.regexp(r"^personality_admin:delete_confirm:[0-9a-fA-F]{8}$")
)
async def cb_personality_admin_delete_confirm(
    callback: CallbackQuery, state: FSMContext,
    personality_repo: PersonalityRepository, config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    data = await _fresh_pending_data(state, SettingsStates.confirm_personality_delete)
    nonce = (callback.data or "").rsplit(":", 1)[-1]
    key = data.get("personality_key") if data else None
    if (
        data is None
        or data.get("operation") != "personality_delete"
        or not key
        or not _same_nonce(data.get("operation_nonce"), nonce)
    ):
        await _answer(callback, "кнопка устарела — начни удаление заново", show_alert=True)
        return
    if await personality_repo.get(key) is None:
        await state.clear()
        await _show_personality_admin_list(callback, personality_repo)
        await _answer(callback, "характер уже удалён")
        return
    # Consume the one-time state before mutating the repository.
    await state.clear()
    deleted = await personality_repo.delete(key)
    if not deleted:
        await _answer(callback, "характер уже удалён", show_alert=True)
        return
    await _show_personality_admin_list(callback, personality_repo)
    await _answer(callback, "характер удалён")


@router.callback_query(F.data.regexp(r"^personality_admin:edit_title:[^:]+$"))
async def cb_personality_admin_edit_title(
    callback: CallbackQuery, state: FSMContext,
    personality_repo: PersonalityRepository, config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await state.clear()
    key = await _resolve_personality_key(callback, personality_repo)
    preset = await _lookup_preset(personality_repo, key)
    if preset is None:
        await _answer(callback, "характер уже удалён", show_alert=True)
        await _show_personality_admin_list(callback, personality_repo)
        return
    await _set_pending_state(
        state,
        SettingsStates.waiting_personality_edit_title,
        personality_key=key,
    )
    await _safe_edit(
        callback,
        f"✏️ текущее имя: {_safe_text(preset.title)}\n\n"
        "отправь новое имя одним сообщением.",
        reply_markup=kb.cancel_prompt(),
    )
    await _answer(callback)


@router.callback_query(F.data.regexp(r"^personality_admin:edit_prompt:[^:]+$"))
async def cb_personality_admin_edit_prompt(
    callback: CallbackQuery, state: FSMContext,
    personality_repo: PersonalityRepository, config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await state.clear()
    key = await _resolve_personality_key(callback, personality_repo)
    preset = await _lookup_preset(personality_repo, key)
    if preset is None:
        await _answer(callback, "характер уже удалён", show_alert=True)
        await _show_personality_admin_list(callback, personality_repo)
        return
    await _set_pending_state(
        state,
        SettingsStates.waiting_personality_edit_prompt,
        personality_key=key,
    )
    await _safe_edit(
        callback,
        f"📝 текущее описание «{_safe_text(preset.title)}»:\n\n"
        f"{_safe_text(preset.prompt[:1500])}\n\n"
        "отправь новое описание одним сообщением.",
        reply_markup=kb.cancel_prompt(),
    )
    await _answer(callback)


def _personality_title(value: str) -> str:
    """Нормализуем отображаемое имя, не меняя его смысл и оформление."""
    return " ".join(value.split())[:50]


@router.message(SettingsStates.waiting_personality_create_title)
async def msg_personality_create_title(
    message: Message, state: FSMContext, personality_repo: PersonalityRepository,
    config: Config,
) -> None:
    if not _is_private_message(message):
        await _reject_non_private_message(message)
        return
    if (await _fresh_pending_data(state, SettingsStates.waiting_personality_create_title)) is None:
        await _expired_input_reply(message)
        return
    if not _is_admin(config, message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("нужно название, попробуй ещё раз")
        return
    title = _personality_title(text)
    if not title:
        await message.answer("нужно название, попробуй ещё раз")
        return
    await _set_pending_state(
        state,
        SettingsStates.waiting_personality_create_prompt,
        personality_title=title,
    )
    await message.answer(
        "теперь отправь описание этого характера одним сообщением.",
        reply_markup=kb.cancel_prompt(),
    )


@router.message(SettingsStates.waiting_personality_create_prompt)
async def msg_personality_create_prompt(
    message: Message, state: FSMContext, personality_repo: PersonalityRepository,
    config: Config,
) -> None:
    if not _is_private_message(message):
        await _reject_non_private_message(message)
        return
    if (await _fresh_pending_data(state, SettingsStates.waiting_personality_create_prompt)) is None:
        await _expired_input_reply(message)
        return
    if not _is_admin(config, message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("нужно описание, попробуй ещё раз")
        return
    data = await state.get_data()
    title = data.get("personality_title")
    if not title:
        await state.clear()
        await message.answer("не удалось продолжить создание, начни заново",
                             reply_markup=kb.back_to_menu())
        return
    key = f"managed_{uuid4().hex[:12]}"
    try:
        await personality_repo.create(key, title, text[:3000])
    except Exception:
        logger.exception("user_id=%s event=personality_create_failed", message.from_user.id)
        await message.answer("не удалось сохранить характер, попробуй ещё раз")
        return
    await state.clear()
    logger.info("user_id=%s event=personality_created key=%s", message.from_user.id, key)
    await message.answer(
        "✅ характер добавлен",
        reply_markup=kb.personality_admin_menu(await personality_repo.list()),
    )


async def _finish_personality_edit(
    message: Message, state: FSMContext, personality_repo: PersonalityRepository,
    key: str, *, title: str | None = None, prompt: str | None = None,
) -> bool:
    if await personality_repo.get(key) is None:
        await state.clear()
        await message.answer("характер уже удалён", reply_markup=kb.back_to_menu())
        return False
    try:
        updated = await personality_repo.update(key, title=title, prompt=prompt)
    except Exception:
        logger.exception("user_id=%s event=personality_update_failed", message.from_user.id)
        await message.answer("не удалось сохранить изменения, попробуй ещё раз")
        return False
    if updated is None:
        await state.clear()
        await message.answer("характер уже удалён", reply_markup=kb.back_to_menu())
        return False
    await state.clear()
    logger.info("user_id=%s event=personality_updated key=%s", message.from_user.id, key)
    await message.answer(
        "✅ характер изменён",
        reply_markup=kb.personality_admin_item(updated),
    )
    return True


@router.message(SettingsStates.waiting_personality_edit_title)
async def msg_personality_edit_title(
    message: Message, state: FSMContext, personality_repo: PersonalityRepository,
    config: Config,
) -> None:
    if not _is_private_message(message):
        await _reject_non_private_message(message)
        return
    data = await _fresh_pending_data(state, SettingsStates.waiting_personality_edit_title)
    if data is None:
        await _expired_input_reply(message)
        return
    if not _is_admin(config, message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("нужно имя, попробуй ещё раз")
        return
    key = data.get("personality_key")
    if not key:
        await state.clear()
        await message.answer("не удалось определить характер", reply_markup=kb.back_to_menu())
        return
    await _finish_personality_edit(
        message, state, personality_repo, key, title=_personality_title(text)
    )


@router.message(SettingsStates.waiting_personality_edit_prompt)
async def msg_personality_edit_prompt(
    message: Message, state: FSMContext, personality_repo: PersonalityRepository,
    config: Config,
) -> None:
    if not _is_private_message(message):
        await _reject_non_private_message(message)
        return
    data = await _fresh_pending_data(state, SettingsStates.waiting_personality_edit_prompt)
    if data is None:
        await _expired_input_reply(message)
        return
    if not _is_admin(config, message.from_user.id):
        await state.clear()
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("нужно описание, попробуй ещё раз")
        return
    key = data.get("personality_key")
    if not key:
        await state.clear()
        await message.answer("не удалось определить характер", reply_markup=kb.back_to_menu())
        return
    await _finish_personality_edit(message, state, personality_repo, key, prompt=text[:3000])


@router.callback_query(
    F.data.regexp(r"^personality:[^:]+$")
)
async def cb_personality_set(
    callback: CallbackQuery,
    state: FSMContext,
    settings_repo: UserSettingsRepository,
    personality_repo: PersonalityRepository,
    config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    raw_key = (callback.data or "").split(":", 1)[1]
    # Any personality navigation cancels a pending input.  The two warning
    # screens below intentionally do not change the stored personality.
    await state.clear()

    # Для этого пресета сначала явно показываем предупреждение и ничего не меняем.
    if raw_key == "manipulator":
        await _safe_edit(
            callback,
            "⚠️ Учти: этот характер может создавать сильную эмоциональную "
            "привязанность к этой личности и использовать эмоционально давящий "
            "стиль общения. Все возможные риски, в том числе моральные, ты "
            "берёшь на себя.",
            reply_markup=kb.manipulator_warning(),
        )
        await _answer(callback)
        return

    if raw_key == "18plus":
        await _safe_edit(
            callback,
            "🔞 Этот характер предназначен только для совершеннолетних. "
            "Он добавляет смелый романтический флирт, чувственные намёки и "
            "двусмысленные поддразнивания. Подтверди, что тебе уже исполнилось 18 лет.",
            reply_markup=kb.adult_warning(),
        )
        await _answer(callback)
        return

    # свой характер — FSM: ждём текстовое описание
    if raw_key == "custom":
        settings = await settings_repo.get(callback.from_user.id)
        current = settings.custom_personality.strip() or "не задан"
        await _set_pending_state(state, SettingsStates.waiting_custom_personality)
        await _safe_edit(
            callback,
            f"✍️ твой характер для неё сейчас:\n«{_safe_text(current, limit=500)}»\n\n"
            "опиши одним сообщением, какой она должна быть — "
            "это заменит пресет и будет действовать только у тебя.\n"
            "слово «сбросить» — вернуться к пресету «реалистичный»",
            reply_markup=kb.cancel_prompt(),
        )
        await _answer(callback)
        return

    personalities = await personality_repo.list()
    key = _resolve_key(raw_key, [preset.key for preset in personalities])
    preset = next((item for item in personalities if item.key == key), None)
    if key is None or preset is None:
        await _answer(callback, "неизвестный характер", show_alert=True)
        return
    await settings_repo.update(callback.from_user.id, personality=key)
    logger.info("user_id=%s event=personality_changed", callback.from_user.id)
    await _safe_edit(
        callback,
        "🎭 выбери характер (только для тебя):",
        reply_markup=kb.personality_menu(
            personalities, key,
            bool((await settings_repo.get(callback.from_user.id)).custom_personality.strip()),
            _is_admin(config, callback.from_user.id),
        ),
    )
    await _answer(callback, "характер обновлён")


@router.callback_query(F.data == "personality:manipulator:confirm")
async def cb_manipulator_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    settings_repo: UserSettingsRepository,
    personality_repo: PersonalityRepository,
    config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    await state.clear()
    if await personality_repo.get("manipulator") is None:
        await _answer(callback, "характер уже удалён", show_alert=True)
        return
    await settings_repo.update(callback.from_user.id, personality="manipulator")
    logger.info("user_id=%s event=personality_changed", callback.from_user.id)
    settings = await settings_repo.get(callback.from_user.id)
    await _safe_edit(
        callback,
        "🎭 выбери характер (только для тебя):",
        reply_markup=kb.personality_menu(await personality_repo.list(), settings.personality,
                                         bool(settings.custom_personality.strip()),
                                         _is_admin(config, callback.from_user.id)),
    )
    await _answer(callback, "характер обновлён")


@router.callback_query(F.data == "personality:manipulator:cancel")
async def cb_manipulator_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    settings_repo: UserSettingsRepository,
    personality_repo: PersonalityRepository,
    config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    await state.clear()
    settings = await settings_repo.get(callback.from_user.id)
    await _safe_edit(
        callback,
        "🎭 выбери характер (только для тебя):",
        reply_markup=kb.personality_menu(await personality_repo.list(), settings.personality,
                                         bool(settings.custom_personality.strip()),
                                         _is_admin(config, callback.from_user.id)),
    )
    await _answer(callback)


@router.callback_query(F.data == "personality:18plus:confirm")
async def cb_adult_confirm(
    callback: CallbackQuery,
    state: FSMContext,
    settings_repo: UserSettingsRepository,
    personality_repo: PersonalityRepository,
    config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    await state.clear()
    if await personality_repo.get("18plus") is None:
        await _answer(callback, "характер уже удалён", show_alert=True)
        return
    await settings_repo.update(callback.from_user.id, personality="18plus")
    logger.info("user_id=%s event=personality_changed", callback.from_user.id)
    settings = await settings_repo.get(callback.from_user.id)
    await _safe_edit(
        callback,
        "🎭 выбери характер (только для тебя):",
        reply_markup=kb.personality_menu(await personality_repo.list(), settings.personality,
                                         bool(settings.custom_personality.strip()),
                                         _is_admin(config, callback.from_user.id)),
    )
    await _answer(callback, "характер обновлён")


@router.callback_query(F.data == "personality:18plus:cancel")
async def cb_adult_cancel(
    callback: CallbackQuery,
    state: FSMContext,
    settings_repo: UserSettingsRepository,
    personality_repo: PersonalityRepository,
    config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    await state.clear()
    settings = await settings_repo.get(callback.from_user.id)
    await _safe_edit(
        callback,
        "🎭 выбери характер (только для тебя):",
        reply_markup=kb.personality_menu(await personality_repo.list(), settings.personality,
                                         bool(settings.custom_personality.strip()),
                                         _is_admin(config, callback.from_user.id)),
    )
    await _answer(callback)


@router.message(SettingsStates.waiting_custom_personality)
async def msg_custom_personality(
    message: Message,
    state: FSMContext,
    settings_repo: UserSettingsRepository,
    personality_repo: PersonalityRepository,
) -> None:
    if not _is_private_message(message):
        await _reject_non_private_message(message)
        return
    if (await _fresh_pending_data(state, SettingsStates.waiting_custom_personality)) is None:
        await _expired_input_reply(message)
        return
    text = (message.text or "").strip()
    if not text:
        await message.answer("нужно текстовое описание, попробуй ещё раз")
        return
    await state.clear()
    if text.lower() == "сбросить":
        default_key = await personality_repo.default_key() or "realistic"
        default_preset = await personality_repo.get(default_key)
        await settings_repo.update(
            message.from_user.id, personality=default_key, custom_personality=""
        )
        logger.info("user_id=%s event=custom_personality_cleared", message.from_user.id)
        default_title = _safe_text(default_preset.title) if default_preset else "🎧 реалистичный"
        await message.answer(f"✍️ свой характер сброшен, снова пресет «{default_title}»",
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
    state: FSMContext,
    global_repo: GlobalSettingsRepository,
    model_registry: ModelRegistry,
    config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await state.clear()
    current = await global_repo.get_str("selected_model")
    models = await model_registry.get_models()
    await _safe_edit(
        callback,
        "🤖 выбери модель (глобально, для всех):",
        reply_markup=kb.models_menu(models, current, page=0),
    )
    await _answer(callback)


@router.callback_query(F.data.regexp(r"^models_page:\d+$"))
async def cb_models_page(
    callback: CallbackQuery,
    state: FSMContext,
    global_repo: GlobalSettingsRepository,
    model_registry: ModelRegistry,
    config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await state.clear()
    try:
        page = int((callback.data or "").split(":", 1)[1])
    except (IndexError, TypeError, ValueError):
        await _answer(callback, "неизвестная страница моделей", show_alert=True)
        return
    current = await global_repo.get_str("selected_model")
    models = await model_registry.get_models()
    await _safe_edit(
        callback,
        "🤖 выбери модель (глобально, для всех):",
        reply_markup=kb.models_menu(models, current, page=page),
    )
    await _answer(callback)


@router.callback_query(F.data.regexp(r"^model:(.+)$"))
async def cb_model_set(
    callback: CallbackQuery,
    state: FSMContext,
    global_repo: GlobalSettingsRepository,
    model_registry: ModelRegistry,
    config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await state.clear()
    raw_model = (callback.data or "").split(":", 1)[1]
    if raw_model.startswith("~"):
        models = await model_registry.get_models()
        model = _resolve_key(raw_model, models)
    else:
        model = raw_model
    if not model or not await model_registry.is_valid(model):
        await _answer(callback, "модель недоступна", show_alert=True)
        return
    await global_repo.set("selected_model", model)
    logger.info("user_id=%s event=global_model_changed", callback.from_user.id)
    await _safe_edit(
        callback,
        f"🤖 модель для всех изменена на {_safe_text(model)}",
        reply_markup=kb.back_to_menu(),
    )
    await _answer(callback)


# --- параметры typing/debounce (п. 28 ТЗ) ----------------------------------- #

@router.callback_query(F.data == "menu:params")
async def cb_params(
    callback: CallbackQuery,
    state: FSMContext,
    global_repo: GlobalSettingsRepository,
    config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await state.clear()
    typing_glob = await global_repo.get_bool("typing_enabled")
    debounce = await global_repo.get_float("debounce_seconds")
    await _safe_edit(
        callback,
        "⚙️ параметры поведения (глобально, для всех):",
        reply_markup=kb.params_menu(typing_glob, debounce),
    )
    await _answer(callback)


@router.callback_query(F.data == "params:typing")
async def cb_params_typing(
    callback: CallbackQuery,
    state: FSMContext,
    global_repo: GlobalSettingsRepository,
    config: Config,
) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await state.clear()
    new_value = not await global_repo.get_bool("typing_enabled")
    await global_repo.set("typing_enabled", new_value)
    debounce = await global_repo.get_float("debounce_seconds")
    logger.info("user_id=%s event=global_typing_toggled enabled=%s", callback.from_user.id, new_value)
    await _safe_edit(
        callback,
        "⚙️ параметры поведения (глобально, для всех):",
        reply_markup=kb.params_menu(new_value, debounce),
    )
    await _answer(callback)


@router.callback_query(F.data == "params:debounce")
async def cb_params_debounce(callback: CallbackQuery, state: FSMContext, config: Config) -> None:
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    if not await _admin_only(callback, config):
        return
    await _set_pending_state(state, SettingsStates.waiting_debounce)
    await _safe_edit(
        callback,
        "⏱ отправь новое значение debounce в секундах (от 0.5 до 10):",
        reply_markup=kb.cancel_prompt(),
    )
    await _answer(callback)


@router.message(SettingsStates.waiting_debounce)
async def msg_new_debounce(
    message: Message,
    state: FSMContext,
    global_repo: GlobalSettingsRepository,
    config: Config,
) -> None:
    if not _is_private_message(message):
        await _reject_non_private_message(message)
        return
    if (await _fresh_pending_data(state, SettingsStates.waiting_debounce)) is None:
        await _expired_input_reply(message)
        return
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


@router.callback_query()
async def cb_unknown(callback: CallbackQuery) -> None:
    """Fallback for old, malformed, or otherwise unknown callback payloads."""
    if not _is_private_callback(callback):
        await _reject_non_private_callback(callback)
        return
    await _answer(
        callback,
        "неизвестная или устаревшая кнопка",
        show_alert=True,
    )
