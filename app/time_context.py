"""Helpers for timestamps passed to the language model.

The timestamp is deliberately kept out of the text shown to the user.  It is
stored with every history row and added to the model-facing representation of
that row instead.
"""

from __future__ import annotations

from datetime import datetime

from app.config import MSK


TIMEZONE_NAME = getattr(MSK, "key", None) or "Europe/Moscow"


def now() -> datetime:
    return datetime.now(MSK)


def iso(value: datetime | None = None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=MSK)
    return value.isoformat(timespec="microseconds")


def epoch(value: datetime) -> float:
    if value.tzinfo is None:
        value = value.replace(tzinfo=MSK)
    return value.timestamp()


def elapsed(now_value: datetime, previous: datetime | None) -> float | None:
    if previous is None:
        return None
    return max(0.0, (now_value - previous).total_seconds())


def model_message(role: str, content: str, created_at: datetime) -> str:
    """Make a history message unambiguous for the model without leaking it."""
    return (
        f"<message role=\"{role}\" timestamp=\"{iso(created_at)}\">\n"
        f"{content}\n"
        "</message>"
    )
