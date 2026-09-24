"""Асинхронный клиент OpenAI-совместимого AI API.

Клиент намеренно не включает тело ответа провайдера в ошибки или журналы: ответ
может содержать пользовательский текст, prompt или другие секреты.  Повторяются
только сетевые ошибки и явно временные HTTP-статусы.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.config import Config

logger = logging.getLogger(__name__)


class AIClientError(Exception):
    """Безопасная структурированная ошибка AI-клиента.

    ``message`` никогда не строится из provider body или ``str`` исключения
    транспорта.  Поля ошибки предназначены для метрик, логов и вызывающего кода.
    """

    def __init__(
        self,
        message: str = "AI API request failed",
        *,
        code: str = "ai_error",
        status_code: int | None = None,
        retryable: bool = False,
        retry_after: float | None = None,
        attempts: int = 0,
    ) -> None:
        super().__init__(message)
        self.code = code
        # Более длинное имя оставлено для удобства внешних callers.
        self.error_code = code
        self.category = code
        self.status_code = status_code
        self.http_status = status_code
        self.status = status_code
        self.kind = code
        self.reason = code
        self.retryable = bool(retryable)
        self.retry_after = retry_after
        self.attempts = int(attempts)

    def to_dict(self) -> dict[str, Any]:
        """Return a log/metrics-safe representation."""

        return {
            "code": self.code,
            "status_code": self.status_code,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "attempts": self.attempts,
        }

    as_dict = to_dict


class AIRateLimitError(AIClientError):
    """Ограничение частоты запросов (HTTP 429 или локальный лимит)."""


@dataclass
class _AttemptFailure(Exception):
    error: AIClientError
    retryable: bool
    retry_after: float | None = None


class AIClient:
    """Small, bounded async wrapper around the provider API."""

    # These are instance attributes too, so a test or an embedding application
    # can tighten them without changing global process state.
    DEFAULT_MAX_ATTEMPTS = 3
    DEFAULT_DEADLINE_SECONDS = 60.0
    DEFAULT_BACKOFF_SECONDS = 0.5
    DEFAULT_MAX_BACKOFF_SECONDS = 8.0
    DEFAULT_MAX_RETRY_AFTER_SECONDS = 30.0

    def __init__(
        self,
        config: Config,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    ) -> None:
        self._config = config
        concurrency = getattr(config, "ai_max_concurrency", 4)
        try:
            concurrency = int(concurrency)
        except (TypeError, ValueError):
            concurrency = 4
        self._semaphore = asyncio.Semaphore(max(1, concurrency))

        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            raise ValueError("max_attempts должен быть положительным целым числом")
        try:
            deadline_seconds = float(deadline_seconds)
        except (TypeError, ValueError):
            raise ValueError("deadline_seconds должен быть конечным положительным числом") from None
        if not math.isfinite(deadline_seconds) or deadline_seconds <= 0:
            raise ValueError("deadline_seconds должен быть конечным положительным числом")
        # A caller may set a smaller deadline, but cannot accidentally remove
        # the total bound with an enormous value.
        self._max_attempts = max_attempts
        self._deadline_seconds = min(deadline_seconds, 3600.0)
        self._backoff_seconds = self.DEFAULT_BACKOFF_SECONDS
        self._max_backoff_seconds = self.DEFAULT_MAX_BACKOFF_SECONDS
        self._max_retry_after_seconds = self.DEFAULT_MAX_RETRY_AFTER_SECONDS

        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(60.0, connect=10.0),
            headers={"Authorization": config.ai_api_key},
            transport=transport,
        )

        # Capability is tracked per model.  The old single boolean is retained
        # as a compatibility attribute for code that inspected it, but never
        # used to decide whether a particular model gets response_format.
        self._json_mode_supported = True
        self._json_mode_capabilities: dict[str, bool] = {}
        self._closed = False

        self._request_times: dict[object, deque[float]] = {}
        self._rate_lock = asyncio.Lock()

    @property
    def semaphore(self) -> asyncio.Semaphore:
        return self._semaphore

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._client.aclose()

    async def __aenter__(self) -> "AIClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    # ------------------------------------------------------------------ #
    # Public model capability helpers
    # ------------------------------------------------------------------ #

    def supports_json_mode(self, model: str) -> bool:
        """Return the known response_format capability for *model*.

        Unknown models are optimistic: the provider remains the source of truth
        and a permanent 400/422 response can disable the capability for that
        model only.
        """

        return self._json_mode_capabilities.get(model, True)

    def set_json_mode_capability(self, model: str, supported: bool) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model должен быть непустой строкой")
        self._json_mode_capabilities[model] = bool(supported)
        # Never let the compatibility bit claim that all models support JSON if
        # at least one known model does not.
        self._json_mode_supported = all(self._json_mode_capabilities.values())

    def get_json_mode_capability(self, model: str) -> bool:
        return self.supports_json_mode(model)

    # ------------------------------------------------------------------ #
    # Chat API
    # ------------------------------------------------------------------ #

    async def chat(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 2000,
        temperature: float = 0.9,
        json_mode: bool = False,
        *,
        user_id: object | None = None,
        deadline_seconds: float | None = None,
    ) -> str:
        """Call ``chat/completions`` and return a validated text content.

        ``user_id`` and ``deadline_seconds`` are optional extensions.  All
        existing positional calls used by the application and fake AI objects
        remain valid.
        """

        model = self._validate_model(model)
        prepared_messages = self._prepare_messages(messages)
        max_tokens = self._validate_max_tokens(max_tokens)
        temperature = self._validate_temperature(temperature)

        deadline = self._make_deadline(deadline_seconds)
        if user_id is not None:
            await self._reserve_user_request(user_id, deadline)

        payload: dict[str, Any] = {
            "model": model,
            "messages": prepared_messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "useWalletBalance": True,
        }
        use_json_mode = bool(json_mode) and self.supports_json_mode(model)
        if use_json_mode:
            payload["response_format"] = {"type": "json_object"}

        response = await self._request_with_retries(
            "POST",
            self._config.chat_completions_url,
            deadline=deadline,
            json_payload=payload,
        )
        status = self._response_status(response)
        if status in {400, 422} and "response_format" in payload:
            # A provider may reject only response_format while accepting the
            # same model/request without it. Disable capability for this model
            # and make one clean non-JSON request; this is a compatibility
            # fallback, not a retry of the failed request.
            self.set_json_mode_capability(model, False)
            payload.pop("response_format", None)
            response = await self._request_with_retries(
                "POST",
                self._config.chat_completions_url,
                deadline=deadline,
                json_payload=payload,
            )
            status = self._response_status(response)
        if status < 200 or status >= 300:
            self._raise_http_error(status, attempts=1)

        data = self._decode_json_object(response)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise self._invalid_response_error("choices")
        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            raise self._invalid_response_error("choice")
        message = first_choice.get("message")
        if not isinstance(message, dict):
            raise self._invalid_response_error("message")
        content = message.get("content")
        if not isinstance(content, str) or not content.strip():
            raise self._invalid_response_error("content")
        return content

    # ------------------------------------------------------------------ #
    # Models and balance
    # ------------------------------------------------------------------ #

    async def list_models(self) -> list[str]:
        """Return verified text model IDs, or an empty list on API failure."""

        try:
            response = await self._request_with_retries(
                "GET",
                self._config.models_url,
                deadline=self._make_deadline(None),
                params={"useWalletBalance": "true"},
            )
            status = self._response_status(response)
            if status < 200 or status >= 300:
                logger.warning("event=ai_models_failed status=%s", status)
                return []

            data = self._decode_json_object(response)
            raw_models = data.get("data")
            if not isinstance(raw_models, list):
                raise self._invalid_response_error("models")
            models: list[str] = []
            seen: set[str] = set()
            for item in raw_models:
                if not isinstance(item, dict):
                    continue
                model_id = item.get("id")
                if not isinstance(model_id, str) or not model_id.strip():
                    continue
                model_id = model_id.strip()
                model_type = item.get("type")
                if model_type not in (None, "TEXT") or item.get("deprecated"):
                    continue
                for capability_key in (
                    "supports_json_mode",
                    "json_mode_supported",
                    "supports_response_format",
                ):
                    capability = item.get(capability_key)
                    if isinstance(capability, bool):
                        self.set_json_mode_capability(model_id, capability)
                        break
                if model_id not in seen:
                    seen.add(model_id)
                    models.append(model_id)
            return models
        except AIClientError as exc:
            logger.warning(
                "event=ai_models_failed code=%s status=%s",
                exc.code,
                exc.status_code,
            )
            return []
        except Exception as exc:  # defensive boundary; never log its text
            logger.warning("event=ai_models_failed error_type=%s", type(exc).__name__)
            return []

    async def get_balance(self) -> str:
        try:
            response = await self._request_with_retries(
                "GET",
                self._config.balance_url,
                deadline=self._make_deadline(None),
                params={"useWalletBalance": "true"},
            )
            status = self._response_status(response)
            if status < 200 or status >= 300:
                return "неизвестно"
            data = self._decode_json_object(response)
            balance = data.get("balance")
            if isinstance(balance, bool):
                return "неизвестно"
            value = float(balance)
            if not math.isfinite(value):
                return "неизвестно"
            return f"{value:.2f}"
        except AIClientError as exc:
            logger.warning(
                "event=ai_balance_failed code=%s status=%s",
                exc.code,
                exc.status_code,
            )
            return "неизвестно"
        except Exception as exc:
            logger.warning("event=ai_balance_failed error_type=%s", type(exc).__name__)
            return "неизвестно"

    # ------------------------------------------------------------------ #
    # Request/retry implementation
    # ------------------------------------------------------------------ #

    async def _request_with_retries(
        self,
        method: str,
        url: str,
        *,
        deadline: float,
        json_payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        last_failure: _AttemptFailure | None = None

        for attempt in range(1, self._max_attempts + 1):
            if self._remaining(deadline) <= 0:
                raise self._deadline_error(last_failure, attempt - 1)

            try:
                response = await self._perform_request(
                    method,
                    url,
                    deadline=deadline,
                    json_payload=json_payload,
                    params=params,
                )
            except _AttemptFailure as failure:
                if not failure.retryable:
                    failure.error.attempts = attempt
                    raise failure.error from None
                last_failure = failure
            else:
                status = self._response_status(response)
                if not self._is_retryable_status(status):
                    return response
                last_failure = _AttemptFailure(
                    error=self._http_error(status),
                    retryable=True,
                    retry_after=self._retry_after(response),
                )

            if attempt >= self._max_attempts:
                break

            delay = self._retry_delay(attempt, last_failure.retry_after if last_failure else None)
            remaining = self._remaining(deadline)
            if remaining <= 0:
                raise self._deadline_error(last_failure, attempt)
            if delay >= remaining:
                # Waiting until the deadline cannot lead to another useful
                # attempt.  Keep the operation bounded even for a hostile
                # Retry-After value.
                await asyncio.sleep(remaining)
                raise self._deadline_error(last_failure, attempt)
            if delay > 0:
                await asyncio.sleep(delay)
            logger.warning(
                "event=ai_api_retry attempt=%d/%d code=%s status=%s",
                attempt,
                self._max_attempts,
                last_failure.error.code if last_failure else "unknown",
                last_failure.error.status_code if last_failure else None,
            )

        if last_failure is None:  # pragma: no cover - defensive
            raise AIClientError("AI API request failed", code="ai_error")
        last_failure.error.attempts = self._max_attempts
        if last_failure.retry_after is not None:
            last_failure.error.retry_after = last_failure.retry_after
        raise last_failure.error from None

    async def _perform_request(
        self,
        method: str,
        url: str,
        *,
        deadline: float,
        json_payload: dict[str, Any] | None,
        params: dict[str, Any] | None,
    ) -> httpx.Response:
        remaining = self._remaining(deadline)
        if remaining <= 0:
            raise _AttemptFailure(
                self._deadline_error(None, 0),
                retryable=False,
            )

        acquired = False
        try:
            try:
                await asyncio.wait_for(self._semaphore.acquire(), timeout=remaining)
                acquired = True
            except asyncio.TimeoutError:
                raise _AttemptFailure(
                    self._deadline_error(None, 0),
                    retryable=False,
                ) from None

            post_timeout = self._remaining(deadline)
            if post_timeout <= 0:
                raise _AttemptFailure(
                    self._deadline_error(None, 0),
                    retryable=False,
                )

            if method == "POST":
                request_coro = self._client.post(url, json=json_payload)
            else:
                request_coro = self._client.get(url, params=params)
            try:
                response = await asyncio.wait_for(request_coro, timeout=post_timeout)
            except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
                error = AIClientError(
                    "AI API transport request failed",
                    code="transport_error",
                    retryable=True,
                )
                # Never include str(exc): some HTTPX exceptions contain a URL
                # or request representation supplied by a custom transport.
                logger.warning(
                    "event=ai_api_transport_error error_type=%s",
                    type(exc).__name__,
                )
                raise _AttemptFailure(error, retryable=True) from None
            except asyncio.TimeoutError:
                # A transport/test double may surface a timeout as
                # asyncio.TimeoutError rather than httpx.TimeoutException.  It
                # is retryable unless the operation's total deadline has
                # actually elapsed.
                if self._remaining(deadline) <= 0:
                    raise _AttemptFailure(
                        self._deadline_error(None, 0),
                        retryable=False,
                    ) from None
                error = AIClientError(
                    "AI API transport request timed out",
                    code="transport_error",
                    retryable=True,
                )
                raise _AttemptFailure(error, retryable=True) from None
            except Exception as exc:
                # An arbitrary client-side exception is not assumed to be a
                # transport failure and therefore is not retried.
                error = AIClientError(
                    "AI API client request failed",
                    code="client_error",
                    retryable=False,
                )
                logger.warning(
                    "event=ai_api_client_error error_type=%s",
                    type(exc).__name__,
                )
                raise _AttemptFailure(error, retryable=False) from None

            if not isinstance(response, httpx.Response):
                error = AIClientError(
                    "AI API returned an invalid response object",
                    code="invalid_response",
                    retryable=False,
                )
                raise _AttemptFailure(error, retryable=False)
            return response
        finally:
            if acquired:
                self._semaphore.release()

    @staticmethod
    def _is_retryable_status(status: int) -> bool:
        return status in {408, 429} or 500 <= status <= 599

    @staticmethod
    def _response_status(response: httpx.Response) -> int:
        status = response.status_code
        if isinstance(status, bool) or not isinstance(status, int):
            raise AIClientError(
                "AI API returned an invalid HTTP status",
                code="invalid_response",
                retryable=False,
            )
        return status

    @staticmethod
    def _http_error(status: int) -> AIClientError:
        if status == 429:
            return AIRateLimitError(
                "AI API rate limit exceeded",
                code="rate_limit",
                status_code=status,
                retryable=True,
            )
        if status == 408:
            return AIClientError(
                "AI API request timed out",
                code="request_timeout",
                status_code=status,
                retryable=True,
            )
        return AIClientError(
            f"AI API server error (status={status})",
            code="server_error",
            status_code=status,
            retryable=True,
        )

    def _raise_http_error(self, status: int, *, attempts: int = 1) -> None:
        if 400 <= status < 500:
            raise AIClientError(
                f"AI API client error (status={status})",
                code=f"http_{status}",
                status_code=status,
                retryable=False,
                attempts=attempts,
            ) from None
        raise AIClientError(
            f"AI API request failed (status={status})",
            code=f"http_{status}",
            status_code=status,
            retryable=False,
            attempts=attempts,
        ) from None

    @staticmethod
    def _invalid_response_error(part: str) -> AIClientError:
        # ``part`` is a fixed internal label, never provider data.
        return AIClientError(
            f"AI API returned an invalid response envelope ({part})",
            code="invalid_response",
            retryable=False,
            attempts=1,
        )

    @staticmethod
    def _decode_json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except (TypeError, ValueError, UnicodeError):
            raise AIClientError(
                "AI API returned invalid JSON",
                code="invalid_response",
                retryable=False,
                attempts=1,
            ) from None
        if not isinstance(data, dict):
            raise AIClientError(
                "AI API returned an invalid response envelope",
                code="invalid_response",
                retryable=False,
            )
        return data

    @staticmethod
    def _remaining(deadline: float) -> float:
        return deadline - time.monotonic()

    def _make_deadline(self, override: float | None) -> float:
        if override is None:
            duration = self._deadline_seconds
        else:
            try:
                duration = float(override)
            except (TypeError, ValueError):
                raise ValueError("deadline_seconds должен быть конечным положительным числом") from None
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("deadline_seconds должен быть конечным положительным числом")
            duration = min(duration, self._deadline_seconds)
        return time.monotonic() + duration

    @staticmethod
    def _deadline_error(
        last_failure: _AttemptFailure | None,
        attempts: int,
    ) -> AIClientError:
        status = last_failure.error.status_code if last_failure else None
        retry_after = last_failure.retry_after if last_failure else None
        code = "deadline_exceeded"
        return AIClientError(
            "AI API request deadline exceeded",
            code=code,
            status_code=status,
            retryable=False,
            retry_after=retry_after,
            attempts=max(0, attempts),
        )

    def _retry_delay(self, attempt: int, retry_after: float | None) -> float:
        base = min(
            self._max_backoff_seconds,
            self._backoff_seconds * (2 ** max(0, attempt - 1)),
        )
        # Bounded jitter prevents synchronized retries while keeping every
        # individual delay finite and capped.
        jitter = random.uniform(0.0, min(base * 0.25, self._max_backoff_seconds))
        delay = min(self._max_backoff_seconds, base + jitter)
        if retry_after is not None:
            delay = max(delay, min(self._max_retry_after_seconds, max(0.0, retry_after)))
        return min(self._max_backoff_seconds if retry_after is None else self._max_retry_after_seconds, delay)

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if raw is None:
            return None
        raw = raw.strip()
        if not raw:
            return None
        try:
            seconds = float(raw)
        except (TypeError, ValueError):
            seconds = float("nan")
        if math.isfinite(seconds):
            return max(0.0, seconds)
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError, OverflowError, IndexError):
            return None
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        try:
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None

    # ------------------------------------------------------------------ #
    # Input and local rate limiting
    # ------------------------------------------------------------------ #

    @staticmethod
    def _validate_model(model: str) -> str:
        if not isinstance(model, str) or not model.strip():
            raise AIClientError("model должен быть непустой строкой", code="invalid_request")
        value = model.strip()
        if len(value) > 256 or any(ord(char) < 32 for char in value):
            raise AIClientError("model имеет недопустимый формат", code="invalid_request")
        return value

    @staticmethod
    def _validate_max_tokens(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value > 1_000_000:
            raise AIClientError("max_tokens имеет недопустимое значение", code="invalid_request")
        return value

    @staticmethod
    def _validate_temperature(value: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AIClientError("temperature имеет недопустимое значение", code="invalid_request")
        converted = float(value)
        if not math.isfinite(converted) or converted < 0.0 or converted > 2.0:
            raise AIClientError("temperature имеет недопустимое значение", code="invalid_request")
        return converted

    def _prepare_messages(self, messages: list[dict]) -> list[dict]:
        if not isinstance(messages, list):
            raise AIClientError("messages должен быть списком", code="invalid_request")
        copied: list[dict] = []
        for message in messages:
            if not isinstance(message, dict):
                raise AIClientError("message должен быть объектом", code="invalid_request")
            role = message.get("role")
            content = message.get("content")
            if not isinstance(role, str) or not role.strip() or not isinstance(content, str):
                raise AIClientError("message имеет недопустимый формат", code="invalid_request")
            copied.append(dict(message))

        limit = getattr(self._config, "ai_max_context_chars", None)
        if limit is None:
            return copied
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise AIClientError("AI_MAX_CONTEXT_CHARS имеет недопустимое значение", code="invalid_request") from None
        if limit <= 0:
            raise AIClientError("AI_MAX_CONTEXT_CHARS имеет недопустимое значение", code="invalid_request")

        def cost(message: dict) -> int:
            # Include a small structural allowance so the serialized payload,
            # not just visible content, remains bounded.
            return len(message["role"]) + len(message["content"]) + 16

        if sum(cost(message) for message in copied) <= limit:
            return copied

        # Always retain the system instruction when possible, then retain the
        # newest context entries.  A single oversized newest entry is clipped
        # rather than allowing the request to bypass the configured bound.
        selected: list[tuple[int, dict]] = []
        used = 0
        system_indexes = [i for i, message in enumerate(copied) if message["role"] == "system"]
        reserved_indexes: set[int] = set()
        if system_indexes:
            index = system_indexes[0]
            reserved_indexes.add(index)
            item = copied[index]
            available = max(0, limit - 16 - len(item["role"]))
            clipped = dict(item)
            clipped["content"] = item["content"][:available]
            selected.append((index, clipped))
            used += cost(clipped)

        for index in range(len(copied) - 1, -1, -1):
            if index in reserved_indexes:
                continue
            item = copied[index]
            remaining = limit - used
            if remaining <= 0:
                break
            if cost(item) <= remaining:
                selected.append((index, dict(item)))
                used += cost(item)
                continue
            available = max(0, remaining - len(item["role"]) - 16)
            if available <= 0:
                break
            clipped = dict(item)
            clipped["content"] = item["content"][:available]
            selected.append((index, clipped))
            used += cost(clipped)
            break

        selected.sort(key=lambda pair: pair[0])
        return [message for _, message in selected]

    async def _reserve_user_request(self, user_id: object, deadline: float) -> None:
        limit = getattr(self._config, "ai_max_requests_per_user_per_hour", None)
        if limit is None:
            return
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise AIClientError(
                "AI_MAX_REQUESTS_PER_USER_PER_HOUR имеет недопустимое значение",
                code="invalid_request",
            ) from None
        if limit <= 0:
            raise AIClientError(
                "AI_MAX_REQUESTS_PER_USER_PER_HOUR имеет недопустимое значение",
                code="invalid_request",
            )
        try:
            hash(user_id)
        except TypeError:
            raise AIClientError("user_id должен быть хешируемым", code="invalid_request") from None
        if self._remaining(deadline) <= 0:
            raise self._deadline_error(None, 0)

        now = time.monotonic()
        async with self._rate_lock:
            timestamps = self._request_times.setdefault(user_id, deque())
            cutoff = now - 3600.0
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= limit:
                retry_after = max(0.0, timestamps[0] + 3600.0 - now)
                raise AIRateLimitError(
                    "AI request limit for user exceeded",
                    code="user_rate_limit",
                    retryable=True,
                    retry_after=retry_after,
                )
            timestamps.append(now)
