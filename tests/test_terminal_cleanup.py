"""Task 3.5 terminal scrub, atomic removal, and bounded cleanup tests.

**Validates: Requirements 2.3, 2.12, 2.13, 3.7, 3.12**
"""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError

import pytest

from photo_guess_game.models import (
    Ballot,
    DeliveryEntry,
    GameSession,
    GameState,
    Player,
    RoleAssignment,
    RoundPhase,
    SessionResultSnapshot,
)
from photo_guess_game.session_store import SessionStore


class FakeClock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now


def make_secret_session(group_id: int = 100) -> GameSession:
    session = GameSession(
        group_chat_id=group_id,
        host_id=1,
        state=GameState.GUESSING,
        players={
            1: Player(1, "Alice", "photo-a", "secret-a", True),
            2: Player(2, "Bob", "photo-b", "secret-b", False),
        },
        round_phase=RoundPhase.VOTING,
    )
    session.labels["A"] = 1
    session.guesses[1] = {"A": 2}
    session.current_turn_user_id = 1
    session.turn_order[:] = [1, 2]
    session.pending_guess_user_id = 2
    session.spy_user_id = 1
    session.secret_location_name = "Secret Hospital"
    session.secret_location_word = "hospital"
    session.votes[1] = 2
    session.delivery[1] = DeliveryEntry(
        RoleAssignment(1, "spy", "private role text")
    )
    session.ballot_sequence = 3
    session.ballot = Ballot(3, frozenset({1, 2}), frozenset({1, 2}), {1: 2})
    session.spy_guess_used = True
    return session


@pytest.mark.parametrize(
    ("terminal_state", "winner", "reason"),
    [
        (GameState.COMPLETED, "citizens", "vote_resolved"),
        (GameState.COMPLETED, "spy", "spy_guess_resolved"),
        (GameState.CANCELLED, None, "host_cancelled"),
    ],
)
def test_commit_terminal_preserves_public_result_then_scrubs_every_private_field(
    terminal_state, winner, reason
):
    async def scenario():
        store = SessionStore()
        retained = make_secret_session()
        key = store.create(retained)
        result = await store.commit_terminal(
            key, terminal_state, winner=winner, reason=reason
        )
        return store, retained, key, result

    store, retained, key, result = asyncio.run(scenario())
    snapshot = result.public_snapshot
    assert isinstance(snapshot, SessionResultSnapshot)
    assert snapshot.session_key == key
    assert snapshot.state == terminal_state
    assert snapshot.terminal is True
    assert snapshot.winner == winner
    assert snapshot.reason == reason
    assert tuple(player.display_name for player in snapshot.players) == ("Alice", "Bob")
    assert all(player.active for player in snapshot.players)
    with pytest.raises(FrozenInstanceError):
        snapshot.reason = "changed"

    assert store.get(key.group_chat_id) is None
    assert store.active_session_keys_for_user(1) == frozenset()
    assert store.active_session_keys_for_user(2) == frozenset()
    assert store.tombstone_for(key).outcome == snapshot
    assert store.lock_slot_count == 0

    assert retained.terminal is True
    assert retained.state == terminal_state
    assert retained.labels == retained.guesses == retained.votes == {}
    assert retained.turn_order == []
    assert retained.current_turn_user_id is None
    assert retained.pending_guess_user_id is None
    assert retained.spy_user_id is None
    assert retained.secret_location_name is None
    assert retained.secret_location_word is None
    assert retained.delivery == {}
    assert retained.ballot is None
    assert retained.ballot_sequence == 0
    assert retained.spy_guess_used is False
    assert all(not player.active for player in retained.players.values())
    assert all(player.photo_file_id is None for player in retained.players.values())
    assert all(player.secret_word is None for player in retained.players.values())
    assert all(not player.is_spy for player in retained.players.values())

    assert tuple(effect.kind for effect in result.effects) == (
        "cancel_timer",
        "cancel_tasks",
    )
    assert all(effect.session_key == key for effect in result.effects)
    assert all(effect.expected_revision == snapshot.revision for effect in result.effects)
    assert "hospital" not in repr(result).lower()


def test_terminal_commit_retires_lock_after_waiter_and_holder_leave():
    async def scenario():
        store = SessionStore()
        session = make_secret_session()
        key = store.create(session)
        holder = store.lock_for(key.group_chat_id)
        await holder.acquire()
        terminal = asyncio.create_task(
            store.commit_terminal(key, GameState.CANCELLED, reason="cancelled")
        )
        await asyncio.sleep(0)
        blocked = store.lock_slot_snapshot(key.group_chat_id)
        holder.release()
        result = await terminal
        return store, blocked, result

    store, blocked, result = asyncio.run(scenario())
    assert blocked.locked is True
    assert blocked.refs == 2
    assert result.ok is True
    assert store.lock_slot_count == 0


def test_hundreds_of_complete_cancel_cycles_keep_resources_bounded():
    async def scenario():
        store = SessionStore(max_tombstones=13, cleanup_batch_limit=5)
        generations = []
        for index in range(400):
            session = make_secret_session(group_id=777)
            key = store.create(session)
            generations.append(key.generation)
            state = GameState.COMPLETED if index % 2 else GameState.CANCELLED
            result = await store.commit_terminal(
                key,
                state,
                winner="spy" if state == GameState.COMPLETED else None,
                reason=f"cycle-{index % 4}",
            )
            assert result.public_snapshot.state == state
            assert store.get(777) is None
            assert store.lock_slot_count == 0
        return store, generations

    store, generations = asyncio.run(scenario())
    resources = store.reliability_resource_snapshot
    assert generations == sorted(generations)
    assert len(generations) == len(set(generations))
    assert resources.active_sessions == 0
    assert resources.membership_links == 0
    assert resources.lock_slots == 0
    assert resources.tombstones <= 13


def test_expired_terminal_context_cleanup_is_clamped_to_fixed_batch_size():
    async def scenario():
        clock = FakeClock()
        store = SessionStore(
            clock=clock,
            max_tombstones=64,
            tombstone_ttl_seconds=1,
            cleanup_batch_limit=7,
        )
        for group_id in range(40):
            session = make_secret_session(group_id)
            key = store.create(session)
            await store.commit_terminal(key, GameState.COMPLETED)
        clock.now += 2
        removals = []
        while store.reliability_resource_snapshot.tombstones:
            removals.append(store.cleanup_expired(limit=10_000).total_removed)
        return store, removals

    store, removals = asyncio.run(scenario())
    assert sum(removals) == 40
    assert max(removals) <= 7
    assert store.reliability_resource_snapshot.tombstones == 0
