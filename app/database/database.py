"""Подключение к SQLite (aiosqlite).

Слой доступа к данным изолирован в repository.py, поэтому переезд
на PostgreSQL позже потребует замены только этого модуля и repository (п. 19 ТЗ).
"""

from __future__ import annotations

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_settings (
    user_id          INTEGER PRIMARY KEY,
    selected_model   TEXT    NOT NULL,
    custom_prompt    TEXT    NOT NULL DEFAULT '',
    personality      TEXT    NOT NULL DEFAULT 'realistic',
    typing_enabled   INTEGER NOT NULL DEFAULT 1,
    debounce_seconds REAL    NOT NULL DEFAULT 2.0,
    created_at       TEXT    NOT NULL,
    updated_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    role       TEXT    NOT NULL,
    content    TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id, id);

CREATE TABLE IF NOT EXISTS memory_facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    fact       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_facts_user ON memory_facts(user_id, id);

CREATE TABLE IF NOT EXISTS global_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


async def init_db(path: str) -> aiosqlite.Connection:
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    await db.executescript(_SCHEMA)
    # миграция: колонка mood для баз, созданных ранними версиями
    cursor = await db.execute("PRAGMA table_info(user_settings)")
    columns = {row["name"] for row in await cursor.fetchall()}
    if "mood" not in columns:
        await db.execute("ALTER TABLE user_settings ADD COLUMN mood TEXT NOT NULL DEFAULT ''")
    if "proactive_stage" not in columns:
        await db.execute("ALTER TABLE user_settings ADD COLUMN proactive_stage INTEGER NOT NULL DEFAULT 0")
    if "last_activity_ts" not in columns:
        await db.execute("ALTER TABLE user_settings ADD COLUMN last_activity_ts REAL NOT NULL DEFAULT 0")
    if "last_chat_id" not in columns:
        await db.execute("ALTER TABLE user_settings ADD COLUMN last_chat_id INTEGER")
    if "last_user_message_ts" not in columns:
        await db.execute("ALTER TABLE user_settings ADD COLUMN last_user_message_ts REAL NOT NULL DEFAULT 0")
    if "last_ai_message_ts" not in columns:
        await db.execute("ALTER TABLE user_settings ADD COLUMN last_ai_message_ts REAL NOT NULL DEFAULT 0")
    if "custom_personality" not in columns:
        await db.execute("ALTER TABLE user_settings ADD COLUMN custom_personality TEXT NOT NULL DEFAULT ''")
    # миграция характера: пресет «милая и живая» (бывший дефолт) → «реалистичный»
    await db.execute("UPDATE user_settings SET personality = 'realistic' WHERE personality = 'default'")
    await db.commit()
    return db
