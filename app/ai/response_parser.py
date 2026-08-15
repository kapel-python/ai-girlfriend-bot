"""Парсер ответа модели (п. 8, 12, 23 ТЗ).

Основной формат — JSON {"should_reply": bool, "messages": [...]}.
Парсер устойчив к типичным нарушениям:
- JSON в markdown-ограждении ```json ... ```
- лишний текст вокруг JSON
- вообще не JSON (модель ответила обычным текстом) — считаем это ответом
- маркер [NO_REPLY] — считаем отказом от ответа
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

NO_REPLY_MARKER = "[NO_REPLY]"
MAX_MESSAGES = 8
MAX_MESSAGE_LEN = 4000  # запас под лимит Telegram 4096


@dataclass
class ParsedResponse:
    should_reply: bool
    messages: list[str] = field(default_factory=list)
    mood: str = ""


def split_fallback_text(text: str, max_parts: int = 3) -> list[str]:
    """Нарезает длинный не-JSON ответ на 1-3 логических сообщения (улучшение 7).

    Делит по абзацам, затем по предложениям; короткий текст не трогает.
    """
    text = text.strip()
    if len(text) <= 400:
        return [text]

    # сначала по абзацам
    blocks = [b.strip() for b in re.split(r"\n\s*\n|\n", text) if b.strip()]
    if len(blocks) < 2:
        # затем по предложениям
        blocks = [s.strip() for s in re.split(r"(?<=[.!?…])\s+", text) if s.strip()]
    if len(blocks) < 2:
        return [text[:MAX_MESSAGE_LEN]]

    # жадно распределяем блоки по max_parts частям сбалансированной длины
    target_len = len(text) / max_parts
    parts: list[str] = []
    current = ""
    for block in blocks:
        if (
            current
            and len(parts) < max_parts - 1
            and len(current) >= target_len
        ):
            parts.append(current)
            current = block
        else:
            current = f"{current}\n{block}" if current else block
    if current:
        parts.append(current)
    parts = [p[:MAX_MESSAGE_LEN] for p in parts if p.strip()]
    return parts or [text[:MAX_MESSAGE_LEN]]


def _extract_json(text: str) -> dict | None:
    # убираем markdown-ограждения
    cleaned = re.sub(r"```(?:json)?\s*", "", text).strip()
    try:
        return json.loads(cleaned)
    except (ValueError, TypeError):
        pass
    # ищем первый сбалансированный {...} блок
    start = cleaned.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(cleaned)):
        if cleaned[i] == "{":
            depth += 1
        elif cleaned[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[start : i + 1])
                except (ValueError, TypeError):
                    return None
    return None


def parse_response(raw: str) -> ParsedResponse:
    text = raw.strip()

    # пустой или whitespace-only ответ — считаем отказом от ответа
    if not text:
        return ParsedResponse(should_reply=False)

    if NO_REPLY_MARKER in text:
        return ParsedResponse(should_reply=False)

    data = _extract_json(text)
    if isinstance(data, dict):
        should_reply = bool(data.get("should_reply", True))
        mood = str(data.get("mood") or "").strip()[:120]
        messages_raw = data.get("messages")
        if messages_raw is None and "reply" in data:
            messages_raw = [data["reply"]] if data["reply"] else []
        if not isinstance(messages_raw, list):
            messages_raw = [str(messages_raw)] if messages_raw else []

        messages = [
            m.strip()[:MAX_MESSAGE_LEN]
            for m in messages_raw
            if isinstance(m, str) and m.strip()
        ]

        # модель иногда вкладывает протокольный JSON внутрь сообщения —
        # разворачиваем такие вложения, чтобы пользователь не видел сырой JSON
        unwrapped: list[str] = []
        for m in messages:
            if m.startswith("{") and "should_reply" in m:
                inner_msgs: list[str] = []
                inner = _extract_json(m)
                if isinstance(inner, dict) and isinstance(inner.get("messages"), list):
                    inner_msgs = [
                        s.strip()[:MAX_MESSAGE_LEN]
                        for s in inner["messages"]
                        if isinstance(s, str) and s.strip()
                    ]
                    if not mood and inner.get("mood"):
                        mood = str(inner["mood"]).strip()[:120]
                if not inner_msgs:
                    # невалидный JSON — вытаскиваем строки из блока "messages" регэкспом
                    block = re.search(r'"messages"\s*:\s*\[(.*?)\]', m, re.S)
                    if block:
                        inner_msgs = [
                            s.strip()[:MAX_MESSAGE_LEN]
                            for s in re.findall(r'"([^"\n]{1,4000})"', block.group(1))
                            if s.strip()
                        ]
                if inner_msgs:
                    unwrapped.extend(inner_msgs)
                    continue
                # протокольный мусор без содержимого — выбрасываем
                logger.info("Отброшено сообщение с сырым протокольным JSON")
                continue
            unwrapped.append(m)
        messages = unwrapped[:MAX_MESSAGES]

        if not should_reply or not messages:
            return ParsedResponse(should_reply=False, mood=mood)
        return ParsedResponse(should_reply=True, messages=messages, mood=mood)

    # верхний уровень: JSON сломан (например, неэкранированные кавычки),
    # но структура протокола видна — вытаскиваем сообщения регэкспом
    if "should_reply" in text and '"messages"' in text:
        anchors = list(re.finditer(r'"messages"\s*:\s*\[', text))
        if anchors:
            # берём ПОСЛЕДНИЙ блок "messages": при вложенности он самый внутренний
            start = anchors[-1].end()
            end = text.find("]", start)
            segment = text[start:end if end != -1 else len(text)]
            msgs = [
                s.strip()[:MAX_MESSAGE_LEN]
                for s in re.findall(r'"([^"\n]{1,4000})"', segment)
                if s.strip()
            ]
            if msgs:
                logger.info("Битый JSON протокола, сообщения извлечены регэкспом")
                return ParsedResponse(should_reply=True, messages=msgs[:MAX_MESSAGES])

    # fallback: модель вернула обычный текст — нарезаем на логические сообщения
    logger.info("Ответ модели не в JSON, используем fallback-режим")
    return ParsedResponse(should_reply=True, messages=split_fallback_text(text))


def parse_facts(raw: str) -> list[str] | None:
    """Парсит ответ извлечения фактов. None — если распарсить не удалось."""
    data = _extract_json(raw.strip())
    if isinstance(data, dict) and isinstance(data.get("facts"), list):
        return [str(f).strip() for f in data["facts"] if str(f).strip()]
    return None
