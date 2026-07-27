"""Task 3.4 tests for generation, membership, and bounded replay metadata.

**Validates: Requirements 2.1, 2.3, 2.4, 2.13, 2.16, 3.1, 3.4, 3.11, 3.12**
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings, strategies as st

from photo_guess_game.models import (
    Effect,
    GameSession,
    GameState,
    Player,
    SessionKey,
    TransitionResult,
)
from photo_guess_game.session_manager import SessionManager
from photo_guess_game.session_store import SessionStore, StaleGenerationError


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_session(group_id: int, *user_ids: int) -> GameSession:
    return GameSession(
        group_chat_id=group_id,
        host_id=user_ids[0],
        state=GameState.LOBBY,
        players={user_id: Player(user_id, str(user_id)) for user_id in user_ids},
    )


def terminate_and_remove(store: SessionStore, group_id: int) -> SessionKey:
    session = store.get(group_id)
    key = session.session_key
    session.mark_terminal(GameState.COMPLETED)
    store.put(session)
    store.remove(group_id)
    return key


def test_old_lobby_is_never_replaced_by_create_session():
    store = SessionStore()
    manager = SessionManager(store)
    first = manager.create_session(10, 1, "Alice")
    first.session.created_at = 0.0
    before = (first.session.session_key, tuple(first.session.players))

    duplicate = manager.create_session(10, 2, "Bob")

    assert duplicate.ok is False
    assert duplicate.reason == "session_already_active"
    assert duplicate.session is first.session
    assert (store.get(10).session_key, tuple(store.get(10).players)) == before

def test_generation_is_global_and_create_after_explicit_terminal_cleanup_is_fresh():
    left = SessionStore()
    right = SessionStore()
    old = make_session(10, 1)
    other = make_session(20, 2)
    old_key = left.create(old)
    other_key = right.create(other)

    assert other_key.generation > old_key.generation
    terminate_and_remove(left, 10)
    replacement = make_session(10, 3)
    replacement_key = left.create(replacement)

    assert replacement_key.generation > other_key.generation
    assert replacement_key.group_chat_id == old_key.group_chat_id
    tombstone = left.tombstone_for(old_key)
    assert tombstone is not None
    assert tombstone.session_key == old_key
    assert "secret" not in repr(tombstone.outcome).lower()


def test_membership_index_tracks_exact_session_keys_across_groups():
    store = SessionStore()
    first = make_session(10, 1, 2)
    second = make_session(20, 1, 3)
    first_key = store.create(first)
    second_key = store.create(second)

    assert store.active_session_keys_for_user(1) == frozenset(
        {first_key, second_key}
    )
    assert store.group_chat_ids_for_user(1) == frozenset({10, 20})

    first.players[1].active = False
    store.put(first)
    assert store.active_session_keys_for_user(1) == frozenset({second_key})
    assert store.active_session_keys_for_user(2) == frozenset({first_key})


def test_stale_generation_cannot_be_read_or_committed_after_recreation():
    store = SessionStore()
    old_key = store.create(make_session(10, 1))
    terminate_and_remove(store, 10)
    current_key = store.create(make_session(10, 2))

    assert current_key != old_key
    assert store.get_for_key(old_key) is None
    assert store.get_for_key(current_key) is store.get(10)
    with pytest.raises(StaleGenerationError, match="stale_generation"):
        store.commit_update(old_key, "late-callback", "must-not-commit")
    assert store.processed_update_outcome(old_key, "late-callback") is None


def test_duplicate_update_replays_outcome_without_mutation_or_effects():
    async def scenario():
        store = SessionStore()
        session = make_session(10, 1)
        key = store.create(session)
        effect = Effect("notify-1", key, 1, "telegram", {"text": "done"})
        calls = 0

        def decide():
            nonlocal calls
            calls += 1
            session.labels["commits"] = calls
            return TransitionResult(True, None, key, 1, (effect,))

        first = await store.transact(
            10, lambda view: view.apply_update_once(key, "update-7", decide)
        )
        duplicate = await store.transact(
            10,
            lambda view: view.apply_update_once(
                key,
                "update-7",
                lambda: pytest.fail("duplicate operation was executed"),
            ),
        )
        return session, calls, first, duplicate

    session, calls, first, duplicate = asyncio.run(scenario())
    assert calls == 1
    assert session.labels == {"commits": 1}
    assert first.duplicate is False
    assert first.outcome.effects
    assert duplicate.duplicate is True
    assert duplicate.outcome.effects == ()
    assert duplicate.outcome.committed_revision == first.outcome.committed_revision


def test_tombstone_and_idempotency_ledgers_are_lru_and_ttl_bounded():
    clock = FakeClock()
    store = SessionStore(
        clock=clock,
        max_tombstones=2,
        max_processed_updates=2,
        max_processed_effects=2,
        tombstone_ttl_seconds=10,
        idempotency_ttl_seconds=10,
    )

    removed_keys = []
    for group_id in (10, 20, 30):
        removed_keys.append(store.create(make_session(group_id, group_id)))
        terminate_and_remove(store, group_id)
    assert store.reliability_resource_snapshot.tombstones == 2
    assert store.tombstone_for(removed_keys[0]) is None

    active_key = store.create(make_session(40, 1))
    store.commit_update(active_key, "u1", "one")
    store.commit_update(active_key, "u2", "two")
    assert store.processed_update_outcome(active_key, "u1") == "one"
    store.commit_update(active_key, "u3", "three")
    assert store.processed_update_outcome(active_key, "u2") is None
    assert store.reliability_resource_snapshot.processed_updates == 2

    store.commit_effect("e1", "sent")
    store.commit_effect("e2", "sent")
    store.commit_effect("e3", "sent")
    assert store.processed_effect_outcome("e1") is None
    assert store.reliability_resource_snapshot.processed_effects == 2

    clock.advance(11)
    first_sweep = store.cleanup_expired(limit=3)
    assert first_sweep.total_removed == 3
    resources = store.reliability_resource_snapshot
    assert resources.tombstones + resources.processed_updates + resources.processed_effects == 3
    second_sweep = store.cleanup_expired(limit=3)
    assert second_sweep.total_removed == 3
    assert store.reliability_resource_snapshot.tombstones == 0
    assert store.reliability_resource_snapshot.processed_updates == 0
    assert store.reliability_resource_snapshot.processed_effects == 0


@settings(max_examples=30, deadline=None, database=None)
@given(
    group_ids=st.lists(
        st.integers(min_value=-10_000, max_value=10_000),
        min_size=1,
        max_size=20,
        unique=True,
    )
)
def test_property_global_generations_and_multigroup_membership(group_ids):
    """Every accepted create has a unique increasing generation and exact membership.

    **Validates: Requirements 2.1, 2.13, 3.4, 3.11**
    """
    store = SessionStore()
    keys = [store.create(make_session(group_id, 7)) for group_id in group_ids]

    generations = [key.generation for key in keys]
    assert generations == sorted(generations)
    assert len(generations) == len(set(generations))
    assert store.active_session_keys_for_user(7) == frozenset(keys)
