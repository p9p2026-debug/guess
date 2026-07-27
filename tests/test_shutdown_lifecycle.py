"""Focused task 3.16 orderly-shutdown tests.

No test uses network access or wall-clock sleeps.
**Validates: Requirements 2.3, 2.7, 2.12, 2.15, 2.16, 3.9, 3.10**
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass

from photo_guess_game.models import DeliveryStatus, GameSession, GameState, Player
from photo_guess_game.telegram_transport import DeliveryOutcome
from photo_guess_game.timer_service import TimerEvent, TimerService
from run_bot import TelegramBotRunner


@dataclass
class FakeHandle:
    callback: object
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True

    def invoke(self) -> None:
        self.callback()


class FakeScheduler:
    def __init__(self) -> None:
        self.handles: list[FakeHandle] = []

    def call_later(self, _delay: float, callback) -> FakeHandle:
        handle = FakeHandle(callback)
        self.handles.append(handle)
        return handle


def group_start_update(update_id: int, group_id: int) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": group_id, "type": "group"},
            "from": {"id": 1, "first_name": "Host"},
            "text": "/startgame",
        },
    }


def test_shutdown_during_role_delivery_drains_and_preserves_completed_send() -> None:
    async def scenario():
        runner = TelegramBotRunner("test-token")
        manager = runner.adapter.session_manager
        group_id = -700
        manager.create_session(group_id, 1, "Host")
        manager.join_session(group_id, 2, "Player")

        calls: Counter[int] = Counter()
        completed = asyncio.Event()
        blocked = asyncio.Event()

        async def send(target_id, _text, _reply_markup=None):
            calls[target_id] += 1
            if target_id == 1:
                completed.set()
                return DeliveryOutcome(True, False, 1, status_code=200)
            blocked.set()
            await asyncio.Event().wait()

        runner.adapter._send_message_fn = send
        start_task = asyncio.create_task(
            runner.process_update(group_start_update(51, group_id))
        )
        await completed.wait()
        await blocked.wait()

        await runner.shutdown()
        outcome = await start_task
        late = await runner.process_update(group_start_update(52, group_id))
        session = runner.store.get(group_id)
        return runner, outcome, late, session, calls

    runner, outcome, late, session, calls = asyncio.run(scenario())

    assert outcome.reason == "shutting_down"
    assert not late.ok and late.reason == "shutting_down"
    assert calls == Counter({1: 1, 2: 1})
    assert session.delivery[1].status == DeliveryStatus.DELIVERED
    assert session.delivery[2].status == DeliveryStatus.FAILED_PERMANENT
    assert runner.adapter.effect_runner.pending_count == 0
    assert runner.adapter.effect_runner.registry.accepting is False
    assert runner.adapter.timer_service.pending_count == 0
    slot = runner.store.lock_slot_snapshot(session.group_chat_id)
    assert slot is not None and slot.refs == 0 and not slot.locked
    assert slot.retire_requested is False


def test_shutdown_during_timer_removes_handle_and_retires_terminal_lock() -> None:
    async def scenario():
        runner = TelegramBotRunner("test-token")
        scheduler = FakeScheduler()
        observed: list[TimerEvent] = []
        service = TimerService(
            scheduler.call_later,
            observed.append,
            session_lookup=runner.store.get_for_key,
        )
        runner.adapter.timer_service = service

        session = GameSession(
            -701,
            1,
            GameState.GUESSING,
            {1: Player(1, "Host")},
        )
        key = runner.store.create(session)
        assert service.schedule(key, "timeout", "round-timeout", 30)
        handle = scheduler.handles[-1]
        await runner.store.commit_terminal(key, GameState.CANCELLED)

        await runner.shutdown()
        handle.invoke()
        await runner.shutdown()
        return runner, service, observed, handle

    runner, service, observed, handle = asyncio.run(scenario())

    assert handle.cancelled is True
    assert observed == []
    assert service.pending_count == 0
    assert service.accepting is False
    assert runner.adapter.effect_runner.pending_count == 0
    assert runner.store.lock_slot_count == 0
