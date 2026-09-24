"""Offline regression suite for conversation lifecycle guarantees.

Run from repository root:

    .venv/bin/python -m app.tests.regression_conversation

No real AI/Telegram calls and no project ``bot.db`` access are performed.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from dataclasses import replace
from types import SimpleNamespace

import aiosqlite
from aiogram.exceptions import TelegramAPIError

from app.config import Config
from app.conversation.manager import CancelReason, ConversationManager
from app.conversation.memory import MemoryService
from app.conversation.sender import (
    SendProgress,
    SendResult,
    TelegramSender,
)
from app.database.database import init_db
from app.database.repository import (
    HistoryRepository,
    MemoryRepository,
    PendingMessageRepository,
    UserSettingsRepository,
)


PASSED: list[str] = []


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    PASSED.append(name)
    print(f"✅ {name}")


async def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timeout waiting for condition")
        await asyncio.sleep(0.01)
    await asyncio.sleep(0)


def make_config(**overrides) -> Config:
    values = dict(
        bot_token="test-token",
        ai_api_key="test-key",
        ai_base_url="https://example.invalid/v1",
        default_model="test-model",
        message_debounce=0.02,
        typing_simulation=False,
        short_memory_limit=100,
        database_path=":memory:",
        proactive_enabled=False,
        proactive_stage1_min_minutes=1.0,
        proactive_stage1_max_minutes=1.0,
        proactive_stage2_min_minutes=1.0,
        proactive_stage2_max_minutes=1.0,
        proactive_offense_min_minutes=1.0,
        proactive_offense_max_minutes=1.0,
        proactive_check_interval=0.01,
        morning_start_hour=0,
        morning_end_hour=23,
        morning_min_idle_minutes=999.0,
        admin_ids=frozenset(),
    )
    values.update(overrides)
    return Config(**values)


async def temp_db() -> tuple[aiosqlite.Connection, str]:
    descriptor, path = tempfile.mkstemp(prefix="conversation-regression-", suffix=".db", dir="/tmp/opencode")
    os.close(descriptor)
    db = await init_db(path)
    return db, path


def repositories(db, config):
    settings = UserSettingsRepository(db, config.default_model, config.message_debounce)
    history = HistoryRepository(db)
    memory = MemoryRepository(db)
    pending = PendingMessageRepository(db)
    return settings, history, memory, pending


class QueueAI:
    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    async def chat(self, model, messages, max_tokens=800, temperature=0.9, json_mode=False):
        self.calls.append(messages)
        if not self.replies:
            raise AssertionError("unexpected AI call")
        await asyncio.sleep(0)
        return self.replies.pop(0)


class BlockingThenAI:
    def __init__(self, reply: str):
        self.reply = reply
        self.started = asyncio.Event()
        self.calls: list[list[dict]] = []

    async def chat(self, model, messages, max_tokens=800, temperature=0.9, json_mode=False):
        self.calls.append(messages)
        if len(self.calls) == 1:
            self.started.set()
            await asyncio.Event().wait()
        return self.reply


class ListSender:
    """Old FakeSender-compatible API: no progress_callback keyword."""

    def __init__(self):
        self.sent: list[str] = []

    async def typing_keepalive(self, chat_id):
        await asyncio.Event().wait()

    async def send_messages(
        self, chat_id, user_id, messages, typing_enabled, typing_task=None
    ):
        self.sent.extend(messages)
        return messages


class CancelAfterFirstChunkSender:
    def __init__(self):
        self.sent: list[str] = []
        self.blocked = asyncio.Event()
        self.calls = 0

    async def typing_keepalive(self, chat_id):
        await asyncio.Event().wait()

    async def send_messages(
        self, chat_id, user_id, messages, typing_enabled, typing_task=None,
        progress_callback=None,
    ):
        self.calls += 1
        block = self.calls == 1
        for index, message in enumerate(messages):
            if block and index > 0:
                self.blocked.set()
                await asyncio.Event().wait()
            self.sent.append(message)
            if progress_callback is not None:
                progress_callback(SendProgress(
                    user_id=user_id,
                    chat_id=chat_id,
                    message_index=index,
                    chunk_index=0,
                    chunk=message,
                    message_id=1000 + len(self.sent),
                    confirmed_chunks=tuple(self.sent),
                ))
        return self.sent[-len(messages):]


class ThrowAfterFirstChunkSender:
    async def typing_keepalive(self, chat_id):
        await asyncio.Event().wait()

    async def send_messages(
        self, chat_id, user_id, messages, typing_enabled, typing_task=None,
        progress_callback=None,
    ):
        first = messages[0]
        progress_callback(SendProgress(
            user_id=user_id,
            chat_id=chat_id,
            message_index=0,
            chunk_index=0,
            chunk=first,
            message_id=77,
            confirmed_chunks=(first,),
        ))
        raise TelegramAPIError(method=None, message="synthetic Telegram failure")


async def test_old_database_migration() -> None:
    descriptor, path = tempfile.mkstemp(
        prefix="legacy-conversation-", suffix=".db", dir="/tmp/opencode"
    )
    os.close(descriptor)
    legacy = await aiosqlite.connect(path)
    await legacy.executescript(
        """
        CREATE TABLE user_settings (
            user_id INTEGER PRIMARY KEY,
            selected_model TEXT NOT NULL,
            custom_prompt TEXT NOT NULL DEFAULT '',
            personality TEXT NOT NULL DEFAULT 'default',
            typing_enabled INTEGER NOT NULL DEFAULT 1,
            debounce_seconds REAL NOT NULL DEFAULT 2,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO user_settings
            (user_id, selected_model, created_at, updated_at)
            VALUES (7, 'legacy-model', '2025-01-01T00:00:00', '2025-01-01T00:00:00');
        INSERT INTO history(user_id, role, content, created_at)
            VALUES (7, 'user', 'legacy', '2025-01-01T00:00:00');
        """
    )
    await legacy.commit()
    await legacy.close()

    db = await init_db(path)
    settings = UserSettingsRepository(db, "new-model", 2.0)
    history = HistoryRepository(db)
    pending = PendingMessageRepository(db)
    migrated = await settings.get(7)
    history_columns = {
        row["name"] for row in await (await db.execute("PRAGMA table_info(history)")).fetchall()
    }
    check("migration: legacy user data preserved", migrated.selected_model == "legacy-model")
    check("migration: legacy history preserved", len(await history.get_recent(7, 10)) == 1)
    check("migration: history source_key added", "source_key" in history_columns)
    check("migration: durable pending table available", await pending.count(7) == 0)
    await db.close()
    await init_db(path)  # repeated startup is a no-op
    check("migration: init_db is idempotent", True)
    os.unlink(path)


async def test_new_message_and_clear_reasons() -> None:
    config = make_config()
    db, path = await temp_db()
    settings, history, memory_repo, pending = repositories(db, config)

    ai = BlockingThenAI('{"should_reply": true, "messages": ["новый ответ"]}')
    sender = ListSender()
    memory = MemoryService(ai, history, memory_repo, 20)
    manager = ConversationManager(config, ai, sender, memory, settings, history)
    await manager.handle_message(1, 10, "первое")
    await asyncio.wait_for(ai.started.wait(), 1.0)
    await manager.handle_message(1, 10, "второе")
    await wait_until(lambda: sender.sent == ["новый ответ"])
    incoming = "\n".join(
        message["content"]
        for message in ai.calls[-1]
        if message["role"] == "user"
    )
    check("cancel NEW_MESSAGE: both inputs retained", "первое" in incoming and "второе" in incoming)
    check("cancel NEW_MESSAGE: old generation never sent", sender.sent == ["новый ответ"])
    check("cancel NEW_MESSAGE: durable queue drained", await pending.count(1) == 0)
    await manager.shutdown()

    ai2 = BlockingThenAI('{"should_reply": true, "messages": ["после clear"]}')
    sender2 = ListSender()
    manager2 = ConversationManager(
        config, ai2, sender2, MemoryService(ai2, history, memory_repo, 20),
        settings, history,
    )
    await manager2.handle_message(2, 20, "старое")
    await asyncio.wait_for(ai2.started.wait(), 1.0)
    check("cancel reason is explicit", CancelReason.CLEAR.returns_taken is False)
    await manager2.cancel_active(2)
    await history.clear(2)
    check("cancel CLEAR: pending queue is empty", await pending.count(2) == 0)
    check("cancel CLEAR: runtime buffer is empty", manager2._sessions[2].buffer == [])
    await manager2.handle_message(2, 20, "свежее")
    await wait_until(lambda: sender2.sent == ["после clear"])
    context = "\n".join(
        message["content"] for message in ai2.calls[-1] if message["role"] == "user"
    )
    check("cancel CLEAR: stale input is not resurrected", "старое" not in context and "свежее" in context)
    await manager2.shutdown()
    await db.close()
    os.unlink(path)


async def test_mood_only_invalidation() -> None:
    config = make_config()
    db, path = await temp_db()
    settings, history, memory_repo, pending = repositories(db, config)
    ai = BlockingThenAI('{"should_reply": true, "messages": ["свежий"], "mood": "новое"}')
    sender = ListSender()
    manager = ConversationManager(
        config, ai, sender, MemoryService(ai, history, memory_repo, 20),
        settings, history,
    )

    await manager.handle_message(3, 30, "входящее")
    await asyncio.wait_for(ai.started.wait(), 1.0)
    await settings.update(3, mood="")
    await manager.invalidate_mood_only(3)
    check("mood-only: input is retained", len(manager._sessions[3].buffer) == 1)
    check("mood-only: durable input is retained", await pending.count(3) == 1)
    await manager.handle_message(3, 30, "продолжение")
    await wait_until(lambda: sender.sent == ["свежий"])
    check("mood-only: stale generation cannot restore mood", (await settings.get(3)).mood == "новое")
    check("mood-only: no input loss", await pending.count(3) == 0)
    await manager.shutdown()
    await db.close()
    os.unlink(path)


async def test_partial_send_confirmation() -> None:
    config = make_config()
    db, path = await temp_db()
    settings, history, memory_repo, pending = repositories(db, config)
    ai = QueueAI([
        '{"should_reply": true, "messages": ["старый-1", "старый-2"]}',
        '{"should_reply": true, "messages": ["актуальный"]}',
    ])
    sender = CancelAfterFirstChunkSender()
    manager = ConversationManager(
        config, ai, sender, MemoryService(ai, history, memory_repo, 20),
        settings, history,
    )

    await manager.handle_message(4, 40, "old-turn")
    await asyncio.wait_for(sender.blocked.wait(), 1.0)
    await manager.handle_message(4, 40, "new-turn")
    await wait_until(lambda: "актуальный" in sender.sent)
    records = await history.get_recent(4, 20)
    contents = [record.content for record in records]
    check("partial send: confirmed chunk saved once", contents.count("старый-1") == 1)
    check("partial send: unconfirmed chunk not retried", "старый-2" not in contents)
    check(
        "partial send: turn order is context-safe",
        [record.role for record in records] == ["user", "assistant", "user", "assistant"],
    )
    check("partial send: durable records consumed once", await pending.count(4) == 0)
    await manager.shutdown()
    await db.close()
    os.unlink(path)


async def test_telegram_exception_partial_result() -> None:
    config = make_config()
    db, path = await temp_db()
    settings, history, memory_repo, pending = repositories(db, config)
    ai = QueueAI(['{"should_reply": true, "messages": ["confirmed", "failed"]}'])
    sender = ThrowAfterFirstChunkSender()
    manager = ConversationManager(
        config, ai, sender, MemoryService(ai, history, memory_repo, 20),
        settings, history,
    )
    await manager.handle_message(5, 50, "message")
    await wait_until(lambda: manager._sessions[5].task.done())
    records = await history.get_recent(5, 10)
    check(
        "Telegram exception: confirmed progress persisted",
        [record.content for record in records] == ["message", "confirmed"],
    )
    check("Telegram exception: durable input consumed", await pending.count(5) == 0)
    await manager.shutdown()
    await db.close()
    os.unlink(path)


async def test_memory_generation_and_clear() -> None:
    config = make_config()
    db, path = await temp_db()
    _settings, history, memory_repo, _pending = repositories(db, config)

    class CancellationSuppressingAI:
        def __init__(self):
            self.started = asyncio.Event()

        async def chat(self, **kwargs):
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return '{"facts": ["stale fact"]}'

    ai = CancellationSuppressingAI()
    memory = MemoryService(ai, history, memory_repo, 20)
    memory._exchange_counters[6] = 2
    task = memory.schedule_extraction(6, "model", "u", "a")
    await asyncio.wait_for(ai.started.wait(), 1.0)
    await memory.cancel_user(6)
    check("memory clear: extraction task awaited", task.done())
    check("memory clear: stale version cannot write facts", await memory_repo.get_facts(6) == [])
    check("memory clear: task registry empty", not memory._tasks.get(6))
    await db.close()
    os.unlink(path)


async def test_buffer_context_and_request_limits() -> None:
    config = make_config(
        ai_max_buffer_messages=2,
        ai_max_buffer_chars=1000,
        ai_max_context_chars=500,
        ai_max_requests_per_user_per_hour=10,
    )
    db, path = await temp_db()
    settings, history, memory_repo, pending = repositories(db, config)
    ai = QueueAI([
        '{"should_reply": true, "messages": ["one"]}',
        '{"should_reply": true, "messages": ["two"]}',
    ])
    sender = ListSender()
    manager = ConversationManager(
        config, ai, sender, MemoryService(ai, history, memory_repo, 20),
        settings, history,
    )
    for index in range(3):
        await manager.handle_message(7, 70, f"message-{index}")
    await wait_until(lambda: len(ai.calls) == 2 and len(sender.sent) == 2)
    first_user_count = sum(message["role"] == "user" for message in ai.calls[0])
    second_user_count = sum(message["role"] == "user" for message in ai.calls[1])
    check("buffer limit: max messages per generation", first_user_count == 2 and second_user_count == 1)
    check(
        "context limit: request stays within character cap",
        all(
            sum(len(str(message.get("content", ""))) for message in call) <= 500
            for call in ai.calls
        ),
    )
    check("buffer limit: all inputs survive", await history.count(7) == 5)
    check("buffer limit: queue drained", await pending.count(7) == 0)
    await manager.shutdown()
    await db.close()
    os.unlink(path)

    char_config = replace(
        config, ai_max_buffer_messages=20, ai_max_buffer_chars=8,
        ai_max_context_chars=500, ai_max_requests_per_user_per_hour=10,
    )
    db, path = await temp_db()
    settings, history, memory_repo, pending = repositories(db, char_config)
    ai = QueueAI([
        '{"should_reply": true, "messages": ["char-1"]}',
        '{"should_reply": true, "messages": ["char-2"]}',
        '{"should_reply": true, "messages": ["char-3"]}',
        '{"facts": ["fact"]}',
    ])
    sender = ListSender()
    manager = ConversationManager(
        char_config, ai, sender, MemoryService(ai, history, memory_repo, 20),
        settings, history,
    )
    for index in range(3):
        await manager.handle_message(10, 100, f"char-{index}")
    await wait_until(lambda: len(sender.sent) == 3)
    check(
        "buffer char limit: oversized message is never silently dropped",
        all(
            sum('<message role="user"' in message["content"] for message in call) == 1
            for call in ai.calls[:3]
        )
        and await history.count(10) == 6
        and await pending.count(10) == 0,
    )
    await manager.shutdown()
    await db.close()
    os.unlink(path)

    limited_config = replace(
        config, ai_max_buffer_messages=2, ai_max_context_chars=24_000,
        ai_max_requests_per_user_per_hour=1,
    )
    db, path = await temp_db()
    settings, history, memory_repo, pending = repositories(db, limited_config)
    ai = QueueAI(['{"should_reply": true, "messages": ["allowed"]}'])
    sender = ListSender()
    manager = ConversationManager(
        limited_config, ai, sender, MemoryService(ai, history, memory_repo, 20),
        settings, history,
    )
    for index in range(3):
        await manager.handle_message(8, 80, f"limited-{index}")
    await wait_until(
        lambda: len(ai.calls) == 1
        and any("лимит запросов" in message for message in sender.sent)
    )
    records = await pending.list_pending(8)
    check("request limit: overflow retained durably", [record.content for record in records] == ["limited-2"])
    check("request limit: user notified explicitly", any("лимит запросов" in message for message in sender.sent))
    await manager.shutdown()
    check("request limit: shutdown awaits retry task", manager._sessions[8].rate_limit_retry_task is None)
    check("request limit: shutdown preserves durable retry", await pending.count(8) == 1)
    await db.close()
    os.unlink(path)


async def test_shutdown_restore_and_idempotent_lifecycle() -> None:
    config = make_config(proactive_enabled=True, proactive_check_interval=0.01)
    db, path = await temp_db()
    settings, history, memory_repo, pending = repositories(db, config)

    blocking = BlockingThenAI('{"should_reply": true, "messages": [" restored"]}')
    sender = ListSender()
    manager = ConversationManager(
        config, blocking, sender, MemoryService(blocking, history, memory_repo, 20),
        settings, history,
    )
    await manager.handle_message(9, 90, "pending")
    await asyncio.wait_for(blocking.started.wait(), 1.0)
    await manager.shutdown()
    await manager.shutdown()
    check("shutdown: idempotent", manager._closed is True)
    check("shutdown: active pipeline awaited", manager._sessions[9].task is None)
    check("shutdown: durable turn retained for restart", await pending.count(9) == 1)

    restored_ai = QueueAI(['{"should_reply": true, "messages": ["восстановлено"]}'])
    restored_sender = ListSender()
    restored = ConversationManager(
        config, restored_ai, restored_sender,
        MemoryService(restored_ai, history, memory_repo, 20), settings, history,
    )
    await restored.restore_sessions()
    await wait_until(lambda: restored_sender.sent == ["восстановлено"])
    check("restore: durable pending automatically retried", await pending.count(9) == 0)
    check("restore: replay visible in history", (await history.count(9)) == 2)

    restored.start_proactive_loop()
    task = restored._proactive_task
    restored.start_proactive_loop()
    check("proactive: start is idempotent", restored._proactive_task is task)
    await restored.shutdown()
    check("proactive: shutdown is idempotent", restored._proactive_task is None)
    await db.close()
    os.unlink(path)


async def test_telegram_sender_progress_contract() -> None:
    class Bot:
        def __init__(self):
            self.calls = 0
            self.blocked = asyncio.Event()

        async def send_message(self, chat_id, text):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(message_id=501)
            self.blocked.set()
            await asyncio.Event().wait()

    bot = Bot()
    sender = TelegramSender(bot)
    progress: list[SendProgress] = []
    task = asyncio.create_task(sender.send_messages(
        chat_id=1, user_id=2, messages=["a" * 4097],
        typing_enabled=False, progress_callback=progress.append,
    ))
    await asyncio.wait_for(bot.blocked.wait(), 1.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError("sender cancellation was swallowed")
    check("sender cancellation: callback has confirmed chunk", [item.chunk for item in progress] == ["a" * 4096])
    check("sender cancellation: Telegram message id preserved", progress[0].message_id == 501)

    class FailingBot:
        def __init__(self):
            self.calls = 0

        async def send_message(self, chat_id, text):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(message_id=601)
            raise TelegramAPIError(method=None, message="synthetic")

    progress.clear()
    failing = FailingBot()
    try:
        await TelegramSender(failing).send_messages(
            chat_id=1, user_id=2, messages=["yes", "no"],
            typing_enabled=False, progress_callback=progress.append,
        )
    except TelegramAPIError:
        pass
    else:
        raise AssertionError("Telegram exception was swallowed")
    check("sender Telegram exception: partial progress exposed", [item.chunk for item in progress] == ["yes"])
    check("SendResult is backwards iterable", list(SendResult(("a", "b"), (1, 2))) == ["a", "b"])


async def main() -> None:
    tests = (
        test_old_database_migration,
        test_new_message_and_clear_reasons,
        test_mood_only_invalidation,
        test_partial_send_confirmation,
        test_telegram_exception_partial_result,
        test_memory_generation_and_clear,
        test_buffer_context_and_request_limits,
        test_shutdown_restore_and_idempotent_lifecycle,
        test_telegram_sender_progress_contract,
    )
    for test in tests:
        print(f"\n-- {test.__name__} --")
        await test()
    print(f"\nREGRESSION OK: {len(PASSED)} checks")


if __name__ == "__main__":
    asyncio.run(main())
