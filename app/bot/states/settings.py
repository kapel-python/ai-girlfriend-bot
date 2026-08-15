"""FSM-состояния aiogram (п. 27 ТЗ). Никаких самодельных состояний в словарях."""

from aiogram.fsm.state import State, StatesGroup


class SettingsStates(StatesGroup):
    waiting_custom_prompt = State()
    confirm_clear_dialog = State()
    waiting_debounce = State()
