"""Task 3.13 tests for generation-safe pending DM attribution.

**Validates: Requirements 2.3, 2.5, 2.13, 3.4, 3.11, 3.12**
"""

from __future__ import annotations

import asyncio

from photo_guess_game.models import GameSession, GameState, Player, SessionKey
from photo_guess_game.photo_distributor import (
    DisambiguationRequired,
    PendingDMResolution,
    PhotoDistributor,
)
from photo_guess_game.session_store import SessionStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def add_session(store: SessionStore, group_id: int, *users: int) -> SessionKey:
    return store.create(
        GameSession(
            group_chat_id=group_id,
            host_id=users[0],
            state=GameState.LOBBY,
            players={user: Player(user, f"user-{user}") for user in users},
        )
    )


async def commit(
    store: SessionStore,
    distributor: PhotoDistributor,
    resolution: PendingDMResolution,
):
    assert resolution.session_key is not None
    return await store.transact(
        resolution.session_key.group_chat_id,
        lambda view: distributor.commit_disambiguated_submission(view, resolution),
    )

def test_multiple_groups_require_explicit_resolution_and_commit() -> None:
    async def scenario():
        store = SessionStore()
        first = add_session(store, 10, 1)
        second = add_session(store, 20, 1)
        distributor = PhotoDistributor(store)

        pending = distributor.submit_photo(1, "photo-A")
        assert isinstance(pending, DisambiguationRequired)
        assert pending.candidate_session_keys == (first, second)
        assert store.get(10).players[1].photo_file_id is None
        assert store.get(20).players[1].photo_file_id is None

        resolution = distributor.resolve_disambiguated_submission(1, second)
        assert resolution.ok
        assert store.get(20).players[1].photo_file_id is None
        result = await commit(store, distributor, resolution)
        return store, distributor, result

    store, distributor, result = asyncio.run(scenario())
    assert result.ok
    assert store.get(10).players[1].photo_file_id is None
    assert store.get(20).players[1].photo_file_id == "photo-A"
    assert distributor.pending_count == 0


def test_generation_is_revalidated_after_resolve_before_commit() -> None:
    async def scenario():
        store = SessionStore()
        old = add_session(store, 10, 1)
        add_session(store, 20, 1)
        distributor = PhotoDistributor(store)
        distributor.submit_photo(1, "old-photo")
        resolution = distributor.resolve_disambiguated_submission(1, old)

        await store.commit_terminal(old, GameState.CANCELLED)
        current = add_session(store, 10, 1)
        result = await commit(store, distributor, resolution)
        return store, old, current, result, distributor

    store, old, current, result, distributor = asyncio.run(scenario())
    assert current != old
    assert result.ok is False and result.reason == "invalid_choice"
    assert store.get(10).players[1].photo_file_id is None
    context = distributor.pending_context_for_user(1)
    assert context is not None
    assert {key.group_chat_id for key in context.candidate_session_keys} == {20}

def test_leave_and_terminal_candidates_are_removed_without_implicit_choice() -> None:
    store = SessionStore()
    first = add_session(store, 10, 1)
    second = add_session(store, 20, 1)
    distributor = PhotoDistributor(store)
    distributor.submit_photo(1, "held-photo")

    left = store.get(10)
    left.players[1].active = False
    store.put(left)
    context = distributor.pending_context_for_user(1)
    assert context is not None
    assert context.candidate_session_keys == frozenset({second})
    assert store.get(20).players[1].photo_file_id is None

    asyncio.run(store.commit_terminal(second, GameState.CANCELLED))
    assert distributor.pending_context_for_user(1) is None
    assert distributor.pending_count == 0
    assert distributor.resolve_disambiguated_submission(1, first).reason == (
        "no_pending_submission"
    )


def test_expired_context_is_deleted_lazily_and_cannot_commit() -> None:
    clock = FakeClock()
    store = SessionStore(clock=clock)
    add_session(store, 10, 1)
    add_session(store, 20, 1)
    distributor = PhotoDistributor(
        store, clock=clock, pending_ttl_seconds=5
    )
    distributor.submit_photo(1, "expired-photo")

    clock.advance(5)
    resolution = distributor.resolve_disambiguated_submission(1, 10)

    assert resolution.ok is False
    assert resolution.reason == "no_pending_submission"
    assert distributor.pending_count == 0
    assert store.get(10).players[1].photo_file_id is None
    assert store.get(20).players[1].photo_file_id is None


def test_expiry_is_rechecked_inside_commit_transaction() -> None:
    async def scenario():
        clock = FakeClock()
        store = SessionStore(clock=clock)
        add_session(store, 10, 1)
        add_session(store, 20, 1)
        distributor = PhotoDistributor(store, clock=clock, pending_ttl_seconds=5)
        distributor.submit_photo(1, "expired-between-steps")
        resolution = distributor.resolve_disambiguated_submission(1, 10)
        clock.advance(5)
        result = await commit(store, distributor, resolution)
        return store, distributor, result

    store, distributor, result = asyncio.run(scenario())
    assert result.ok is False and result.reason == "pending_expired"
    assert distributor.pending_count == 0
    assert store.get(10).players[1].photo_file_id is None

def test_cleanup_sweep_is_clamped_to_configured_batch() -> None:
    clock = FakeClock()
    store = SessionStore(clock=clock)
    users = tuple(range(1, 6))
    add_session(store, 10, *users)
    add_session(store, 20, *users)
    distributor = PhotoDistributor(
        store,
        clock=clock,
        pending_ttl_seconds=1,
        cleanup_batch_limit=2,
    )
    for user in users:
        result = distributor.submit_photo(user, f"photo-{user}")
        assert result.reason == "disambiguation_required"

    clock.advance(2)
    assert distributor.cleanup_expired(limit=10_000) == 2
    assert distributor.pending_count == 3
    assert distributor.cleanup_expired(limit=10_000) == 2
    assert distributor.pending_count == 1
    assert distributor.cleanup_expired(limit=10_000) == 1
    assert distributor.pending_count == 0
