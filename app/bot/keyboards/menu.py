"""Inline-клавиатуры меню (п. 14, 26 ТЗ)."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.ai.prompts import PERSONALITY_PRESETS


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🧹 очистить диалог", callback_data="menu:clear"))
    builder.row(InlineKeyboardButton(text="🎭 настройки характера", callback_data="menu:personality"))
    if is_admin:
        # глобальные настройки — одни на всех пользователей
        builder.row(InlineKeyboardButton(text="🧠 изменить промт (глобально)", callback_data="menu:prompt"))
        builder.row(InlineKeyboardButton(text="🤖 выбрать модель (глобально)", callback_data="menu:model"))
        builder.row(InlineKeyboardButton(text="⚙️ параметры (глобально)", callback_data="menu:params"))
    builder.row(InlineKeyboardButton(text="📋 текущие настройки", callback_data="menu:status"))
    return builder.as_markup()


def status_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🧠 что она обо мне помнит", callback_data="status:facts"))
    builder.row(InlineKeyboardButton(text="⬅️ назад", callback_data="menu:back"))
    return builder.as_markup()


def back_to_status() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ назад", callback_data="menu:status"))
    return builder.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="⬅️ назад", callback_data="menu:back"))
    return builder.as_markup()


def clear_confirm() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ только диалог", callback_data="clear:dialog"),
        InlineKeyboardButton(text="🧠 диалог + память", callback_data="clear:all"),
    )
    builder.row(InlineKeyboardButton(
        text="🗑 очистить всё (и настроение)", callback_data="clear:everything"
    ))
    builder.row(InlineKeyboardButton(text="❌ отмена", callback_data="menu:back"))
    return builder.as_markup()


def personality_menu(current: str, has_custom: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, preset in PERSONALITY_PRESETS.items():
        mark = "✓ " if key == current else ""
        builder.row(InlineKeyboardButton(
            text=f"{mark}{preset['title']}", callback_data=f"personality:{key}"
        ))
    custom_mark = "✓ " if current == "custom" else ""
    custom_text = "✍️ свой характер"
    if has_custom:
        custom_text += " (задан)"
    builder.row(InlineKeyboardButton(text=f"{custom_mark}{custom_text}", callback_data="personality:custom"))
    builder.row(InlineKeyboardButton(text="⬅️ назад", callback_data="menu:back"))
    return builder.as_markup()


def manipulator_warning() -> InlineKeyboardMarkup:
    """Подтверждение дл�� характера с эмоционально давящим стилем."""
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(
        text="✅ Применить", callback_data="personality:manipulator:confirm"
    ))
    builder.row(InlineKeyboardButton(
        text="❌ Назад", callback_data="personality:manipulator:cancel"
    ))
    return builder.as_markup()


def models_menu(models: list[str], current: str, page: int = 0, per_page: int = 8) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    start = page * per_page
    chunk = models[start : start + per_page]
    for model in chunk:
        mark = "✓ " if model == current else ""
        builder.row(InlineKeyboardButton(text=f"{mark}{model}", callback_data=f"model:{model}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"models_page:{page - 1}"))
    if start + per_page < len(models):
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"models_page:{page + 1}"))
    if nav:
        builder.row(*nav)
    builder.row(InlineKeyboardButton(text="⬅️ назад", callback_data="menu:back"))
    return builder.as_markup()


def params_menu(typing_enabled: bool, debounce: float) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    typing_text = "🔇 typing: выкл" if not typing_enabled else "⌨️ typing: вкл"
    builder.row(InlineKeyboardButton(text=typing_text, callback_data="params:typing"))
    builder.row(InlineKeyboardButton(
        text=f"⏱ debounce: {debounce:.1f} сек", callback_data="params:debounce"
    ))
    builder.row(InlineKeyboardButton(text="⬅️ назад", callback_data="menu:back"))
    return builder.as_markup()


def cancel_prompt() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="❌ отмена", callback_data="menu:back"))
    return builder.as_markup()
