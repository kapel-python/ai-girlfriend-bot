"""Самопроверка по п. 33 ТЗ. Запуск: python tests/selfcheck.py

Проверяет без реального Telegram и с подменой AI:
- парсер ответов модели (JSON, markdown, fallback, NO_REPLY);
- симулятор набора (короткие ≠ долго, длинные ≠ мгновенно);
- нарезку длинных текстов;
- ConversationManager: debounce-группировку, отмену устаревших ответов,
  решение «не отвечать», сохранение истории;
- репозитории (настройки переживают «перезапуск»).
"""

import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ai.response_parser import parse_response, parse_facts, split_fallback_text
from app.conversation.typing_simulator import calculate_typing_duration
from app.conversation.sender import split_long_text
from app.conversation.manager import ConversationManager
from app.conversation.memory import MemoryService
from app.config import Config
from app.database.database import init_db
from app.database.repository import (
    HistoryRepository,
    MemoryRepository,
    UserSettingsRepository,
)

PASS, FAIL = "✅", "❌"
failures = []


def check(name: str, condition: bool) -> None:
    print(f"{PASS if condition else FAIL} {name}")
    if not condition:
        failures.append(name)


# ---------- 1. Парсер ответов ---------- #

p = parse_response('{"should_reply": true, "messages": ["привет", "как дела?"]}')
check("parser: чистый JSON", p.should_reply and p.messages == ["привет", "как дела?"])

p = parse_response('```json\n{"should_reply": false, "messages": []}\n```')
check("parser: JSON в markdown + молчание", not p.should_reply)

p = parse_response('конечно! вот ответ: {"should_reply": true, "messages": ["ага"]} надеюсь помогло')
check("parser: JSON внутри текста", p.should_reply and p.messages == ["ага"])

p = parse_response("ну не знаю даже, наверное завтра")
check("parser: fallback обычного текста", p.should_reply and len(p.messages) == 1)

p = parse_response("[NO_REPLY]")
check("parser: маркер молчания", not p.should_reply)

p = parse_response('{"should_reply": true, "messages": ["a","b","c","d","e","f","g"]}')
check("parser: лимит количества сообщений", len(p.messages) == 5)

f = parse_facts('{"facts": ["любит кошек", "работает программистом"]}')
check("parser: факты", f == ["любит кошек", "работает программистом"])

p = parse_response('{"should_reply": true, "messages": ["привет"], "mood": "игривое"}')
check("parser: mood извлекается", p.mood == "игривое")

p = parse_response('{"should_reply": true, "messages": ["{\"should_reply\": true, \"messages\": [\"че, я-то?\"]}"], "mood": "ок"}')
check("parser: вложенный JSON разворачивается", p.messages == ["че, я-то?"])

p = parse_response('{"should_reply": true, "messages": ["{"should_reply": true, "messages": ["че, я-то?", "а что случилось?"]}"], "mood": "ок"}')
check("parser: битый вложенный JSON выжимается регэкспом",
      p.messages == ["че, я-то?", "а что случилось?"])

p = parse_response('{"should_reply": false, "messages": [], "mood": "уставшее"}')
check("parser: mood сохраняется при молчании", not p.should_reply and p.mood == "уставшее")

# ---------- 2.1 Fallback-нарезка (улучшение 7) ---------- #

short_fb = split_fallback_text("короткий ответ")
check("fallback: короткий текст не режется", short_fb == ["короткий ответ"])

long_fb = split_fallback_text("Первый абзац, довольно длинный. " * 20 + "\n\n" +
                              "Второй абзац, тоже длинный. " * 20 + "\n\n" +
                              "Третий абзац с текстом. " * 20)
check("fallback: длинный текст режется на 2-3 части", 2 <= len(long_fb) <= 3)
check("fallback: части в пределах лимита", all(len(p) <= 4000 for p in long_fb))
check("fallback: текст не потерян", "Первый" in long_fb[0] and "Третий" in long_fb[-1])

# ---------- 2. Симулятор набора ---------- #

short = [calculate_typing_duration("ага") for _ in range(50)]
check("typing: «ага» < 2 сек", max(short) < 2.0)

long_text = "дааа, я сегодня вообще ничего не делала, просто валялась дома и смотрела сериал, потом готовила пасту и разговаривала с мамой по телефону почти час"
long_ = [calculate_typing_duration(long_text) for _ in range(50)]
check("typing: длинный текст > 5 сек", min(long_) > 5.0)
check("typing: длинный текст < 28 сек (max)", max(long_) <= 28.0)
check("typing: есть вариативность", len({round(t, 2) for t in long_}) > 10)

emoji_text = "привет 😊😊😊 как дела?"
plain_text = "привет      как дела?"
e = sum(calculate_typing_duration(emoji_text) for _ in range(30)) / 30
pl = sum(calculate_typing_duration(plain_text) for _ in range(30)) / 30
check("typing: эмодзи добавляют время", e > pl)

# ---------- 3. Нарезка длинных текстов ---------- #

huge = ("предложение номер раз. " * 300)
parts = split_long_text(huge, limit=500)
check("split: все части <= лимита", all(len(p) <= 500 for p in parts))
check("split: текст не потерян", "".join(parts).replace(" ", "") != "")
check("split: не режет посередине слова без нужды", all(p.endswith(".") or p == parts[-1] for p in parts))

# ---------- 4. ConversationManager (интеграционно) ---------- #


class FakeAI:
    """Подмена AIClient: отдаёт заранее заданные ответы."""

    def __init__(self, replies: list[str], delay: float = 0.05):
        self.replies = list(replies)
        self.delay = delay
        self.calls: list[list[dict]] = []

    async def chat(self, model, messages, max_tokens=800, temperature=0.9, json_mode=False):
        self.calls.append(messages)
        reply = self.replies.pop(0) if self.replies else '{"should_reply": true, "messages": ["ок"]}'
        await asyncio.sleep(self.delay)
        return reply


class FakeSender:
    """Подмена TelegramSender: отправка мгновенная, всё записывается."""

    def __init__(self):
        self.sent: list[str] = []

    async def typing_keepalive(self, chat_id):
        try:
            while True:
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise

    async def send_messages(self, chat_id, user_id, messages, typing_enabled, typing_task=None):
        self.sent.extend(messages)
        return messages


async def manager_tests() -> None:
    cfg = Config(
        bot_token="x", ai_api_key="x", ai_base_url="https://x",
        default_model="test-model", message_debounce=0.3,
        typing_simulation=True, short_memory_limit=20,
        database_path=":memory:",
        proactive_enabled=True,
        proactive_stage1_min_minutes=0.001, proactive_stage1_max_minutes=0.002,
        proactive_stage2_min_minutes=0.001, proactive_stage2_max_minutes=0.002,
        proactive_offense_min_minutes=0.001, proactive_offense_max_minutes=0.002,
        proactive_check_interval=0.2,
    )
    tmp = tempfile.mktemp(suffix=".db")
    db = await init_db(tmp)
    settings_repo = UserSettingsRepository(db, cfg.default_model, cfg.message_debounce)
    history_repo = HistoryRepository(db)
    memory_repo = MemoryRepository(db)

    # --- 4.1 debounce: три быстрых сообщения = один вызов AI --- #
    ai = FakeAI(['{"should_reply": true, "messages": ["привет!"]}'])
    sender = FakeSender()
    memory = MemoryService(ai, history_repo, memory_repo, 20)
    manager = ConversationManager(cfg, ai, sender, memory, settings_repo, history_repo)

    await manager.handle_message(1, 100, "привет")
    await asyncio.sleep(0.1)
    await manager.handle_message(1, 100, "как дела?")
    await asyncio.sleep(0.1)
    await manager.handle_message(1, 100, "что делаешь?")
    await asyncio.sleep(1.0)

    check("manager: 3 быстрых сообщения = 1 вызов AI", len(ai.calls) == 1)
    merged = ai.calls[0][-1]["content"] if ai.calls else ""
    check("manager: сообщения объединены в одну реплику",
          "привет" in merged and "как дела?" in merged and "что делаешь?" in merged)
    check("manager: ответ отправлен", sender.sent == ["привет!"])

    history = await history_repo.get_recent(1, 10)
    check("manager: история сохранена (user + assistant)",
          [m.role for m in history] == ["user", "assistant"])

    # --- 4.2 сообщение во время генерации отменяет устаревший ответ --- #
    ai2 = FakeAI(
        ['{"should_reply": true, "messages": ["УСТАРЕВШИЙ"]}',
         '{"should_reply": true, "messages": ["актуальный"]}'],
        delay=0.5,
    )
    sender2 = FakeSender()
    memory2 = MemoryService(ai2, history_repo, memory_repo, 20)
    manager2 = ConversationManager(cfg, ai2, sender2, memory2, settings_repo, history_repo)

    await manager2.handle_message(2, 200, "первое")
    await asyncio.sleep(0.6)          # debounce прошёл, генерация идёт (0.5 сек)
    await manager2.handle_message(2, 200, "а ты где?")   # отменяет генерацию
    await asyncio.sleep(1.5)

    check("manager: устаревший ответ НЕ отправлен", "УСТАРЕВШИЙ" not in sender2.sent)
    check("manager: актуальный ответ отправлен", "актуальный" in sender2.sent)
    last_ctx = ai2.calls[-1][-1]["content"] if ai2.calls else ""
    check("manager: новая генерация видит оба сообщения",
          "первое" in last_ctx and "а ты где?" in last_ctx)

    # --- 4.3 модель решила не отвечать --- #
    ai3 = FakeAI(['{"should_reply": false, "messages": []}'])
    sender3 = FakeSender()
    memory3 = MemoryService(ai3, history_repo, memory_repo, 20)
    manager3 = ConversationManager(cfg, ai3, sender3, memory3, settings_repo, history_repo)

    await manager3.handle_message(3, 300, "ок")
    await asyncio.sleep(1.0)
    check("manager: should_reply=false → ничего не отправлено", sender3.sent == [])
    h3 = await history_repo.get_recent(3, 10)
    check("manager: реплика пользователя всё равно в истории",
          len(h3) == 1 and h3[0].role == "user")

    # --- 4.4 ошибка AI не роняет pipeline --- #
    class BrokenAI(FakeAI):
        async def chat(self, model, messages, max_tokens=800, temperature=0.9, json_mode=False):
            from app.ai.client import AIClientError
            raise AIClientError("boom")

    ai4 = BrokenAI([])
    manager4 = ConversationManager(cfg, ai4, FakeSender(),
                                   MemoryService(ai4, history_repo, memory_repo, 20),
                                   settings_repo, history_repo)
    await manager4.handle_message(4, 400, "привет")
    await asyncio.sleep(1.0)
    check("manager: ошибка API обработана, менеджер жив", True)

    # --- 4.5 настройки переживают «перезапуск» --- #
    await settings_repo.update(5, selected_model="gpt-5-mini", custom_prompt="будь милой")
    settings_repo2 = UserSettingsRepository(db, cfg.default_model, cfg.message_debounce)
    s = await settings_repo2.get(5)
    check("db: настройки сохраняются", s.selected_model == "gpt-5-mini" and s.custom_prompt == "будь милой")

    # --- 4.6 настроение сохраняется из ответа модели (улучшение 5) --- #
    ai6 = FakeAI(['{"should_reply": true, "messages": ["хи"], "mood": "игривое"}'])
    manager6 = ConversationManager(cfg, ai6, FakeSender(),
                                   MemoryService(ai6, history_repo, memory_repo, 20),
                                   settings_repo, history_repo)
    await manager6.handle_message(6, 600, "привет")
    await asyncio.sleep(1.0)
    s6 = await settings_repo.get(6)
    check("mood: сохраняется в настройках", s6.mood == "игривое")

    # --- 4.7 проактивность: стадии эскалации (ты куда пропал → последняя попытка → обида) --- #
    ai7 = FakeAI(['{"should_reply": true, "messages": ["ок"]}',                        # обычный ответ
                  '{"should_reply": true, "messages": ["ты куда пропал?"]}',          # стадия 1
                  '{"should_reply": true, "messages": ["ну ладно, молчи дальше"]}'])  # стадия 2
    sender7 = FakeSender()
    manager7 = ConversationManager(cfg, ai7, sender7,
                                   MemoryService(ai7, history_repo, memory_repo, 20),
                                   settings_repo, history_repo)
    await manager7.handle_message(7, 700, "привет")
    await asyncio.sleep(1.0)
    check("proactive: обычный ответ отправлен", sender7.sent == ["ок"])

    manager7.start_proactive_loop()
    await asyncio.sleep(1.5)
    check("proactive: стадия 1 — «ты куда пропал?»", "ты куда пропал?" in sender7.sent)

    await asyncio.sleep(1.5)
    check("proactive: стадия 2 — последняя попытка", "ну ладно, молчи дальше" in sender7.sent)

    await asyncio.sleep(1.5)
    s7 = await settings_repo.get(7)
    check("proactive: после двух игноров настроение «недовольная»",
          "недовольная" in s7.mood)
    check("proactive: стадия 3 сохранена в БД", s7.proactive_stage == 3)
    count7 = len(sender7.sent)
    await asyncio.sleep(1.0)
    check("proactive: стадия 3 — больше не пишет", len(sender7.sent) == count7)

    # пользователь вернулся — стадия сбрасывается, настроение остаётся
    ai7.replies.append('{"should_reply": true, "messages": ["и что молчал?"], "mood": "недовольная"}')
    await manager7.handle_message(7, 700, "прости, был занят")
    # стадия сбрасывается синхронно в handle_message — проверяем сразу,
    # пока тестовый цикл с микро-окнами не успел эскалировать заново
    s7b = await settings_repo.get(7)
    check("proactive: возвращение сбрасывает стадию", s7b.proactive_stage == 0)
    await asyncio.sleep(1.0)
    check("proactive: ответ уже в обиженном тоне (mood в контексте)",
          "и что молчал?" in sender7.sent)

    # --- 4.7.1 уведомление о лимите памяти (один раз при пересечении) --- #
    cfg_mem = Config(
        bot_token="x", ai_api_key="x", ai_base_url="https://x",
        default_model="test-model", message_debounce=0.3,
        typing_simulation=True, short_memory_limit=3,
        database_path=":memory:",
        proactive_enabled=False,
        proactive_stage1_min_minutes=999, proactive_stage1_max_minutes=999,
        proactive_stage2_min_minutes=999, proactive_stage2_max_minutes=999,
        proactive_offense_min_minutes=999, proactive_offense_max_minutes=999,
        proactive_check_interval=999,
    )
    ai9 = FakeAI([])
    sender9 = FakeSender()
    manager9 = ConversationManager(cfg_mem, ai9, sender9,
                                   MemoryService(ai9, history_repo, memory_repo, 3),
                                   settings_repo, history_repo)
    # первый обмен: 2 сообщения в истории (лимит 3 не достигнут)
    await manager9.handle_message(9, 900, "первое")
    await asyncio.sleep(1.0)
    check("memory: до лимита уведомления нет",
          not any("забывать" in m for m in sender9.sent))
    # второй обмен: 4 сообщения > лимит 3 → уведомление после ответа
    await manager9.handle_message(9, 900, "второе")
    await asyncio.sleep(1.0)
    check("memory: при пересечении лимита пришло уведомление",
          any("забывать" in m for m in sender9.sent))
    # третий обмен: уже выше лимита → повторного уведомления нет
    count9 = len(sender9.sent)
    await manager9.handle_message(9, 900, "третье")
    await asyncio.sleep(1.0)
    check("memory: повторного уведомления нет",
          len([m for m in sender9.sent if "забывать" in m]) == 1)

    # --- 4.8 проактивность: модель может решить молчать (попытка не засчитывается) --- #
    ai8 = FakeAI(['{"should_reply": true, "messages": ["ок"]}',
                  '{"should_reply": false, "messages": [], "mood": "уставшее"}'])
    sender8 = FakeSender()
    manager8 = ConversationManager(cfg, ai8, sender8,
                                   MemoryService(ai8, history_repo, memory_repo, 20),
                                   settings_repo, history_repo)
    await manager8.handle_message(8, 800, "привет")
    await asyncio.sleep(1.0)
    manager8.start_proactive_loop()
    await asyncio.sleep(1.5)
    s8 = await settings_repo.get(8)
    check("proactive: should_reply=false → не пишет первой", sender8.sent == ["ок"])
    check("proactive: пропущенная попытка не эскалирует стадию", s8.proactive_stage == 0)

    await manager.shutdown()
    await manager2.shutdown()
    await manager3.shutdown()
    await manager4.shutdown()
    await db.close()
    os.unlink(tmp)


asyncio.run(manager_tests())

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
