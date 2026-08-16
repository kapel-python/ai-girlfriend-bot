"""Симуляция человеческого времени набора текста (п. 4, 5 ТЗ).

Модель: базовая скорость в символах/секунду + поправки на пунктуацию,
переносы строк, эмодзи, длинные слова + небольшой случайный jitter.
Результат ограничен разумными min/max.
"""

from __future__ import annotations

import random
import re

# диапазон скорости набора, символов в секунду
# (базовые 4.5–8.0 ≈ 60-90 слов/мин; +40% — она печатает шустро)
_CPS_MIN = 6.3
_CPS_MAX = 11.2

# поправки, секунды
_PUNCTUATION_PAUSE = 0.12      # за знак конца предложения
_COMMA_PAUSE = 0.05            # за запятую/тире
_NEWLINE_PAUSE = 0.35          # за перенос строки
_EMOJI_TIME = 0.22             # за эмодзи (поиск на клавиатуре)
_LONG_WORD_PENALTY = 0.08      # за символ сверх 10 в длинном слове

# границы результата
_MIN_DURATION = 0.6
_MAX_DURATION = 28.0

# случайная вариативность: ±12%
_JITTER_MIN = 0.9
_JITTER_MAX = 1.12

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF\uFE0F]"
)
_END_PUNCT_RE = re.compile(r"[.!?…]+")
_COMMA_RE = re.compile(r"[,;:\-—]")


def _count_emoji(text: str) -> int:
    return len(_EMOJI_RE.findall(text))


def calculate_typing_duration(text: str) -> float:
    """Возвращает реалистичное время набора текста в секундах."""
    if not text:
        return _MIN_DURATION

    chars = len(text)
    words = text.split()

    # базовое время по символам
    cps = random.uniform(_CPS_MIN, _CPS_MAX)
    duration = chars / cps

    # пунктуация
    duration += len(_END_PUNCT_RE.findall(text)) * _PUNCTUATION_PAUSE
    duration += len(_COMMA_RE.findall(text)) * _COMMA_PAUSE

    # переносы строк
    duration += text.count("\n") * _NEWLINE_PAUSE

    # эмодзи
    duration += _count_emoji(text) * _EMOJI_TIME

    # длинные слова
    for word in words:
        if len(word) > 10:
            duration += (len(word) - 10) * _LONG_WORD_PENALTY

    # небольшая естественная вариативность
    duration *= random.uniform(_JITTER_MIN, _JITTER_MAX)

    return max(_MIN_DURATION, min(_MAX_DURATION, duration))


def calculate_pause_between_messages() -> float:
    """Небольшая естественная пауза между двумя сообщениями подряд."""
    return random.uniform(0.4, 1.1)
