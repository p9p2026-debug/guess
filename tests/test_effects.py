"""Task 3.8 EffectRunner and TaskRegistry tests.

**Validates: Requirements 2.2, 2.3, 2.7, 2.15, 2.16, 3.9**
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from photo_guess_game.effects import EffectRunner, TaskRegistry
from photo_guess_game.models import Effect, GameSession, GameState, Player, SessionKey
from photo_guess_game.session_store import SessionStore


def make_effect(
    effect_id: str = "notify:10:1",
    key: SessionKey = SessionKey(10, 1),
) -> Effect:
    return Effect(
        effect_id=effect_id,
        session_key=key,
        expected_revision=1,
        kind="telegram",
        payload={"target": 7, "text": "private"},
    )


def test_success_runs_after_transaction_lock_release_and_is_tracked():
    async def scenario():
        store = SessionStore()
        session = GameSession(
            10,
            1,
            GameState.LOBBY,
            {1: Player(1, "Host")},
        )
        key = store.create(session)
        effect = make_effect(key=key)
        lock_states = []

        def decide(view):
            assert view.lock_held is True
            session.revision = 1
            view.put(session)
            return (effect,)

        effects = await store.transact(10, decide)

        async def execute(_effect):
            lock_states.append(store.lock_slot_snapshot(10).locked)
            await asyncio.sleep(0)
            return None

        runner = EffectRunner(execute, ledger=store)
        outcomes = await runner.run(effects)
        return runner, store, outcomes, lock_states

    runner, store, outcomes, lock_states = asyncio.run(scenario())
    assert lock_states == [False]
    assert len(outcomes) == 1
    assert outcomes[0].ok is True
    assert outcomes[0].replayed is False
    assert runner.pending_count == 0
    assert store.processed_effect_outcome(outcomes[0].effect_id).ok is True


def test_failure_is_recorded_without_escaping_or_leaking_exception_text():
    async def scenario():
        async def fail(_effect):
            raise RuntimeError("secret role text must not be retained")

        runner = EffectRunner(fail)
        outcome = (await runner.run((make_effect(),)))[0]
        await asyncio.sleep(0)
        return runner, outcome

    runner, outcome = asyncio.run(scenario())
    assert outcome.ok is False
    assert outcome.error_type == "RuntimeError"
    assert outcome.cancelled is False
    assert "secret role text" not in repr(outcome)
    assert runner.outcome_for(outcome.effect_id) == outcome
    assert runner.pending_count == 0


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool


def test_redelivery_retries_failure_but_never_repeats_confirmed_effect():
    async def scenario():
        store = SessionStore()
        attempts = 0

        async def execute(_effect):
            nonlocal attempts
            attempts += 1
            return DeliveryResult(delivered=attempts >= 2)

        runner = EffectRunner(execute, ledger=store)
        effect = make_effect()
        failed = (await runner.run((effect,)))[0]
        succeeded = (await runner.run((effect,)))[0]
        replayed = (await runner.run((effect,)))[0]
        return runner, store, attempts, failed, succeeded, replayed

    runner, store, attempts, failed, succeeded, replayed = asyncio.run(scenario())
    assert failed.ok is False
    assert succeeded.ok is True and succeeded.replayed is False
    assert replayed.ok is True and replayed.replayed is True
    assert attempts == 2
    assert runner.outcome_for(failed.effect_id).ok is True
    assert store.processed_effect_outcome(failed.effect_id).ok is True


def test_duplicate_ids_in_one_batch_coalesce_to_one_execution():
    async def scenario():
        calls = 0

        async def execute(_effect):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            return True

        runner = EffectRunner(execute)
        effect = make_effect()
        outcomes = await runner.run((effect, effect, effect))
        return runner, calls, outcomes

    runner, calls, outcomes = asyncio.run(scenario())
    assert calls == 1
    assert [outcome.ok for outcome in outcomes] == [True, True, True]
    assert [outcome.replayed for outcome in outcomes] == [False, True, True]
    assert runner.pending_count == 0


def test_cancel_and_drain_cancels_only_the_owned_session_without_orphans():
    async def scenario():
        key = SessionKey(10, 1)
        other_key = SessionKey(20, 2)
        started = {key: asyncio.Event(), other_key: asyncio.Event()}
        release_other = asyncio.Event()

        async def execute(effect):
            started[effect.session_key].set()
            if effect.session_key == other_key:
                await release_other.wait()
            else:
                await asyncio.Event().wait()
            return True

        runner = EffectRunner(execute)
        owned_run = asyncio.create_task(runner.run((make_effect(key=key),)))
        other_run = asyncio.create_task(
            runner.run((make_effect("notify:20:2", other_key),))
        )
        await asyncio.gather(*(event.wait() for event in started.values()))

        await runner.cancel_and_drain(key)
        owned_outcome = (await owned_run)[0]
        other_still_pending = runner.registry.pending_for(other_key)
        release_other.set()
        other_outcome = (await other_run)[0]
        return runner, owned_outcome, other_outcome, other_still_pending

    runner, owned, other, other_still_pending = asyncio.run(scenario())
    assert owned.ok is False and owned.cancelled is True
    assert other_still_pending == 1
    assert other.ok is True
    assert runner.pending_count == 0


def test_registry_consumes_background_exception_and_records_sanitized_failure():
    async def scenario():
        registry = TaskRegistry()
        key = SessionKey(30, 3)

        async def explode():
            raise ValueError("sensitive payload")

        registry.create_task(key, explode(), name="background-delivery")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return registry

    registry = asyncio.run(scenario())
    assert registry.pending_count == 0
    assert len(registry.failures) == 1
    assert registry.failures[0].session_key == SessionKey(30, 3)
    assert registry.failures[0].task_name == "background-delivery"
    assert registry.failures[0].exception_type == "ValueError"
    assert "sensitive payload" not in repr(registry.failures)


def test_shutdown_cancels_all_sessions_and_rejects_new_effects():
    async def scenario():
        started = [asyncio.Event(), asyncio.Event()]

        async def execute(effect):
            started[effect.session_key.group_chat_id - 1].set()
            await asyncio.Event().wait()

        runner = EffectRunner(execute)
        runs = [
            asyncio.create_task(
                runner.run((make_effect(f"effect-{group}", SessionKey(group, 1)),))
            )
            for group in (1, 2)
        ]
        await asyncio.gather(*(event.wait() for event in started))
        await runner.shutdown()
        outcomes = [outcome for run in runs for outcome in await run]
        with pytest.raises(RuntimeError, match="shut down"):
            await runner.run((make_effect("late", SessionKey(3, 1)),))
        await runner.shutdown()  # idempotent
        return runner, outcomes

    runner, outcomes = asyncio.run(scenario())
    assert all(outcome.cancelled for outcome in outcomes)
    assert runner.pending_count == 0
    assert runner.registry.accepting is False
