"""Atomic SessionStore transaction and lock lifecycle tests.

**Validates: Requirements 2.2, 2.3, 2.16, 3.2**
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings, strategies as st

from photo_guess_game.models import GameSession, GameState, Player
from photo_guess_game.session_store import SessionStore, StoreView


def make_session(group_id: int = 100, user_ids: tuple[int, ...] = (1,)) -> GameSession:
    return GameSession(
        group_chat_id=group_id,
        host_id=user_ids[0],
        state=GameState.LOBBY,
        players={user_id: Player(user_id, f"Player {user_id}") for user_id in user_ids},
    )


def test_transact_scopes_mutation_and_refreshes_membership_atomically():
    async def scenario():
        store = SessionStore()
        session = make_session(user_ids=(1, 2))

        def create(view: StoreView):
            assert view.group_chat_id == 100
            assert view.lock_held is True
            assert view.get() is None
            view.put(session)
            return view.get()

        result = await store.transact(100, create)
        return store, session, result

    store, session, result = asyncio.run(scenario())
    assert result is session
    assert store.get(100) is session
    assert store.group_chat_ids_for_user(1) == frozenset({100})
    assert store.group_chat_ids_for_user(2) == frozenset({100})
    assert store.lock_slot_snapshot(100).refs == 0

def test_transact_rejects_awaitable_operation_and_releases_lock():
    async def scenario():
        traces = []
        store = SessionStore(transaction_observer=traces.append)

        async def forbidden_operation(view: StoreView):
            return view.get()

        with pytest.raises(TypeError, match="must be synchronous"):
            await store.transact(100, forbidden_operation)

        result = await store.transact(100, lambda view: "usable")
        return store, traces, result

    store, traces, result = asyncio.run(scenario())
    assert result == "usable"
    assert [trace.outcome for trace in traces] == [
        "awaitable_rejected",
        "returned",
    ]
    assert store.lock_slot_count == 0


def test_operation_exception_invalidates_view_and_does_not_strand_lock():
    async def scenario():
        store = SessionStore()
        escaped_view = None

        def explode(view: StoreView):
            nonlocal escaped_view
            escaped_view = view
            raise LookupError("decision failed")

        with pytest.raises(LookupError, match="decision failed"):
            await store.transact(100, explode)

        assert escaped_view is not None
        with pytest.raises(RuntimeError, match="no longer active"):
            escaped_view.get()
        return store, await store.transact(100, lambda view: view.get())

    store, result = asyncio.run(scenario())
    assert result is None
    assert store.lock_slot_count == 0


def test_store_view_rejects_cross_group_writes_without_mutation():
    async def scenario():
        store = SessionStore()
        wrong_group = make_session(group_id=200)
        with pytest.raises(ValueError, match="another group"):
            await store.transact(100, lambda view: view.put(wrong_group))
        return store

    store = asyncio.run(scenario())
    assert store.get(100) is None
    assert store.get(200) is None
    assert store.lock_slot_count == 0


def test_lookup_acquire_retire_race_keeps_slot_until_holder_and_waiter_exit():
    async def scenario():
        store = SessionStore()
        session = make_session()
        store.put(session)
        holder = store.lock_for(100)
        await holder.acquire()

        waiter = asyncio.create_task(store.transact(100, lambda view: view.get()))
        await asyncio.sleep(0)  # let the waiter reference the slot and block
        before_remove = store.lock_slot_snapshot(100)
        removed = store.remove(100)
        after_remove = store.lock_slot_snapshot(100)

        holder.release()
        waiter_result = await waiter
        return store, removed, waiter_result, before_remove, after_remove

    store, removed, waiter_result, before_remove, after_remove = asyncio.run(scenario())
    assert removed is not None
    assert waiter_result is None
    assert before_remove.locked is True
    assert before_remove.refs == 2  # one holder and one waiter
    assert after_remove.retire_requested is True
    assert after_remove.refs == 2
    assert store.lock_slot_count == 0

def test_legacy_lock_interface_uses_same_slot_and_preserves_existing_callers():
    async def scenario():
        store = SessionStore()
        store.put(make_session())
        lock = store.lock_for(100)
        async with lock:
            inside = (lock.locked(), store.lock_slot_snapshot(100))
        outside = (lock.locked(), store.lock_slot_snapshot(100))
        store.remove(100)
        return store, inside, outside

    store, inside, outside = asyncio.run(scenario())
    assert inside[0] is True
    assert inside[1].locked is True
    assert inside[1].refs == 1
    assert outside[0] is False
    assert outside[1].refs == 0
    assert store.lock_slot_count == 0


def test_transaction_instrumentation_records_short_synchronous_decision():
    traces = []

    async def scenario():
        store = SessionStore(transaction_observer=traces.append)
        return await store.transact(55, lambda view: (view.lock_held, "done"))

    assert asyncio.run(scenario()) == (True, "done")
    assert len(traces) == 1
    trace = traces[0]
    assert trace.group_chat_id == 55
    assert trace.outcome == "returned"
    assert trace.lock_held_during_operation is True
    assert trace.wait_seconds >= 0
    assert trace.operation_seconds >= 0


@settings(max_examples=30, deadline=None, database=None)
@given(
    rosters=st.lists(
        st.sets(st.integers(min_value=1, max_value=20), min_size=1, max_size=8),
        min_size=1,
        max_size=12,
    )
)
def test_property_sequential_transact_preserves_legacy_store_results(rosters):
    """For sequential valid writes, transact matches legacy observable state.

    **Validates: Requirements 3.2**
    """

    async def scenario():
        legacy = SessionStore()
        transactional = SessionStore()

        for roster in rosters:
            ordered = tuple(sorted(roster))

            legacy_session = legacy.get(100)
            if legacy_session is None:
                legacy_session = make_session(user_ids=ordered)
            else:
                legacy_session.host_id = ordered[0]
                legacy_session.players = {
                    user_id: Player(user_id, f"Player {user_id}")
                    for user_id in ordered
                }
            legacy.put(legacy_session)

            def update(view: StoreView, ids=ordered):
                session = view.get()
                if session is None:
                    session = make_session(user_ids=ids)
                else:
                    session.host_id = ids[0]
                    session.players = {
                        user_id: Player(user_id, f"Player {user_id}")
                        for user_id in ids
                    }
                view.put(session)

            await transactional.transact(100, update)

        return legacy, transactional

    legacy, transactional = asyncio.run(scenario())
    legacy_session = legacy.get(100)
    transactional_session = transactional.get(100)
    assert tuple(legacy_session.players) == tuple(transactional_session.players)
    for user_id in range(1, 21):
        assert legacy.group_chat_ids_for_user(user_id) == (
            transactional.group_chat_ids_for_user(user_id)
        )


@settings(max_examples=30, deadline=None, database=None)
@given(deltas=st.lists(st.integers(min_value=-20, max_value=20), min_size=2, max_size=20))
def test_property_concurrent_group_transactions_have_no_lost_updates(deltas):
    """Property 3: Atomicity — competing group decisions are linearizable.

    **Validates: Requirements 2.2, 2.16**
    """

    async def scenario():
        store = SessionStore()
        session = make_session()
        session.labels["total"] = 0
        store.put(session)

        def add(view: StoreView, delta: int) -> int:
            current = view.get()
            current.labels["total"] += delta
            view.put(current)
            return current.labels["total"]

        tasks = [
            asyncio.create_task(
                store.transact(100, lambda view, delta=delta: add(view, delta))
            )
            for delta in deltas
        ]
        results = await asyncio.gather(*tasks)
        return store, results

    store, results = asyncio.run(scenario())
    assert store.get(100).labels["total"] == sum(deltas)
    assert len(results) == len(deltas)
    assert store.lock_slot_snapshot(100).refs == 0
