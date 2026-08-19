"""Репозитории: единственное место, где живёт SQL (п. 19 ТЗ)."""

from __future__ import annotations

from datetime import datetime, timezone

import aiosqlite

from app.database.models import HistoryMessage, MemoryFact, UserSettings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    # Databases created by older versions used naive UTC timestamps.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class UserSettingsRepository:
    def __init__(self, db: aiosqlite.Connection, default_model: str, default_debounce: float):
        self._db = db
        self._default_model = default_model
        self._default_debounce = default_debounce

    async def get(self, user_id: int) -> UserSettings:
        cursor = await self._db.execute(
            "SELECT * FROM user_settings WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        if row is None:
            return await self._create_default(user_id)
        return UserSettings(
            user_id=row["user_id"],
            selected_model=row["selected_model"],
            custom_prompt=row["custom_prompt"],
            personality=row["personality"],
            mood=row["mood"] if "mood" in row.keys() else "",
            proactive_stage=row["proactive_stage"] if "proactive_stage" in row.keys() else 0,
            last_activity_ts=row["last_activity_ts"] if "last_activity_ts" in row.keys() else 0.0,
            last_chat_id=row["last_chat_id"] if "last_chat_id" in row.keys() else None,
            last_user_message_ts=row["last_user_message_ts"] if "last_user_message_ts" in row.keys() else 0.0,
            last_ai_message_ts=row["last_ai_message_ts"] if "last_ai_message_ts" in row.keys() else 0.0,
            custom_personality=row["custom_personality"] if "custom_personality" in row.keys() else "",
            typing_enabled=bool(row["typing_enabled"]),
            debounce_seconds=row["debounce_seconds"],
            created_at=_parse_datetime(row["created_at"]),
            updated_at=_parse_datetime(row["updated_at"]),
        )

    async def _create_default(self, user_id: int) -> UserSettings:
        now = _now()
        await self._db.execute(
            """INSERT INTO user_settings
               (user_id, selected_model, custom_prompt, personality,
                typing_enabled, debounce_seconds, created_at, updated_at)
               VALUES (?, ?, '', 'realistic', 1, ?, ?, ?)""",
            (user_id, self._default_model, self._default_debounce, now, now),
        )
        await self._db.commit()
        return await self.get(user_id)

    async def update(self, user_id: int, **fields) -> None:
        allowed = {"selected_model", "custom_prompt", "personality", "mood",
                   "proactive_stage", "last_activity_ts", "last_chat_id",
                   "last_user_message_ts", "last_ai_message_ts", "custom_personality",
                   "typing_enabled", "debounce_seconds"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return
        # гарантируем, что строка существует (upsert)
        await self.get(user_id)
        updates["updated_at"] = _now()
        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = [int(v) if isinstance(v, bool) else v for v in updates.values()]
        await self._db.execute(
            f"UPDATE user_settings SET {set_clause} WHERE user_id = ?",
            (*values, user_id),
        )
        await self._db.commit()

    async def get_recently_active(self, within_seconds: float) -> list[UserSettings]:
        """Пользователи с известным чатом и недавней активностью — для
        восстановления сессий после перезапуска (проактивность, доброе утро)."""
        import time as _time

        cutoff = _time.time() - within_seconds
        cursor = await self._db.execute(
            """SELECT user_id FROM user_settings
               WHERE last_chat_id IS NOT NULL AND last_activity_ts > ?""",
            (cutoff,),
        )
        rows = await cursor.fetchall()
        return [await self.get(row["user_id"]) for row in rows]


class GlobalSettingsRepository:
    """Глобальные настройки бота (одни на всех): модель, промт, typing, debounce.

    Меняет их только администратор; per-user остаются характер, настроение,
    история и факты.
    """

    def __init__(self, db: aiosqlite.Connection, default_model: str, default_debounce: float):
        self._db = db
        self._defaults = {
            "selected_model": default_model,
            "custom_prompt": "",
            "typing_enabled": "1",
            "debounce_seconds": str(default_debounce),
        }

    async def get_str(self, key: str) -> str:
        cursor = await self._db.execute(
            "SELECT value FROM global_settings WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        if row is None:
            return self._defaults.get(key, "")
        return row["value"]

    async def get_float(self, key: str) -> float:
        try:
            return float(await self.get_str(key))
        except ValueError:
            return float(self._defaults.get(key, "0") or 0)

    async def get_bool(self, key: str) -> bool:
        return (await self.get_str(key)).strip().lower() in {"1", "true", "yes", "on"}

    async def set(self, key: str, value) -> None:
        if isinstance(value, bool):
            value = "1" if value else "0"
        await self._db.execute(
            "INSERT INTO global_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
        await self._db.commit()


class HistoryRepository:
    def __init__(self, db: aiosqlite.Connection):
        self._db = db

    async def add(
        self, user_id: int, role: str, content: str,
        created_at: datetime | None = None,
    ) -> None:
        await self._db.execute(
            "INSERT INTO history (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content, created_at.isoformat(timespec="microseconds") if created_at else _now()),
        )
        await self._db.commit()

    async def get_recent(self, user_id: int, limit: int) -> list[HistoryMessage]:
        cursor = await self._db.execute(
            """SELECT role, content, created_at FROM history
               WHERE user_id = ? ORDER BY id DESC LIMIT ?""",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            HistoryMessage(user_id=user_id, role=r["role"], content=r["content"],
                           created_at=_parse_datetime(r["created_at"]))
            for r in reversed(rows)
        ]

    async def get_last_timestamps(self, user_id: int) -> dict[str, datetime | None]:
        """Return the latest event time for each conversational role."""
        cursor = await self._db.execute(
            """SELECT role, created_at FROM history
               WHERE user_id = ? ORDER BY id DESC LIMIT 1000""",
            (user_id,),
        )
        result: dict[str, datetime | None] = {"user": None, "assistant": None}
        for row in await cursor.fetchall():
            role = row["role"]
            if role in result and result[role] is None:
                result[role] = _parse_datetime(row["created_at"])
        return result

    async def clear(self, user_id: int) -> None:
        await self._db.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
        await self._db.commit()

    async def count(self, user_id: int) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) AS c FROM history WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row["c"]


class MemoryRepository:
    """Долгосрочная память: значимые факты о пользователе (п. 13 ТЗ)."""

    MAX_FACTS = 30

    def __init__(self, db: aiosqlite.Connection):
        self._db = db

    async def get_facts(self, user_id: int) -> list[str]:
        cursor = await self._db.execute(
            "SELECT fact FROM memory_facts WHERE user_id = ? ORDER BY id",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [r["fact"] for r in rows]

    async def count(self, user_id: int) -> int:
        cursor = await self._db.execute(
            "SELECT COUNT(*) AS c FROM memory_facts WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row["c"]

    async def replace_facts(self, user_id: int, facts: list[str]) -> None:
        facts = facts[: self.MAX_FACTS]
        await self._db.execute("DELETE FROM memory_facts WHERE user_id = ?", (user_id,))
        await self._db.executemany(
            "INSERT INTO memory_facts (user_id, fact, created_at) VALUES (?, ?, ?)",
            [(user_id, f, _now()) for f in facts],
        )
        await self._db.commit()

    async def clear(self, user_id: int) -> None:
        await self._db.execute("DELETE FROM memory_facts WHERE user_id = ?", (user_id,))
        await self._db.commit()
