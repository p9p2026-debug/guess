"""Task 3.17 integration tests for the single event executor.

**Validates: Requirements 2.2, 2.4, 2.16, 3.4, 3.12**
"""

from __future__ import annotations

import asyncio

from photo_guess_game.callback_codec import encode_callback
from photo_guess_game.event_executor import EventExecutor
from photo_guess_game.models import GameState, RoundPhase
from photo_guess_game.session_store import SessionStore
from photo_guess_game.telegram_adapter import TelegramAdapter
from photo_guess_game.timer_service import TimerEvent
from photo_guess_game.telegram_transport import DeliveryOutcome
from run_bot import TelegramBotRunner


class ProbeTransport:
    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.calls: list[tuple[int, str]] = []
        self.lock_states: list[bool] = []

    async def send_message(self, target_id, text, reply_markup=None):
        self.lock_states.append(
            any(slot.lock.locked() for slot in self.store._locks.values())
        )
        self.calls.append((target_id, text))
        return {"ok": True}

    async def send_photo(self, target_id, file_id, text, reply_markup=None):
        return await self.send_message(target_id, text, reply_markup)


class RecordingExecutor(EventExecutor):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.decisions: list[tuple[int, object]] = []

    async def decide(self, group_chat_id, decision, **kwargs):
        self.decisions.append((group_chat_id, kwargs.get("session_key")))
        return await super().decide(group_chat_id, decision, **kwargs)


def callback_update(update_id: int, user_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": user_id, "first_name": f"U{user_id}"},
            "message": {"chat": {"id": -700, "type": "group"}},
            "data": data,
        },
    }
def test_runner_group_callback_dm_and_timer_use_one_executor() -> None:
    async def scenario():
        store = SessionStore()
        transport = ProbeTransport(store)
        adapter = TelegramAdapter(
            store,
            send_message_fn=transport.send_message,
            send_photo_fn=transport.send_photo,
        )
        executor = RecordingExecutor(
            store, adapter.effect_runner, is_accepting=lambda: adapter.accepting
        )
        adapter.event_executor = executor
        runner = TelegramBotRunner("test-token")
        runner.store = store
        runner.adapter = adapter

        async def answer_callback(*_args, **_kwargs):
            return DeliveryOutcome(True, False, 1, status_code=200)

        runner.answer_callback_query = answer_callback

        created = await runner.process_update({
            "update_id": 1,
            "message": {
                "chat": {"id": -700, "type": "group"},
                "from": {"id": 1, "first_name": "Host"},
                "text": "/newgame",
            },
        })
        assert created.ok
        session = store.get(-700)
        key = session.session_key
        joined = await runner.process_update(
            callback_update(
                2,
                2,
                encode_callback(key.generation, session.revision, "jn"),
            )
        )
        assert joined.ok
        photo = await runner.process_update({
            "update_id": 3,
            "message": {
                "chat": {"id": 1, "type": "private"},
                "from": {"id": 1, "first_name": "Host"},
                "photo": [{"file_id": "small"}, {"file_id": "photo-host"}],
            },
        })
        assert photo.ok

        session = store.get(-700)
        session.state = GameState.GUESSING
        session.round_phase = RoundPhase.DISCUSSION
        session.labels = {"Photo A": 1, "Photo B": 2}
        session.revision += 1
        store.put(session)
        timer = TimerEvent(
            key,
            "guessing_timeout",
            "timer-current",
            RoundPhase.DISCUSSION,
            session.revision,
        )
        adapter._on_timer_event(timer)
        tasks = tuple(adapter.effect_runner.registry._tasks[key])
        await asyncio.gather(*tasks)
        return store, transport, executor, key

    store, transport, executor, key = asyncio.run(scenario())
    assert store.get(key.group_chat_id) is None
    assert transport.lock_states and not any(transport.lock_states)
    exact = [recorded for _group, recorded in executor.decisions if recorded]
    assert exact.count(key) >= 3  # callback, DM commit, and TimerEvent
    assert {group for group, _key in executor.decisions} == {-700}


def test_stale_timer_cannot_touch_replacement_generation() -> None:
    async def scenario():
        store = SessionStore()
        transport = ProbeTransport(store)
        adapter = TelegramAdapter(store, send_message_fn=transport.send_message)
        await adapter.handle_newgame(-701, 1, "Host", update_id=1)
        old = store.get(-701).session_key
        old_revision = store.get(-701).revision
        await adapter.handle_cancelgame(
            -701, 1, generation=old.generation, update_id=2
        )
        await adapter.handle_newgame(-701, 1, "Host", update_id=3)
        current = store.get(-701)
        before = current.public_snapshot()
        transport.calls.clear()
        result = await adapter._execute_timer_event(TimerEvent(
            old,
            "guessing_timeout",
            "timer-old",
            RoundPhase.DISCUSSION,
            old_revision,
        ))
        return result, before, store.get(-701).public_snapshot(), transport.calls

    result, before, after, calls = asyncio.run(scenario())
    assert not result.ok and result.reason == "stale_generation"
    assert after == before
    assert calls == []


def test_multi_group_dm_requires_exact_generation_commit() -> None:
    async def scenario():
        store = SessionStore()
        transport = ProbeTransport(store)
        adapter = TelegramAdapter(store, send_message_fn=transport.send_message)
        await adapter.handle_newgame(-710, 1, "Host", update_id=10)
        await adapter.handle_newgame(-711, 1, "Host", update_id=11)
        first = store.get(-710).session_key
        second = store.get(-711).session_key

        pending = await adapter.handle_dm_photo(1, "private-photo", update_id=12)
        assert pending.reason == "disambiguation_required"
        assert store.get(-710).players[1].photo_file_id is None
        assert store.get(-711).players[1].photo_file_id is None

        stale = await adapter.handle_dm_text_reply(
            1,
            str(second.group_chat_id),
            generation=first.generation,
            update_id=13,
        )
        committed = await adapter.handle_dm_text_reply(
            1,
            str(second.group_chat_id),
            generation=second.generation,
            update_id=14,
        )
        return store, stale, committed, first, second

    store, stale, committed, first, second = asyncio.run(scenario())
    assert stale.reason == "stale_generation"
    assert committed.ok
    assert store.get(first.group_chat_id).players[1].photo_file_id is None
    assert store.get(second.group_chat_id).players[1].photo_file_id == "private-photo"
