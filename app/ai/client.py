"""Асинхронный клиент для gptunnel.ru (OpenAI-совместимый API).

Основан на предоставленном пользователем примере api.py:
- POST {base}/chat/completions
- заголовок Authorization: <ключ> (без "Bearer" — как в примере)
- параметр useWalletBalance: true
- GET {base}/models — список моделей
- GET {base}/balance — баланс

Обработка ошибок (п. 23 ТЗ): timeout, 429, 5xx — с retry и backoff.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.config import Config

logger = logging.getLogger(__name__)


class AIClientError(Exception):
    """Ошибка вызова AI API."""


class AIRateLimitError(AIClientError):
    """429 — превышен лимит запросов."""


class AIClient:
    def __init__(self, config: Config):
        self._config = config
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"Authorization": config.ai_api_key},
        )
        # некоторые модели/провайдеры не поддерживают response_format —
        # при первой 400 на этот параметр отключаем его глобально
        self._json_mode_supported = True

    async def close(self) -> None:
        await self._client.aclose()

    async def chat(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 2000,
        temperature: float = 0.9,
        json_mode: bool = False,
    ) -> str:
        """Один вызов chat/completions. Возвращает текст ответа.

        Лимит токенов с запасом: у reasoning-моделей внутренние рассуждения
        тоже расходуют max_tokens, иначе content может прийти пустым.
        """
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "useWalletBalance": True,
        }
        if json_mode and self._json_mode_supported:
            payload["response_format"] = {"type": "json_object"}

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                response = await self._client.post(
                    self._config.chat_completions_url, json=payload
                )
                if response.status_code == 429:
                    raise AIRateLimitError("API вернул 429 (rate limit)")
                if response.status_code >= 500:
                    raise AIClientError(f"API вернул {response.status_code}")
                if response.status_code >= 400:
                    # модель не поддерживает response_format — отключаем и ретраим без него
                    if "response_format" in payload and "response_format" in response.text:
                        self._json_mode_supported = False
                        payload.pop("response_format")
                        logger.info("API не поддерживает response_format, json_mode отключён")
                        continue
                    # 4xx (кроме 429) не имеет смысла ретраить
                    raise AIClientError(
                        f"API вернул {response.status_code}: {response.text[:200]}"
                    )

                data = response.json()
                content = data["choices"][0]["message"].get("content") or ""
                if not content.strip():
                    # reasoning-модель съела лимит на рассуждения или json_mode
                    # загнал её в пустой ответ — следующая попытка без response_format
                    if "response_format" in payload:
                        payload.pop("response_format")
                        logger.info("Пустой ответ в json_mode, повтор без response_format")
                    raise AIClientError("API вернул пустой ответ")
                return content

            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_error = e
                logger.warning("AI API network error (attempt %d): %s", attempt + 1, type(e).__name__)
            except AIRateLimitError as e:
                last_error = e
                logger.warning("AI API rate limit (attempt %d)", attempt + 1)
            except AIClientError as e:
                last_error = e
                logger.warning("AI API error (attempt %d): %s", attempt + 1, e)
            except (KeyError, ValueError) as e:
                # невалидный JSON / неожиданная структура ответа
                raise AIClientError(f"Некорректный ответ API: {type(e).__name__}") from e

            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))

        raise AIClientError(f"Не удалось получить ответ от API: {last_error}")

    async def list_models(self) -> list[str]:
        """Список доступных текстовых моделей из API (п. 16 ТЗ)."""
        try:
            response = await self._client.get(
                self._config.models_url, params={"useWalletBalance": "true"}
            )
            response.raise_for_status()
            data = response.json()
            return [
                m["id"]
                for m in data.get("data", [])
                if m.get("type") == "TEXT" and not m.get("deprecated")
            ]
        except Exception as e:
            logger.warning("Не удалось получить список моделей: %s", type(e).__name__)
            return []

    async def get_balance(self) -> str:
        try:
            response = await self._client.get(
                self._config.balance_url, params={"useWalletBalance": "true"}
            )
            response.raise_for_status()
            return f"{float(response.json().get('balance', 0)):.2f}"
        except Exception:
            return "неизвестно"
