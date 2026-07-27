"""Strict, bounded Telegram Bot API delivery transport."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
import time
from typing import Any, Awaitable, Callable, Mapping, Protocol

import httpx


class DeliveryFailureKind(str, Enum):
    """Stable failure classifications exposed to delivery reconciliation."""

    CLIENT_UNAVAILABLE = "client_unavailable"
    EXCEPTION = "exception"
    TIMEOUT = "timeout"
    HTTP_STATUS = "http_status"
    INVALID_JSON = "invalid_json"
    TELEGRAM_ERROR = "telegram_error"


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """Result of one bounded logical delivery, including all attempts."""

    delivered: bool
    retryable: bool
    attempts: int
    status_code: int | None = None
    telegram_error_code: int | None = None
    description: str | None = None
    failure_kind: DeliveryFailureKind | None = None


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounds retry count and all waits without relying on wall-clock sleeps."""

    max_attempts: int = 3
    base_delay: float = 0.5
    max_backoff: float = 2.0
    max_retry_after: float = 10.0
    max_elapsed: float = 15.0
    request_timeout: float = 10.0
    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        for name in (
            "base_delay",
            "max_backoff",
            "max_retry_after",
            "max_elapsed",
            "request_timeout",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} cannot be negative")


class TelegramResponse(Protocol):
    status_code: int
    headers: Mapping[str, str]

    def json(self) -> Any: ...


class TelegramHttpClient(Protocol):
    async def post(
        self, url: str, *, json: dict[str, Any], timeout: float
    ) -> TelegramResponse: ...


Sleeper = Callable[[float], Awaitable[None]]
Clock = Callable[[], float]
ClientProvider = Callable[[], TelegramHttpClient | None]


class TelegramTransport:
    """Shared strict transport for every outbound Telegram API method."""

    def __init__(
        self,
        *,
        base_url: str,
        client_provider: ClientProvider,
        retry_policy: RetryPolicy | None = None,
        sleeper: Sleeper = asyncio.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client_provider = client_provider
        self._policy = retry_policy or RetryPolicy()
        self._sleeper = sleeper
        self._clock = clock

    async def send_message(
        self,
        target_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> DeliveryOutcome:
        payload: dict[str, Any] = {
            "chat_id": target_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._request("sendMessage", payload)

    async def send_photo(
        self,
        target_id: int,
        photo_file_id: str,
        caption: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str = "HTML",
    ) -> DeliveryOutcome:
        payload: dict[str, Any] = {
            "chat_id": target_id,
            "photo": photo_file_id,
            "caption": caption,
            "parse_mode": parse_mode,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._request("sendPhoto", payload)
    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str = "",
        show_alert: bool = False,
    ) -> DeliveryOutcome:
        return await self._request(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            },
        )

    async def _request(
        self, method: str, payload: dict[str, Any]
    ) -> DeliveryOutcome:
        started_at = self._clock()
        attempts = 0
        while attempts < self._policy.max_attempts:
            attempts += 1
            client = self._client_provider()
            if client is None:
                return DeliveryOutcome(
                    delivered=False,
                    retryable=False,
                    attempts=attempts,
                    description="Telegram HTTP client is not configured",
                    failure_kind=DeliveryFailureKind.CLIENT_UNAVAILABLE,
                )

            try:
                response = await client.post(
                    f"{self._base_url}/{method}",
                    json=payload,
                    timeout=self._policy.request_timeout,
                )
            except (httpx.TimeoutException, asyncio.TimeoutError) as error:
                outcome = DeliveryOutcome(
                    delivered=False,
                    retryable=True,
                    attempts=attempts,
                    description=str(error) or "Telegram request timed out",
                    failure_kind=DeliveryFailureKind.TIMEOUT,
                )
                retry_after = None
            except Exception as error:
                return DeliveryOutcome(
                    delivered=False,
                    retryable=False,
                    attempts=attempts,
                    description=str(error) or type(error).__name__,
                    failure_kind=DeliveryFailureKind.EXCEPTION,
                )
            else:
                outcome, retry_after = self._classify_response(response, attempts)
                if outcome.delivered or not outcome.retryable:
                    return outcome

            if attempts >= self._policy.max_attempts:
                return outcome
            delay = self._retry_delay(attempts, retry_after)
            if self._clock() - started_at + delay > self._policy.max_elapsed:
                return outcome
            await self._sleeper(delay)

        raise AssertionError("bounded retry loop exited unexpectedly")

    def _classify_response(
        self, response: TelegramResponse, attempts: int
    ) -> tuple[DeliveryOutcome, float | None]:
        status_code = response.status_code
        try:
            data = response.json()
        except (TypeError, ValueError):
            data = None

        if not 200 <= status_code < 300:
            error_code, description, retry_after = self._error_details(data, response.headers)
            return (
                DeliveryOutcome(
                    delivered=False,
                    retryable=status_code == 429 or 500 <= status_code < 600,
                    attempts=attempts,
                    status_code=status_code,
                    telegram_error_code=error_code,
                    description=description or f"Telegram HTTP status {status_code}",
                    failure_kind=DeliveryFailureKind.HTTP_STATUS,
                ),
                retry_after,
            )

        if not isinstance(data, dict):
            return (
                DeliveryOutcome(
                    delivered=False,
                    retryable=False,
                    attempts=attempts,
                    status_code=status_code,
                    description="Telegram response was not a JSON object",
                    failure_kind=DeliveryFailureKind.INVALID_JSON,
                ),
                None,
            )

        if data.get("ok") is True:
            return DeliveryOutcome(True, False, attempts, status_code=status_code), None

        error_code, description, retry_after = self._error_details(data, response.headers)
        retryable = error_code == 429 or retry_after is not None
        return (
            DeliveryOutcome(
                delivered=False,
                retryable=retryable,
                attempts=attempts,
                status_code=status_code,
                telegram_error_code=error_code,
                description=description or "Telegram API returned ok:false",
                failure_kind=DeliveryFailureKind.TELEGRAM_ERROR,
            ),
            retry_after,
        )
    def _error_details(
        self, data: Any, headers: Mapping[str, str]
    ) -> tuple[int | None, str | None, float | None]:
        error_code: int | None = None
        description: str | None = None
        retry_value: Any = None
        if isinstance(data, dict):
            raw_error_code = data.get("error_code")
            if isinstance(raw_error_code, int) and not isinstance(raw_error_code, bool):
                error_code = raw_error_code
            raw_description = data.get("description")
            if isinstance(raw_description, str):
                description = raw_description
            parameters = data.get("parameters")
            if isinstance(parameters, dict):
                retry_value = parameters.get("retry_after")

        if retry_value is None:
            retry_value = headers.get("Retry-After") or headers.get("retry-after")
        return error_code, description, self._bounded_retry_after(retry_value)

    def _bounded_retry_after(self, value: Any) -> float | None:
        if isinstance(value, bool):
            return None
        try:
            delay = float(value)
        except (TypeError, ValueError):
            return None
        if delay < 0:
            return None
        return min(delay, self._policy.max_retry_after)

    def _retry_delay(self, attempts: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return retry_after
        exponential = self._policy.base_delay * (2 ** (attempts - 1))
        return min(exponential, self._policy.max_backoff)
