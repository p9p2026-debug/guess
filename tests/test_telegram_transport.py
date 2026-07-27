"""Outcome and retry matrix for strict Telegram delivery."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from photo_guess_game.telegram_transport import (
    DeliveryFailureKind,
    RetryPolicy,
    TelegramTransport,
)


@dataclass
class FakeResponse:
    status_code: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)
    invalid_json: bool = False

    def json(self):
        if self.invalid_json:
            raise ValueError("invalid json")
        return self.body


class FakeClient:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def post(self, url, *, json, timeout):
        self.calls.append((url, json, timeout))
        result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay
def make_transport(client, fake_time=None, **policy_overrides):
    fake_time = fake_time or FakeTime()
    policy = RetryPolicy(**policy_overrides)
    return (
        TelegramTransport(
            base_url="https://api.telegram.test/bot-token",
            client_provider=lambda: client,
            retry_policy=policy,
            sleeper=fake_time.sleep,
            clock=fake_time.clock,
        ),
        fake_time,
    )


@pytest.mark.parametrize(
    ("response", "kind", "status", "telegram_code"),
    [
        (FakeResponse(400, {"ok": False, "description": "bad"}), DeliveryFailureKind.HTTP_STATUS, 400, None),
        (FakeResponse(200, invalid_json=True), DeliveryFailureKind.INVALID_JSON, 200, None),
        (FakeResponse(200, ["not", "an", "object"]), DeliveryFailureKind.INVALID_JSON, 200, None),
        (FakeResponse(200, {"ok": False, "error_code": 403, "description": "forbidden"}), DeliveryFailureKind.TELEGRAM_ERROR, 200, 403),
        (FakeResponse(200, {"ok": 1}), DeliveryFailureKind.TELEGRAM_ERROR, 200, None),
    ],
)
def test_permanent_response_failure_matrix(response, kind, status, telegram_code):
    async def scenario():
        client = FakeClient(response)
        transport, fake_time = make_transport(client)
        outcome = await transport.send_message(10, "hello")

        assert outcome.delivered is False
        assert outcome.retryable is False
        assert outcome.attempts == 1
        assert outcome.failure_kind is kind
        assert outcome.status_code == status
        assert outcome.telegram_error_code == telegram_code
        assert len(client.calls) == 1
        assert fake_time.sleeps == []

    asyncio.run(scenario())


def test_exception_is_failure_and_only_timeout_is_retried():
    async def scenario():
        exception_client = FakeClient(OSError("connection reset"))
        transport, _ = make_transport(exception_client)
        exception_outcome = await transport.send_message(10, "hello")
        assert exception_outcome.failure_kind is DeliveryFailureKind.EXCEPTION
        assert exception_outcome.retryable is False
        assert exception_outcome.attempts == 1

        timeout_client = FakeClient(
            httpx.ReadTimeout("slow"), FakeResponse(200, {"ok": True})
        )
        retrying_transport, fake_time = make_transport(timeout_client)
        timeout_outcome = await retrying_transport.send_message(10, "hello")
        assert timeout_outcome.delivered is True
        assert timeout_outcome.attempts == 2
        assert fake_time.sleeps == [0.5]

    asyncio.run(scenario())
@pytest.mark.parametrize(
    ("first_response", "expected_delay"),
    [
        (FakeResponse(500, {"ok": False}), 0.5),
        (
            FakeResponse(
                429,
                {"ok": False, "parameters": {"retry_after": 2}},
            ),
            2.0,
        ),
        (
            FakeResponse(
                200,
                {
                    "ok": False,
                    "error_code": 429,
                    "parameters": {"retry_after": 3},
                },
            ),
            3.0,
        ),
    ],
)
def test_retryable_response_matrix(first_response, expected_delay):
    async def scenario():
        client = FakeClient(first_response, FakeResponse(200, {"ok": True}))
        transport, fake_time = make_transport(client)
        outcome = await transport.send_photo(10, "file-id", "caption")

        assert outcome.delivered is True
        assert outcome.attempts == 2
        assert len(client.calls) == 2
        assert fake_time.sleeps == [expected_delay]

    asyncio.run(scenario())


def test_retries_are_bounded_by_attempt_count_and_elapsed_time():
    async def scenario():
        responses = [FakeResponse(503, {"ok": False}) for _ in range(3)]
        client = FakeClient(*responses)
        transport, fake_time = make_transport(client, max_attempts=3)
        outcome = await transport.send_message(10, "hello")
        assert outcome.delivered is False
        assert outcome.retryable is True
        assert outcome.attempts == 3
        assert len(client.calls) == 3
        assert fake_time.sleeps == [0.5, 1.0]

        elapsed_client = FakeClient(
            FakeResponse(429, {"ok": False, "parameters": {"retry_after": 99}}),
            FakeResponse(200, {"ok": True}),
        )
        elapsed_transport, elapsed_time = make_transport(
            elapsed_client, max_retry_after=4, max_elapsed=3
        )
        elapsed_outcome = await elapsed_transport.send_message(10, "hello")
        assert elapsed_outcome.attempts == 1
        assert len(elapsed_client.calls) == 1
        assert elapsed_time.sleeps == []

    asyncio.run(scenario())
@pytest.mark.parametrize(
    ("method_name", "expected_endpoint", "expected_payload"),
    [
        (
            "message",
            "sendMessage",
            {"chat_id": 7, "text": "text", "parse_mode": "HTML"},
        ),
        (
            "photo",
            "sendPhoto",
            {
                "chat_id": 7,
                "photo": "photo-id",
                "caption": "caption",
                "parse_mode": "HTML",
            },
        ),
        (
            "callback",
            "answerCallbackQuery",
            {"callback_query_id": "cb-1", "text": "done", "show_alert": True},
        ),
    ],
)
def test_each_public_method_uses_shared_strict_transport_once(
    method_name, expected_endpoint, expected_payload
):
    async def scenario():
        client = FakeClient(FakeResponse(200, {"ok": True}))
        transport, _ = make_transport(client)

        if method_name == "message":
            outcome = await transport.send_message(7, "text")
        elif method_name == "photo":
            outcome = await transport.send_photo(7, "photo-id", "caption")
        else:
            outcome = await transport.answer_callback_query("cb-1", "done", True)

        assert outcome.delivered is True
        assert outcome.attempts == 1
        assert len(client.calls) == 1
        url, payload, timeout = client.calls[0]
        assert url.endswith(f"/{expected_endpoint}")
        assert payload == expected_payload
        assert timeout == 10.0

    asyncio.run(scenario())


def test_unconfigured_client_is_visible_failure_without_attempting_io():
    async def scenario():
        fake_time = FakeTime()
        transport = TelegramTransport(
            base_url="https://api.telegram.test/bot-token",
            client_provider=lambda: None,
            sleeper=fake_time.sleep,
            clock=fake_time.clock,
        )
        outcome = await transport.answer_callback_query("cb")
        assert outcome.delivered is False
        assert outcome.failure_kind is DeliveryFailureKind.CLIENT_UNAVAILABLE
        assert outcome.attempts == 1
        assert fake_time.sleeps == []

    asyncio.run(scenario())
