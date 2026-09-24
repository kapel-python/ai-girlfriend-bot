"""Inline-клавиатуры меню (п. 14, 26 ТЗ).

The bot uses plain Telegram text rather than a global HTML parse mode.  This
module still keeps callback payloads bounded: Telegram permits at most 64
UTF-8 bytes in ``callback_data``.  Long, user/API supplied values are
represented by a short deterministic digest; handlers resolve that digest
against the current list of values before using it.
"""

from __future__ import annotations

from hashlib import sha256
from collections.abc import Iterable

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.database.models import PersonalityPreset


MAX_CALLBACK_DATA_BYTES = 64
_CLEAR_KINDS = frozenset({"dialog", "all", "mood", "everything"})
_ZERO_NONCE = "00000000"


def _value_text(value: object, *, limit: int | None = None) -> str:
    text = "" if value is None else str(value)
    if limit is not None:
        text = text[:limit]
    # The bot uses plain Telegram text. Keep labels literal instead of
    # displaying HTML entities such as ``&amp;`` to the user.
    return text.replace("\x00", "")


def _value_digest(value: object) -> str:
    return "~" + sha256(str(value).encode("utf-8")).hexdigest()[:16]


def _validate_callback_data(data: str) -> str:
    value = str(data)
    size = len(value.encode("utf-8"))
    if size > MAX_CALLBACK_DATA_BYTES:
        raise ValueError(
            f"callback_data is {size} UTF-8 bytes; Telegram allows "
            f"{MAX_CALLBACK_DATA_BYTES}"
        )
    return value


def callback_data_for_value(prefix: str, value: object) -> str:
    """Return a bounded ``prefix:value`` callback payload.

    Normal values remain backwards compatible.  Only values which would
    exceed Telegram's limit are shortened, and the shortened form is resolved
    by :func:`resolve_callback_value` in the handler.
    """
    raw_value = str(value)
    raw = f"{prefix}:{raw_value}"
    if len(raw.encode("utf-8")) <= MAX_CALLBACK_DATA_BYTES:
        return raw
    shortened = f"{prefix}:{_value_digest(raw_value)}"
    return _validate_callback_data(shortened)


def resolve_callback_value(raw: object, values: Iterable[object]) -> str | None:
    """Resolve a raw callback value, including a length-safe digest form."""
    if raw is None:
        return None
    candidate = str(raw)
    values_as_strings = [str(value) for value in values]
    if candidate in values_as_strings:
        return candidate
    if candidate.startswith("~"):
        for value in values_as_strings:
            if _value_digest(value) == candidate:
                return value
    return None


def _button(text: object, callback_data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=_value_text(text),
        callback_data=_validate_callback_data(callback_data),
    )


def _dynamic_button(prefix: str, value: object, text: object | None = None) -> InlineKeyboardButton:
    return _button(
        prefix if text is None else text,
        callback_data_for_value(prefix, value),
    )


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(_button("🧹 очистить диалог", "menu:clear"))
    builder.row(_button("🎭 настройки характера", "menu:personality"))
    if is_admin:
        # глобальные настройки — одни на всех пользователей
        builder.row(_button("🧠 изменить промт (глобально)", "menu:prompt"))
        builder.row(_button("🤖 выбрать модель (глобально)", "menu:model"))
        builder.row(_button("⚙️ параметры (глобально)", "menu:params"))
    builder.row(_button("📋 текущие настройки", "menu:status"))
    return builder.as_markup()


def status_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(_button("🧠 что она обо мне помнит", "status:facts"))
    builder.row(_button("⬅️ назад", "menu:back"))
    return builder.as_markup()


def back_to_status() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(_button("⬅️ назад", "menu:status"))
    return builder.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(_button("⬅️ назад", "menu:back"))
    return builder.as_markup()


def clear_options() -> InlineKeyboardMarkup:
    """Available cleanup scopes. Each scope leads to a separate confirmation."""
    builder = InlineKeyboardBuilder()
    builder.row(
        _button("💬 только диалог", "clear:request:dialog"),
        _button("🧠 диалог + память", "clear:request:all"),
    )
    builder.row(_button("💭 только настроение", "clear:request:mood"))
    builder.row(
        _button("🗑 всё (диалог, память и настроение)", "clear:request:everything")
    )
    builder.row(_button("❌ отмена", "menu:back"))
    return builder.as_markup()


def _safe_nonce(nonce: str | None) -> str:
    """Normalize a caller-provided nonce; an invalid one can never delete."""
    if nonce is None:
        return _ZERO_NONCE
    value = str(nonce)
    if len(value) != 8 or any(char not in "0123456789abcdefABCDEF" for char in value):
        return _ZERO_NONCE
    return value.lower()


def clear_delete_confirm(kind: str, nonce: str | None = None) -> InlineKeyboardMarkup:
    """Build a confirmation button carrying the one-time operation token.

    ``nonce`` is intentionally optional for source compatibility with older
    callers.  A missing/invalid token becomes a harmless placeholder and the
    handler rejects it; real confirmations always pass the token generated by
    the handler.
    """
    safe_kind = kind if kind in _CLEAR_KINDS else "invalid"
    token = _safe_nonce(nonce)
    builder = InlineKeyboardBuilder()
    builder.row(
        _button(
            "✅ Да, удалить",
            f"clear:confirm:{safe_kind}:{token}",
        )
    )
    # The cancel action deliberately goes through the normal menu transition,
    # which clears the pending operation state.
    builder.row(_button("❌ Отмена", "menu:clear"))
    return builder.as_markup()


# Backwards-compatible alias for callers outside the handler module.
clear_confirm = clear_options


def personality_menu(
    personalities: list[PersonalityPreset], current: str, has_custom: bool = False,
    is_admin: bool = False,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if is_admin:
        builder.row(_button("⚙️ Настройки характера", "personality_admin:manage"))
    for preset in personalities:
        mark = "✓ " if preset.key == current else ""
        builder.row(
            _dynamic_button(
                "personality",
                preset.key,
                f"{mark}{preset.title}",
            )
        )
    custom_mark = "✓ " if current == "custom" else ""
    custom_text = "✍️ свой характер"
    if has_custom:
        custom_text += " (задан)"
    builder.row(_button(f"{custom_mark}{custom_text}", "personality:custom"))
    builder.row(_button("⬅️ назад", "menu:back"))
    return builder.as_markup()


def personality_admin_menu(personalities: list[PersonalityPreset]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(_button("➕ Добавить характер", "personality_admin:add"))
    for preset in personalities:
        builder.row(
            _dynamic_button(
                "personality_admin:item",
                preset.key,
                preset.title,
            )
        )
    builder.row(_button("⬅️ назад", "menu:personality"))
    return builder.as_markup()


def personality_admin_item(preset: PersonalityPreset) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        _dynamic_button(
            "personality_admin:view",
            preset.key,
            "👁 Просмотр описания",
        )
    )
    builder.row(
        _dynamic_button(
            "personality_admin:edit",
            preset.key,
            "✏️ Изменить",
        )
    )
    builder.row(
        _dynamic_button(
            "personality_admin:delete",
            preset.key,
            "🗑 Удалить",
        )
    )
    builder.row(
        _dynamic_button(
            "personality_admin:stats",
            preset.key,
            "📊 Статистика",
        )
    )
    builder.row(_button("⬅️ к списку", "personality_admin:manage"))
    return builder.as_markup()


def personality_edit_menu(key: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        _dynamic_button(
            "personality_admin:edit_title",
            key,
            "✏️ Изменить имя",
        )
    )
    builder.row(
        _dynamic_button(
            "personality_admin:edit_prompt",
            key,
            "📝 Изменить описание",
        )
    )
    builder.row(
        _dynamic_button(
            "personality_admin:item",
            key,
            "⬅️ назад",
        )
    )
    return builder.as_markup()


def personality_delete_confirm(key: str, nonce: str | None = None) -> InlineKeyboardMarkup:
    """Build a personality-delete confirmation.

    The key is kept in the handler's FSM data rather than in callback_data;
    this leaves room for the short one-time nonce and also makes old buttons
    useless after the operation is replaced.  ``key`` remains in the public
    signature for compatibility with the old keyboard API.
    """
    token = _safe_nonce(nonce)
    builder = InlineKeyboardBuilder()
    builder.row(
        _button(
            "✅ Да, удалить",
            f"personality_admin:delete_confirm:{token}",
        )
    )
    # Preserve the old navigation target while keeping the key bounded.
    builder.row(_dynamic_button("personality_admin:item", key, "❌ Отмена"))
    return builder.as_markup()


def manipulator_warning() -> InlineKeyboardMarkup:
    """Подтверждение для характера с эмоционально давящим стилем."""
    builder = InlineKeyboardBuilder()
    builder.row(_button("✅ Применить", "personality:manipulator:confirm"))
    builder.row(_button("❌ Назад", "personality:manipulator:cancel"))
    return builder.as_markup()


def adult_warning() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(_button("✅ Мне есть 18 лет", "personality:18plus:confirm"))
    builder.row(_button("❌ Назад", "personality:18plus:cancel"))
    return builder.as_markup()


def models_menu(
    models: list[str], current: str, page: int = 0, per_page: int = 8
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    try:
        page = max(0, int(page))
    except (TypeError, ValueError):
        page = 0
    per_page = max(1, int(per_page))
    start = page * per_page
    chunk = models[start : start + per_page]
    for model in chunk:
        mark = "✓ " if model == current else ""
        builder.row(_dynamic_button("model", model, f"{mark}{model}"))
    nav = []
    if page > 0:
        nav.append(_button("◀️", f"models_page:{page - 1}"))
    if start + per_page < len(models):
        nav.append(_button("▶️", f"models_page:{page + 1}"))
    if nav:
        builder.row(*nav)
    builder.row(_button("⬅️ назад", "menu:back"))
    return builder.as_markup()


def params_menu(typing_enabled: bool, debounce: float) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    typing_text = "🔇 typing: выкл" if not typing_enabled else "⌨️ typing: вкл"
    builder.row(_button(typing_text, "params:typing"))
    builder.row(_button(f"⏱ debounce: {debounce:.1f} сек", "params:debounce"))
    builder.row(_button("⬅️ назад", "menu:back"))
    return builder.as_markup()


def cancel_prompt() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(_button("❌ отмена", "menu:back"))
    return builder.as_markup()
