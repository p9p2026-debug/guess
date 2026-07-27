"""Focused tests for task 3.15 safe update routing.

Validates Requirements 2.4, 2.5, 2.7, 2.13, 2.15, 2.16, 3.9, 3.11, 3.12.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from photo_guess_game.callback_codec import decode_callback, encode_callback
from photo_guess_game.models import GameSession, GameState, OperationResult, Player
from photo_guess_game.telegram_transport import DeliveryOutcome
from run_bot import TelegramBotRunner


def run(coro):
    return asyncio.run(coro)


def private_update(update_id=1, **payload):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": 7, "type": "private"},
            "from": {"id": 7, "first_name": "User"},
            **payload,
        },
    }


def callback_update(data, update_id=1):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"cb-{update_id}",
            "from": {"id": 7, "first_name": "User"},
            "message": {"chat": {"id": -100, "type": "group"}},
            "data": data,
        },
        "message": private_update(update_id, photo=[{"file_id": "ignored"}])["message"],
    }


def successful_delivery():
    return DeliveryOutcome(True, False, 1, status_code=200)


def test_callback_has_priority_and_selects_exactly_one_handler():
    async def scenario():
        runner = TelegramBotRunner("test-token")
        calls = []

        async def join(*args, **kwargs):
            calls.append(("join", args, kwargs))
            return OperationResult(True)

        async def photo(*args, **kwargs):
            calls.append(("photo", args, kwargs))
            return OperationResult(True)

        async def answer(*args, **kwargs):
            calls.append(("answer", args, kwargs))
            return successful_delivery()

        runner.adapter.handle_join = join
        runner.adapter.handle_dm_photo = photo
        runner.answer_callback_query = answer
        outcome = await runner.process_update(callback_update(encode_callback(3, 9, "jn")))
        return outcome, calls

    outcome, calls = run(scenario())
    assert outcome.ok and outcome.route == "callback"
    assert [name for name, *_ in calls] == ["join", "answer"]
    _, args, kwargs = calls[0]
    assert args[:3] == (-100, 7, "User")
    assert kwargs == {"generation": 3, "panel_revision": 9, "update_id": 1}


def test_private_photo_uses_highest_resolution_and_never_sends_help():
    async def scenario():
        runner = TelegramBotRunner("test-token")
        calls = []

        async def photo(user_id, file_id, **kwargs):
            calls.append(("photo", user_id, file_id, kwargs))
            return OperationResult(True)

        async def help_send(*args, **kwargs):
            calls.append(("help", args, kwargs))
            return successful_delivery()

        runner.adapter.handle_dm_photo = photo
        runner.send_message = help_send
        outcome = await runner.process_update(private_update(
            photo=[{"file_id": "small"}, {"file_id": "large"}],
            text="/help",
        ))
        return outcome, calls

    outcome, calls = run(scenario())
    assert outcome.route == "private_photo" and outcome.ok
    assert calls == [("photo", 7, "large", {"update_id": 1})]


def test_pending_private_text_routes_once_before_help():
    async def scenario():
        runner = TelegramBotRunner("test-token")
        calls = []
        runner.adapter.photo_distributor.pending_context_for_user = (
            lambda _user_id: SimpleNamespace(context_id=4)
        )

        async def text_reply(user_id, text, **kwargs):
            calls.append(("text", user_id, text, kwargs))
            return OperationResult(True)

        async def help_send(*args, **kwargs):
            calls.append(("help", args, kwargs))
            return successful_delivery()

        runner.adapter.handle_dm_text_reply = text_reply
        runner.send_message = help_send
        outcome = await runner.process_update(private_update(text="-100"))
        return outcome, calls

    outcome, calls = run(scenario())
    assert outcome.route == "private_text" and outcome.ok
    assert calls == [("text", 7, "-100", {"update_id": 1})]


@pytest.mark.parametrize("payload", [{"text": "/help"}, {"text": "unknown"}, {"document": {}}])
def test_private_help_is_only_the_unsupported_fallback(payload):
    async def scenario():
        runner = TelegramBotRunner("test-token")
        sends = []

        async def send(target_id, text, **kwargs):
            sends.append((target_id, text, kwargs))
            return successful_delivery()

        runner.send_message = send
        return await runner.process_update(private_update(**payload)), sends

    outcome, sends = run(scenario())
    assert outcome.ok and outcome.route == "private_help"
    assert len(sends) == 1 and sends[0][0] == 7


def test_malformed_callback_is_bounded_and_answered_once():
    async def scenario():
        runner = TelegramBotRunner("test-token")
        answers = []

        async def answer(*args, **kwargs):
            answers.append((args, kwargs))
            return successful_delivery()

        runner.answer_callback_query = answer
        outcome = await runner.process_update(callback_update("vote:not-an-int"))
        return outcome, answers

    outcome, answers = run(scenario())
    assert not outcome.ok and outcome.reason == "malformed_callback"
    assert len(answers) == 1


def test_stale_callback_preserves_current_generation():
    async def scenario():
        runner = TelegramBotRunner("test-token")
        session = GameSession(-100, 1, GameState.LOBBY, {1: Player(1, "Host")})
        runner.store.create(session)
        before = session.public_snapshot()
        answers = []

        async def answer(*args, **kwargs):
            answers.append((args, kwargs))
            return successful_delivery()

        runner.answer_callback_query = answer
        stale = encode_callback(session.generation + 1, session.revision, "jn")
        outcome = await runner.process_update(callback_update(stale))
        return outcome, before, runner.store.get(-100).public_snapshot(), answers

    outcome, before, after, answers = run(scenario())
    assert not outcome.ok and outcome.reason == "stale_generation"
    assert before == after
    assert len(answers) == 1


def test_safe_dm_disambiguation_callback_contains_generation_and_context():
    runner = TelegramBotRunner("test-token")
    for group_id in (-201, -202):
        session = GameSession(group_id, 7, GameState.LOBBY, {7: Player(7, "User")})
        runner.store.create(session)
    result = runner.adapter.photo_distributor.submit_photo(7, "private-file-id")

    assert result.reason == "disambiguation_required"
    callbacks = [row[0]["callback_data"] for row in result.notifications[0].buttons]
    decoded = [decode_callback(value) for value in callbacks]
    assert [item.action for item in decoded] == ["dm", "dm"]
    assert len({item.phase_or_ballot for item in decoded}) == 1
    assert [item.arg for item in decoded] == [0, 1]


def test_duplicate_committed_group_update_does_not_repeat_mutation_or_effect():
    async def scenario():
        runner = TelegramBotRunner("test-token")
        sends = []

        async def send(target_id, text, reply_markup=None):
            sends.append((target_id, text, reply_markup))
            return successful_delivery()

        runner.adapter._send_message_fn = send
        update = {
            "update_id": 42,
            "message": {
                "chat": {"id": -300, "type": "group"},
                "from": {"id": 1, "first_name": "Host"},
                "text": "/newgame",
            },
        }
        first = await runner.process_update(update)
        duplicate = await runner.process_update(update)
        return runner, first, duplicate, sends

    runner, first, duplicate, sends = run(scenario())
    assert first.ok and duplicate.ok
    assert tuple(runner.store.get(-300).players) == (1,)
    assert len(sends) == 1


def test_failure_is_contained_and_structured_logs_exclude_payloads(caplog):
    async def scenario():
        runner = TelegramBotRunner("test-token")

        async def fail(_user_id, _file_id, **_kwargs):
            raise RuntimeError("secret-word role=spy private-file-id")

        runner.adapter.handle_dm_photo = fail
        with caplog.at_level(logging.ERROR, logger="run_bot"):
            outcome = await runner.process_update(
                private_update(photo=[{"file_id": "private-file-id"}])
            )
        return outcome

    outcome = run(scenario())
    assert not outcome.ok and outcome.reason == "handler_exception"
    assert caplog.records
    record = caplog.records[-1]
    assert record.event_name == "update_exception"
    assert record.update_id == 1
    serialized = " ".join(record.getMessage() for record in caplog.records)
    assert "secret-word" not in serialized
    assert "role=spy" not in serialized
    assert "private-file-id" not in serialized


def test_group_command_routes_once_with_update_id():
    async def scenario():
        runner = TelegramBotRunner("test-token")
        calls = []

        async def status(group_id, **kwargs):
            calls.append((group_id, kwargs))
            return OperationResult(True)

        runner.adapter.handle_status = status
        outcome = await runner.process_update({
            "update_id": 8,
            "message": {
                "chat": {"id": -400, "type": "group"},
                "from": {"id": 9, "first_name": "Host"},
                "text": "/status",
            },
        })
        return outcome, calls

    outcome, calls = run(scenario())
    assert outcome.ok and outcome.route == "group_command"
    assert calls == [(-400, {"update_id": 8})]
