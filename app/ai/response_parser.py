"""Парсер ответов модели (п. 8, 12, 23 ТЗ).

Основной формат — JSON ``{"should_reply": bool, "messages": [...]}``.  Любой
невалидный протокол закрывается fail-closed: в Telegram не отправляется исходный
JSON.  Обычный текст без протокольных маркеров сохраняет совместимый fallback.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

NO_REPLY_MARKER = "[NO_REPLY]"
MAX_MESSAGES = 8
# Оставляем небольшой запас до Telegram-лимита 4096, как и в старой версии.
MAX_MESSAGE_LEN = 4000

_PROTOCOL_FIELD_RE = re.compile(
    r'"(?:should_reply|messages|initiative|reply|mood)"\s*:',
    re.IGNORECASE,
)
_PROTOCOL_LOOSE_RE = re.compile(
    r"(?:^|[{\s])(?:should_reply|messages|initiative|reply|mood)\s*:",
    re.IGNORECASE,
)
_SHOULD_REPLY_RE = re.compile(r'"should_reply"\s*:\s*(true|false)\b', re.IGNORECASE)
_MESSAGES_ANCHOR_RE = re.compile(r'"messages"\s*:\s*\[', re.IGNORECASE)
_MOOD_RE = re.compile(r'"mood"\s*:\s*"((?:\\.|[^"\\])*)"', re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*([\s\S]*?)\s*```\s*$", re.IGNORECASE)


@dataclass
class ParsedResponse:
    should_reply: bool
    messages: list[str] = field(default_factory=list)
    mood: str = ""
    # Used only by the proactive DECISION request.  Normal replies leave it
    # empty, so the existing reply/mood algorithm remains unchanged.
    initiative: str = ""


def _hard_chunks(text: str, limit: int) -> list[str]:
    """Split text at safe boundaries without dropping the tail.

    This is the final safety net for a single enormous block.  It deliberately
    returns more than the requested logical part count when preserving all text
    requires it.
    """

    if limit <= 0:
        raise ValueError("limit должен быть положительным")
    remaining = text
    result: list[str] = []
    while len(remaining) > limit:
        window = remaining[:limit]
        # Prefer a paragraph, then a line, then a sentence, then a word.  The
        # indexes are all within the current bounded window.
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            sentence_cut = max(window.rfind(". "), window.rfind("! "), window.rfind("? "))
            if sentence_cut >= limit // 2:
                cut = sentence_cut + 1
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut <= 0:
            cut = limit

        chunk = remaining[:cut]
        if not chunk.strip():
            # A boundary consisting only of whitespace must not create an
            # empty message or an infinite loop.
            chunk = remaining[:limit]
            cut = limit
        result.append(chunk)
        # Do not trim boundary whitespace: concatenating the chunks should
        # retain the complete fallback, not just its non-whitespace characters.
        remaining = remaining[cut:]
    if remaining:
        result.append(remaining)
    return result


def _logical_blocks(text: str) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if len(paragraphs) < 2:
        # A single paragraph can still contain several natural sentences.
        sentences = [
            part.strip()
            for part in re.split(r"(?<=[.!?…])\s+", text)
            if part.strip()
        ]
        if len(sentences) >= 2:
            return sentences
    if not paragraphs:
        return [text.strip()] if text.strip() else []
    return paragraphs


def _exact_logical_chunks(text: str, desired_parts: int, limit: int) -> list[str]:
    """Cut the original string at boundaries without losing separators."""

    if not text:
        return []
    desired_parts = max(1, desired_parts)
    result: list[str] = []
    start = 0
    parts_left = desired_parts
    while start < len(text):
        remaining = len(text) - start
        if remaining <= limit and parts_left <= 1:
            result.append(text[start:])
            break

        hard_end = min(len(text), start + limit)
        proposed = start + max(1, (remaining + parts_left - 1) // parts_left)
        proposed = min(proposed, hard_end)
        minimum = start + max(1, min(limit // 2, remaining // max(2, parts_left)))
        window_start = max(start + 1, minimum)

        # Prefer paragraph/line/sentence boundaries, then any whitespace.  The
        # end position is retained in the original string, so the next chunk
        # can begin with the separator and concatenation remains lossless.
        candidates: list[tuple[int, int]] = []
        segment = text[window_start:proposed]
        for match in re.finditer(r"\n\n|\n|(?<=[.!?…])\s+|\s", segment):
            candidates.append((window_start + match.end(), 3))
        if not candidates:
            cut = proposed
        else:
            # Highest-priority boundary closest to the proportional target.
            best_rank = max(rank for _, rank in candidates)
            cut = next(
                position
                for position, rank in reversed(candidates)
                if rank == best_rank
            )
        if cut <= start:
            cut = proposed
        result.append(text[start:cut])
        start = cut
        parts_left -= 1
        if parts_left <= 0:
            # The remainder is still hard-bounded below; this branch is mostly
            # defensive for unusual boundary layouts.
            if start < len(text):
                result.extend(_hard_chunks(text[start:], limit))
            break
    return [part for part in result if part]


def split_fallback_text(text: str, max_parts: int = 3) -> list[str]:
    """Split a long non-JSON answer into complete Telegram-safe chunks.

    ``max_parts`` is a preference for ordinary-sized answers, not a truncation
    limit.  If the text is larger than that preference, all remaining text is
    still emitted in chunks no longer than :data:`MAX_MESSAGE_LEN`.
    """

    if not isinstance(text, str):
        return []
    text = text.strip()
    if not text:
        return []
    if len(text) <= MAX_MESSAGE_LEN and len(text) <= 400:
        return [text]

    try:
        requested_parts = int(max_parts)
    except (TypeError, ValueError):
        requested_parts = 3
    requested_parts = max(1, requested_parts)
    needed_parts = max(1, math.ceil(len(text) / MAX_MESSAGE_LEN))
    blocks = _logical_blocks(text)
    if len(text) <= 400:
        target_parts = 1
    elif needed_parts > requested_parts:
        target_parts = needed_parts
    elif len(blocks) >= requested_parts:
        target_parts = requested_parts
    else:
        target_parts = min(requested_parts, max(1, len(blocks)))

    result = _exact_logical_chunks(text, target_parts, MAX_MESSAGE_LEN)
    if not result:
        result = _hard_chunks(text, MAX_MESSAGE_LEN)
    return result


def _strip_fence(text: str) -> str:
    stripped = text.strip()
    match = _FENCE_RE.match(stripped)
    return match.group(1).strip() if match else stripped


def _loads_dict(value: str) -> dict | None:
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, UnicodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _known_json_object(value: dict[str, Any]) -> bool:
    return bool(
        {"should_reply", "messages", "initiative", "reply", "mood", "facts"} & value.keys()
    )


def _balanced_object_end(text: str, start: int) -> int | None:
    """Return the closing brace for a JSON object, respecting strings."""

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
            if depth < 0:
                return None
    return None


def _extract_json(text: str) -> dict | None:
    """Extract the first valid object without counting braces inside strings."""

    if not isinstance(text, str):
        return None
    cleaned = _strip_fence(text)
    first_object: dict | None = None
    direct = _loads_dict(cleaned)
    if direct is not None:
        if _known_json_object(direct):
            return direct
        first_object = direct

    # A fenced block embedded in explanatory text is a common model response.
    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE):
        direct = _loads_dict(match.group(1).strip())
        if direct is not None:
            if _known_json_object(direct):
                return direct
            if first_object is None:
                first_object = direct

    # Try each opening brace.  Invalid candidates do not prevent a later valid
    # object (for example after a prose prefix) from being found.  Prefer a
    # protocol-shaped object over an unrelated JSON object in the same reply.
    search_from = 0
    while True:
        start = cleaned.find("{", search_from)
        if start < 0:
            return first_object
        end = _balanced_object_end(cleaned, start)
        if end is not None:
            parsed = _loads_dict(cleaned[start : end + 1])
            if parsed is not None:
                if _known_json_object(parsed):
                    return parsed
                if first_object is None:
                    first_object = parsed
        search_from = start + 1


def _find_matching_bracket(text: str, start: int) -> int | None:
    """Find the matching ``]`` for a messages array, respecting JSON strings."""

    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
            if depth == 0 and char == "]":
                return index
            if depth < 0:
                return None
    return None


def _json_string_values(segment: str) -> list[str]:
    values: list[str] = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(segment):
        if segment[index] != '"':
            index += 1
            continue
        try:
            value, end = decoder.raw_decode(segment, index)
        except (TypeError, ValueError, UnicodeError):
            index += 1
            continue
        if isinstance(value, str):
            values.append(value)
        index = max(index + 1, end)
    return values


def _protocol_text(text: str) -> bool:
    return bool(_PROTOCOL_FIELD_RE.search(text) or _PROTOCOL_LOOSE_RE.search(text))


def _has_valid_json_value(text: str) -> bool:
    """Whether the whole response (or a fenced value) is valid JSON."""

    candidates = [text]
    candidates.extend(
        match.group(1).strip()
        for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    )
    for candidate in candidates:
        try:
            json.loads(candidate)
        except (TypeError, ValueError, UnicodeError):
            continue
        return True
    return False


def _safe_mood(data: dict[str, Any]) -> str:
    mood = data.get("mood")
    return mood.strip()[:120] if isinstance(mood, str) else ""


def _safe_initiative(data: dict[str, Any]) -> str:
    initiative = data.get("initiative")
    if not isinstance(initiative, str):
        return ""
    value = initiative.strip().upper()
    return value if value in {"NO", "MAYBE", "YES"} else ""


def _recover_protocol_messages(text: str) -> tuple[list[str], str] | None:
    """Recover only strings from an obviously malformed protocol response.

    Recovery is intentionally conservative: an explicit true decision is
    required, and values are decoded as JSON strings rather than copied as raw
    protocol text.
    """

    decisions = _SHOULD_REPLY_RE.findall(text)
    if not decisions or decisions[-1].lower() != "true":
        return None
    anchors = list(_MESSAGES_ANCHOR_RE.finditer(text))
    if not anchors:
        return None
    start = anchors[-1].end()
    end = _find_matching_bracket(text, start)
    segment = text[start : end if end is not None else len(text)]
    values = _json_string_values(segment)
    if not values:
        return None
    mood_match = _MOOD_RE.search(text)
    mood = ""
    if mood_match:
        try:
            mood = str(json.loads(f'"{mood_match.group(1)}"')).strip()[:120]
        except (TypeError, ValueError, UnicodeError):
            mood = ""
    return values, mood


def _sanitize_recovered_values(values: list[str], *, depth: int = 0) -> list[str]:
    """Sanitize values recovered from malformed protocol text."""

    if depth > 3:
        return []
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned:
            continue
        if _protocol_text(cleaned):
            inner = _extract_json(cleaned)
            if isinstance(inner, dict):
                inner_messages = _sanitize_protocol_messages(inner, depth=depth + 1)
                if inner_messages is not None:
                    result.extend(inner_messages)
                    continue
            nested = _recover_protocol_messages(cleaned)
            if nested is not None:
                result.extend(_sanitize_recovered_values(nested[0], depth=depth + 1))
            continue
        result.extend(split_fallback_text(cleaned))
    return result


def _sanitize_protocol_messages(data: dict[str, Any], *, depth: int = 0) -> list[str] | None:
    """Validate and unwrap protocol messages; ``None`` means invalid protocol."""

    if depth > 3:
        return None
    should_reply = data.get("should_reply")
    if type(should_reply) is not bool:
        return None
    if not should_reply:
        return []

    if "messages" in data:
        messages_raw = data.get("messages")
        if not isinstance(messages_raw, list):
            return None
    elif "reply" in data:
        reply = data.get("reply")
        if not isinstance(reply, str):
            return None
        messages_raw = [reply]
    else:
        return None

    result: list[str] = []
    for message in messages_raw:
        if not isinstance(message, str):
            # Do not salvage a partially valid protocol object: fail closed.
            return None
        value = message.strip()
        if not value:
            continue
        if _protocol_text(value):
            inner = _extract_json(value)
            if isinstance(inner, dict):
                inner_messages = _sanitize_protocol_messages(inner, depth=depth + 1)
                if inner_messages is not None:
                    result.extend(inner_messages)
                    continue
            recovered = _recover_protocol_messages(value)
            if recovered is not None:
                recovered_messages, _ = recovered
                result.extend(_sanitize_recovered_values(recovered_messages, depth=depth + 1))
                continue
            # Never forward a raw protocol object.
            logger.info("Отброшено сообщение с сырым протокольным JSON")
            continue
        result.extend(split_fallback_text(value))

    return result[:MAX_MESSAGES]


def _parsed_protocol(data: dict[str, Any]) -> ParsedResponse:
    mood = _safe_mood(data)
    initiative = _safe_initiative(data)
    should_reply = data.get("should_reply")
    # Exact bool check is intentional: bool("false") is True and accepting
    # arbitrary truthy values is a common source of unsolicited messages.
    if type(should_reply) is not bool:
        return ParsedResponse(should_reply=False, mood=mood, initiative=initiative)
    if not should_reply:
        return ParsedResponse(should_reply=False, mood=mood, initiative=initiative)

    messages = _sanitize_protocol_messages(data)
    if not messages:
        return ParsedResponse(should_reply=False, mood=mood, initiative=initiative)
    return ParsedResponse(
        should_reply=True,
        messages=messages[:MAX_MESSAGES],
        mood=mood,
        initiative=initiative,
    )


def parse_response(raw: str) -> ParsedResponse:
    if not isinstance(raw, str):
        return ParsedResponse(should_reply=False)
    text = raw.strip()
    if not text:
        return ParsedResponse(should_reply=False)
    if NO_REPLY_MARKER in text:
        return ParsedResponse(should_reply=False)

    data = _extract_json(text)
    if isinstance(data, dict):
        return _parsed_protocol(data)

    # A valid JSON value which is not the expected object must not be sent as
    # a user-facing message (it could be a raw protocol array/string).
    if _has_valid_json_value(text):
        return ParsedResponse(should_reply=False)

    if _protocol_text(text):
        recovered = _recover_protocol_messages(text)
        if recovered is not None:
            recovered_messages, mood = recovered
            messages = _sanitize_recovered_values(recovered_messages)
            if messages:
                return ParsedResponse(
                    should_reply=True,
                    messages=messages[:MAX_MESSAGES],
                    mood=mood,
                )
        # Malformed protocol is fail-closed, never echoed to the chat.
        return ParsedResponse(should_reply=False)

    # Compatibility fallback: ordinary prose is a valid model answer.
    logger.info("Ответ модели не в JSON, используем fallback-режим")
    return ParsedResponse(should_reply=True, messages=split_fallback_text(text))


def parse_initiative(raw: str) -> str:
    """Parse the small, mood-free DECISION response.

    Unknown/malformed values are treated as NO by the caller.  This keeps a
    broken model response from becoming an unsolicited message.
    """

    data = _extract_json(raw.strip() if isinstance(raw, str) else "")
    if not isinstance(data, dict):
        return "NO"
    value = data.get("initiative")
    if not isinstance(value, str):
        return "NO"
    value = value.strip().upper()
    return value if value in {"NO", "MAYBE", "YES"} else "NO"


def parse_facts(raw: str) -> list[str] | None:
    """Parse the facts response; malformed/non-list data fails closed."""

    data = _extract_json(raw.strip() if isinstance(raw, str) else "")
    if not isinstance(data, dict) or not isinstance(data.get("facts"), list):
        return None
    result: list[str] = []
    for fact in data["facts"]:
        if isinstance(fact, str):
            value = fact.strip()
        elif isinstance(fact, (int, float)) and not isinstance(fact, bool):
            if isinstance(fact, float) and not math.isfinite(fact):
                return None
            value = str(fact).strip()
        else:
            return None
        if value:
            result.append(value)
    return result
