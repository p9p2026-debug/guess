"""Task 3.14 transaction/effect boundary and immutable panel tests.

**Validates: Requirements 2.2, 2.4, 2.7, 2.14, 2.15, 2.16, 3.8, 3.9**
"""

from __future__ import annotations

import asyncio

from hypothesis import given, settings, strategies as st

from photo_guess_game.models import Ballot, GameState, RoundPhase
from photo_guess_game.session_store import SessionStore
from photo_guess_game.telegram_adapter import TelegramAdapter


class ProbeTransport:
    def __init__(self, store: SessionStore, *, fail: bool = False) -> None:
        self.store = store
        self.fail = fail
        self.calls: list[tuple[int, str, object]] = []
        self.lock_states: list[bool] = []

    async def send_message(self, target_id, text, reply_markup=None):
        self.lock_states.append(
            any(slot.lock.locked() for slot in self.store._locks.values())
        )
        self.calls.append((target_id, text, reply_markup))
        return {"ok": not self.fail}

    async def send_photo(self, target_id, file_id, text, reply_markup=None):
        return await self.send_message(target_id, text, reply_markup)


def test_commit_precedes_failed_effect_and_transport_never_runs_under_lock():
    async def scenario():
        store = SessionStore()
        transport = ProbeTransport(store, fail=True)
        adapter = TelegramAdapter(store, send_message_fn=transport.send_message)
        result = await adapter.handle_newgame(-2001, 1, "Host", update_id=10)
        return store, transport, result

    store, transport, result = asyncio.run(scenario())
    session = store.get(-2001)
    assert session is not None and session.host_id == 1
    assert not result.ok and result.reason == "effect_failed"
    assert transport.lock_states == [False]
def test_duplicate_update_replays_without_duplicate_mutation_or_effect():
    async def scenario():
        store = SessionStore()
        transport = ProbeTransport(store)
        adapter = TelegramAdapter(store, send_message_fn=transport.send_message)
        await adapter.handle_newgame(-2002, 1, "Host", update_id=1)
        transport.calls.clear()
        first = await adapter.handle_join(-2002, 2, "Player", update_id=2)
        duplicate = await adapter.handle_join(-2002, 2, "Player", update_id=2)
        return store, transport, first, duplicate

    store, transport, first, duplicate = asyncio.run(scenario())
    assert first.ok and duplicate.ok
    assert tuple(store.get(-2002).players) == (1, 2)
    assert len(transport.calls) == 1


def test_stale_generation_and_phase_revision_are_rejected_without_mutation():
    async def scenario():
        store = SessionStore()
        transport = ProbeTransport(store)
        adapter = TelegramAdapter(store, send_message_fn=transport.send_message)
        await adapter.handle_newgame(-2003, 1, "Host")
        await adapter.handle_join(-2003, 2, "P2")
        session = store.get(-2003)
        key = session.session_key
        stale_revision = session.revision
        session.state = GameState.GUESSING
        session.round_phase = RoundPhase.DISCUSSION
        session.revision += 1
        store.put(session)
        transport.calls.clear()
        before = (session.revision, session.phase, session.ballot, dict(session.votes))
        stale_panel = await adapter.handle_start_voting(
            -2003,
            requester_id=1,
            generation=key.generation,
            panel_revision=stale_revision,
        )
        stale_generation = await adapter.handle_start_voting(
            -2003,
            requester_id=1,
            generation=key.generation + 1,
        )
        after = (session.revision, session.phase, session.ballot, dict(session.votes))
        return stale_panel, stale_generation, before, after, transport

    stale_panel, stale_generation, before, after, transport = asyncio.run(scenario())
    assert stale_panel.reason == "stale_panel"
    assert stale_generation.reason == "stale_generation"
    assert before == after
    assert transport.calls == []
def test_status_renders_one_detached_eligible_snapshot():
    async def scenario():
        store = SessionStore()
        transport = ProbeTransport(store)
        adapter = TelegramAdapter(store, send_message_fn=transport.send_message)
        await adapter.handle_newgame(-2004, 1, "Host")
        await adapter.handle_join(-2004, 2, "Active")
        await adapter.handle_join(-2004, 3, "Leaving")
        session = store.get(-2004)
        session.players[3].active = False
        session.revision += 1
        store.put(session)
        await adapter.handle_status(-2004, update_id="status-1")
        return store, transport.calls[-1]

    store, call = asyncio.run(scenario())
    _target, text, reply_markup = call
    assert "(2/15)" in text
    assert "Active" in text and "Leaving" not in text
    callbacks = [
        button["callback_data"]
        for row in reply_markup["inline_keyboard"]
        for button in row
    ]
    generation = store.get(-2004).generation
    revision = store.get(-2004).revision
    assert all(value.startswith("v1|") for value in callbacks)
    assert generation > 0


@settings(max_examples=12, deadline=None, database=None)
@given(active_flags=st.lists(st.booleans(), min_size=2, max_size=8))
def test_property_12_panel_snapshot_remains_detached_from_live_eligibility(active_flags):
    """Property 12: panel candidates/counts come from one immutable snapshot.

    **Validates: Requirements 2.4, 2.14**
    """
    async def scenario():
        store = SessionStore()
        adapter = TelegramAdapter(store)
        await adapter.handle_newgame(-2100, 1, "P1")
        for user_id in range(2, len(active_flags) + 1):
            await adapter.handle_join(-2100, user_id, f"P{user_id}")
        session = store.get(-2100)
        for user_id, active in enumerate(active_flags, start=1):
            session.players[user_id].active = active
        store.put(session)
        snapshot = await store.transact(-2100, adapter._panel_from_view)
        captured = tuple(player.user_id for player in snapshot.eligible_players)
        for player in session.players.values():
            player.active = not player.active
        return snapshot, captured

    snapshot, captured = asyncio.run(scenario())
    expected = tuple(
        user_id for user_id, active in enumerate(active_flags, start=1) if active
    )
    assert captured == expected
    assert tuple(player.user_id for player in snapshot.eligible_players) == expected


def test_stale_ballot_and_opportunity_callbacks_are_harmless():
    async def scenario():
        store = SessionStore()
        adapter = TelegramAdapter(store)
        await adapter.handle_newgame(-2005, 1, "Spy")
        await adapter.handle_join(-2005, 2, "Citizen")
        session = store.get(-2005)
        session.state = GameState.GUESSING
        session.round_phase = RoundPhase.VOTING
        session.spy_user_id = 1
        session.players[1].is_spy = True
        session.ballot = Ballot(7, frozenset({1, 2}), frozenset({1, 2}))
        store.put(session)
        before_vote = dict(session.ballot.votes)
        stale_vote = await adapter.handle_spy_vote(
            -2005, 1, 2,
            generation=session.generation,
            ballot_id=6,
        )
        session.round_phase = RoundPhase.SPY_GUESS
        session.ballot.open = False
        session.spy_guess_options = ("مطار", "مدرسة")
        before_guess = (session.phase, session.spy_guess_used, session.secret_location_word)
        stale_guess = await adapter.handle_spy_guess(
            -2005, 1, "مطار",
            generation=session.generation,
            opportunity_id=6,
        )
        return stale_vote, stale_guess, before_vote, dict(session.ballot.votes), before_guess, session

    stale_vote, stale_guess, before_vote, after_vote, before_guess, session = asyncio.run(scenario())
    assert stale_vote.reason == "stale_ballot"
    assert stale_guess.reason == "stale_opportunity"
    assert before_vote == after_vote == {}
    assert (session.phase, session.spy_guess_used, session.secret_location_word) == before_guess
