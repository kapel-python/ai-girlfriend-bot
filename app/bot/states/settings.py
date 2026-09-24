"""FSM-состояния aiogram (п. 27 ТЗ). Никаких самодельных состояний в словарях."""

from aiogram.fsm.state import State, StatesGroup


class SettingsStates(StatesGroup):
    waiting_custom_prompt = State()
    confirm_clear_dialog = State()
    # A separate state keeps a personality delete confirmation from being
    # accepted by the clear-dialog callback (and vice versa).
    confirm_personality_delete = State()
    waiting_debounce = State()
    waiting_custom_personality = State()
    waiting_personality_create_title = State()
    waiting_personality_create_prompt = State()
    waiting_personality_edit_title = State()
    waiting_personality_edit_prompt = State()
