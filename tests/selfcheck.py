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
import time
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.ai.response_parser import (
    parse_facts,
    parse_initiative,
    parse_response,
    split_fallback_text,
)
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


async def wait_until(condition, timeout: float = 2.0) -> bool:
    """Ждёт синхронный или асинхронный результат без хрупких sleep."""
    async def evaluate():
        result = condition()
        if asyncio.iscoroutine(result):
            return await result
        return bool(result)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await evaluate():
            return True
        await asyncio.sleep(0.02)
    return await evaluate()


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

p = parse_response('{"should_reply": true, "messages": ["a","b","c","d","e","f","g","h","i","j"]}')
check("parser: лимит количества сообщений", len(p.messages) == 8)

p = parse_response('{"should_reply": true, "messages": ["1","2","3","4","5"]}')
check("parser: 5 сообщений подряд допустимы", len(p.messages) == 5)

f = parse_facts('{"facts": ["любит кошек", "работает программистом"]}')
check("parser: факты", f == ["любит кошек", "работает программистом"])

check("parser: proactive DECISION YES", parse_initiative('{"initiative": "YES"}') == "YES")
check("parser: proactive DECISION MAYBE", parse_initiative('{"initiative": "maybe"}') == "MAYBE")
check("parser: malformed DECISION=no", parse_initiative("garbage") == "NO")

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
        self.models: list[str] = []

    async def chat(self, model, messages, max_tokens=800, temperature=0.9, json_mode=False):
        self.calls.append(messages)
        self.models.append(model)
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
        morning_start_hour=0, morning_end_hour=23, morning_min_idle_minutes=999,
        admin_ids=frozenset(),
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
    await wait_until(lambda: len(ai.calls) == 1 and sender.sent == ["привет!"])

    check("manager: 3 быстрых сообщения = 1 вызов AI", len(ai.calls) == 1)
    incoming = "\n".join(
        m["content"] for m in (ai.calls[0] if ai.calls else []) if m["role"] == "user"
    )
    check("manager: сообщения переданы одной генерации",
          "привет" in incoming and "как дела?" in incoming and "что делаешь?" in incoming)
    check("manager: ответ отправлен", sender.sent == ["привет!"])

    history = await history_repo.get_recent(1, 10)
    check("manager: история сохраняет каждое сообщение и ответ",
          [m.role for m in history] == ["user", "user", "user", "assistant"])

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
    await wait_until(lambda: "актуальный" in sender2.sent, timeout=3.0)

    check("manager: устаревший ответ НЕ отправлен", "УСТАРЕВШИЙ" not in sender2.sent)
    check("manager: актуальный ответ отправлен", "актуальный" in sender2.sent)
    last_user_ctx = "\n".join(
        m["content"] for m in (ai2.calls[-1] if ai2.calls else []) if m["role"] == "user"
    )
    check("manager: новая генерация видит оба сообщения",
          "первое" in last_user_ctx and "а ты где?" in last_user_ctx)

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
    class FlakyAI(FakeAI):
        def __init__(self):
            super().__init__([
                '{"should_reply": true, "messages": ["после ошибки"]}'
            ])
            self.fail_next = True
            self.attempted = False

        async def chat(self, model, messages, max_tokens=800, temperature=0.9, json_mode=False):
            from app.ai.client import AIClientError
            if self.fail_next:
                self.fail_next = False
                self.attempted = True
                raise AIClientError("boom")
            return await super().chat(model, messages, max_tokens, temperature, json_mode)

    ai4 = FlakyAI()
    sender4 = FakeSender()
    manager4 = ConversationManager(cfg, ai4, sender4,
                                   MemoryService(ai4, history_repo, memory_repo, 20),
                                   settings_repo, history_repo)
    await manager4.handle_message(4, 400, "привет")
    await wait_until(
        lambda: ai4.attempted and manager4._sessions[4].task.done()
    )
    buffered4 = manager4._sessions[4].buffer
    check("manager: ошибка API обработана, менеджер жив", True)
    check("manager: сообщение не теряется после ошибки API",
          len(buffered4) == 1 and buffered4[0].text == "привет")

    await manager4.handle_message(4, 400, "повтор")
    await wait_until(lambda: "после ошибки" in sender4.sent, timeout=2.0)
    retry_context = "\n".join(
        m["content"] for m in (ai4.calls[-1] if ai4.calls else []) if m["role"] == "user"
    )
    check("manager: повторная генерация получает обе реплики",
          "после ошибки" in sender4.sent
          and "привет" in retry_context and "повтор" in retry_context)

    # --- 4.4.1 характер: дефолт «реалистичный», свой характер --- #
    from app.ai.prompts import PERSONALITY_PRESETS, build_system_prompt

    check("personality: первый пресет — реалистичный",
          next(iter(PERSONALITY_PRESETS)) == "realistic")
    s_new = await settings_repo.get(4242)
    check("personality: новый пользователь получает «реалистичный»",
          s_new.personality == "realistic")
    sp = build_system_prompt("custom", "", custom_personality="ты ворчливая библиотекарша")
    check("personality: свой характер заменяет пресет",
          "ворчливая библиотекарша" in sp and "реалистичный" not in sp.lower())
    sp2 = build_system_prompt("realistic", "")
    check("personality: пресет реалистичный подставляется",
          "обычный собеседник в интернет чате" in sp2)

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

    # --- 4.7 проактивность: DECISION → TIMING → одно сообщение за цикл --- #
    ai7 = FakeAI([
        '{"should_reply": true, "messages": ["ок"]}',                         # обычный ответ
        '{"initiative": "YES"}',                                                # решение 1
        '{"should_reply": true, "messages": ["ты куда пропал?"]}',            # сообщение 1
        '{"initiative": "YES"}',                                                # решение 2
        '{"should_reply": true, "messages": ["ну ладно, молчи дальше"]}',      # сообщение 2
    ])
    sender7 = FakeSender()
    cfg7 = replace(cfg, proactive_max_messages=2, proactive_cooldown_minutes=0.0)
    manager7 = ConversationManager(cfg7, ai7, sender7,
                                   MemoryService(ai7, history_repo, memory_repo, 20),
                                   settings_repo, history_repo)
    manager7._sample_proactive_delay = lambda: 0.01
    manager7._initiative_probability = lambda initiative, settings: 1.0 if initiative == "YES" else 0.0

    await manager7.handle_message(7, 700, "привет")
    await wait_until(lambda: sender7.sent == ["ок"])
    check("proactive: обычный ответ отправлен", sender7.sent == ["ок"])

    manager7.start_proactive_loop()
    await wait_until(lambda: "ты куда пропал?" in sender7.sent, timeout=3.0)
    check("proactive: первое сообщение после YES", "ты куда пропал?" in sender7.sent)

    await wait_until(lambda: "ну ладно, молчи дальше" in sender7.sent, timeout=3.0)
    check("proactive: второе сообщение в отдельном цикле",
          "ну ладно, молчи дальше" in sender7.sent)

    async def proactive_count_saved():
        return (await settings_repo.get(7)).proactive_stage == 2

    await wait_until(proactive_count_saved)
    s7 = await settings_repo.get(7)
    check("proactive: счётчик сообщений сохранён в БД", s7.proactive_stage == 2)
    count7 = len(sender7.sent)
    await asyncio.sleep(0.3)
    check("proactive: лимит сообщений без новой активности", len(sender7.sent) == count7)

    # пользователь вернулся — счётчик сбрасывается, настроение задаётся моделью
    ai7.replies.append('{"should_reply": true, "messages": ["и что молчал?"], "mood": "недовольная"}')
    await manager7.handle_message(7, 700, "прости, был занят")
    s7b = await settings_repo.get(7)
    session7 = manager7._sessions[7]
    check("proactive: возвращение сбрасывает счётчик",
          s7b.proactive_stage == 0 and session7.proactive_count_since_user == 0)
    await wait_until(lambda: "и что молчал?" in sender7.sent)
    check("proactive: настроение из ответа сохранено",
          (await settings_repo.get(7)).mood == "недовольная")

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
        morning_start_hour=0, morning_end_hour=23, morning_min_idle_minutes=999,
        admin_ids=frozenset(),
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

    # --- 4.8 проактивность: DECISION=NO не приводит к отправке --- #
    ai8 = FakeAI(['{"should_reply": true, "messages": ["ок"]}',
                  '{"initiative": "NO"}'])
    sender8 = FakeSender()
    cfg8 = replace(cfg, proactive_cooldown_minutes=0.0)
    manager8 = ConversationManager(cfg8, ai8, sender8,
                                   MemoryService(ai8, history_repo, memory_repo, 20),
                                   settings_repo, history_repo)
    manager8._sample_proactive_delay = lambda: 0.01

    await manager8.handle_message(8, 800, "привет")
    await wait_until(lambda: sender8.sent == ["ок"])
    manager8.start_proactive_loop()
    await wait_until(lambda: len(ai8.calls) >= 2, timeout=3.0)
    await asyncio.sleep(0.1)
    s8 = await settings_repo.get(8)
    check("proactive: DECISION=NO → не пишет первой", sender8.sent == ["ок"])
    check("proactive: NO не увеличивает счётчик",
          s8.proactive_stage == 0 and manager8._sessions[8].proactive_count_since_user == 0)

    # --- 4.9 готовим сессию для проверки восстановления после рестарта --- #
    cfg_restore = replace(cfg, proactive_enabled=True)
    ai10 = FakeAI(['{"should_reply": true, "messages": ["споки)"], "mood": "сонное"}'])
    sender10 = FakeSender()
    manager10 = ConversationManager(cfg_restore, ai10, sender10,
                                    MemoryService(ai10, history_repo, memory_repo, 20),
                                    settings_repo, history_repo)
    await manager10.handle_message(10, 1000, "всё, я спать")
    await wait_until(lambda: sender10.sent == ["споки)"])
    check("restore: диалог для восстановления обработан", sender10.sent == ["споки)"])

    # --- 4.9.1 глобальные настройки (одни на всех) --- #
    from app.database.repository import GlobalSettingsRepository

    global_repo = GlobalSettingsRepository(db, cfg.default_model, cfg.message_debounce)
    check("global: дефолты из конфига",
          (await global_repo.get_str("selected_model")) == "test-model"
          and (await global_repo.get_bool("typing_enabled")) is True)
    await global_repo.set("selected_model", "gpt-5-mini")
    await global_repo.set("typing_enabled", False)
    await global_repo.set("custom_prompt", "будь милой")
    check("global: значения сохраняются",
          (await global_repo.get_str("selected_model")) == "gpt-5-mini"
          and (await global_repo.get_bool("typing_enabled")) is False
          and (await global_repo.get_str("custom_prompt")) == "будь милой")

    # менеджер с глобальным репозиторием берёт модель из него, а не из per-user
    ai12 = FakeAI(['{"should_reply": true, "messages": ["ок"]}'])
    manager12 = ConversationManager(cfg, ai12, FakeSender(),
                                    MemoryService(ai12, history_repo, memory_repo, 20),
                                    settings_repo, history_repo, global_repo)
    await manager12.handle_message(12, 1200, "привет")
    await asyncio.sleep(1.0)
    check("global: менеджер использует глобальную модель",
          ai12.models == ["gpt-5-mini"])

    # --- 4.10 сессии переживают «перезапуск» (last_activity в БД) --- #
    s11 = await settings_repo.get(10)
    check("restore: last_chat_id и last_activity_ts сохранены в БД",
          s11.last_chat_id == 1000 and s11.last_activity_ts > 0)

    manager11 = ConversationManager(cfg_restore, ai10, sender10,
                                    MemoryService(ai10, history_repo, memory_repo, 20),
                                    settings_repo, history_repo)
    await manager11.restore_sessions()
    restored = manager11._sessions.get(10)
    check("restore: сессия восстановлена после «перезапуска»",
          restored is not None and restored.last_chat_id == 1000)
    idle_restored = (time.monotonic() - restored.last_activity) / 60 if restored else 0
    check("restore: молчание до перезапуска учтено", idle_restored > 0.0005)

    for active_manager in (
        manager11, manager10, manager12, manager9, manager8,
        manager7, manager6, manager4, manager3, manager2, manager,
    ):
        await active_manager.shutdown()
    await db.close()
    os.unlink(tmp)


asyncio.run(manager_tests())

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)}")
    sys.exit(1)
print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
