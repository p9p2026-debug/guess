"""Task 3.12 ballot resolution and one-shot spy guess tests.

**Validates: Requirements 2.10, 2.11, 2.12, 3.6**
"""

from __future__ import annotations

import asyncio

import pytest

from photo_guess_game.callback_codec import CallbackPayload, decode_callback
from photo_guess_game.models import GameState, RoundPhase, SessionKey
from photo_guess_game.session_manager import (
    OpenBallotEvent,
    SessionManager,
    SpyGuessEvent,
    VoteEvent,
)
from photo_guess_game.session_store import SessionStore


OPTIONS = ["مستشفى", "مطار", "مدرسة"]


def transact(store, group_id, operation):
    return asyncio.run(store.transact(group_id, operation))


def make_voting(player_count=3, *, group_id=13001):
    store = SessionStore()
    manager = SessionManager(store, spy_option_selector=lambda _secret: list(OPTIONS))
    manager.create_session(group_id, 1, "P1")
    for user_id in range(2, player_count + 1):
        manager.join_session(group_id, user_id, f"P{user_id}")
    session = store.get(group_id)
    session.state = GameState.GUESSING
    session.round_phase = RoundPhase.DISCUSSION
    session.spy_user_id = 1
    session.players[1].is_spy = True
    session.secret_location_name = "🏥 مستشفى"
    session.secret_location_word = OPTIONS[0]
    store.put(session)
    key = session.session_key
    opened = transact(
        store,
        group_id,
        lambda view: manager.open_ballot(view, OpenBallotEvent(key, 1)),
    )
    assert opened.ok
    return store, manager, key

def cast(store, manager, key, voter_id, target_id):
    ballot_id = store.get(key.group_chat_id).ballot.ballot_id
    return transact(
        store,
        key.group_chat_id,
        lambda view: manager.cast_vote(
            view, VoteEvent(key, ballot_id, voter_id, target_id)
        ),
    )


def submit(store, manager, event):
    return transact(
        store,
        event.session_key.group_chat_id,
        lambda view: manager.submit_spy_guess(view, event),
    )


def state_snapshot(session):
    ballot = session.ballot
    return (
        session.revision,
        session.state,
        session.phase,
        session.terminal,
        session.spy_user_id,
        session.secret_location_name,
        session.secret_location_word,
        session.spy_guess_options,
        session.spy_guess_used,
        tuple((uid, player.active, player.is_spy) for uid, player in session.players.items()),
        None
        if ballot is None
        else (
            ballot.ballot_id,
            ballot.open,
            ballot.eligible_voters,
            ballot.eligible_targets,
            tuple(sorted(ballot.votes.items())),
        ),
    )


def enter_spy_guess(store, manager, key):
    assert cast(store, manager, key, 1, 1).ok
    assert cast(store, manager, key, 2, 1).ok
    result = cast(store, manager, key, 3, 2)
    assert result.ok and result.reason == "spy_identified"
    return result


def test_incomplete_eligible_ballot_only_reports_progress_without_resolution():
    store, manager, key = make_voting()

    assert cast(store, manager, key, 1, 1).ok
    result = cast(store, manager, key, 2, 1)

    session = store.get(key.group_chat_id)
    assert result.ok and result.reason is None
    assert session.phase == RoundPhase.VOTING
    assert session.ballot.open is True
    assert session.ballot.votes == {1: 1, 2: 1}
    assert session.spy_guess_options == ()

def test_tie_closes_without_elimination_and_host_can_open_fresh_ballot():
    store, manager, key = make_voting(player_count=4)
    assert cast(store, manager, key, 1, 1).ok
    assert cast(store, manager, key, 2, 1).ok
    assert cast(store, manager, key, 3, 2).ok

    result = cast(store, manager, key, 4, 2)

    session = store.get(key.group_chat_id)
    assert result.ok and result.reason == "tie"
    assert session.phase == RoundPhase.DISCUSSION
    assert session.ballot.open is False
    assert all(player.active for player in session.players.values())
    assert "تعادل" in result.effects[-1].payload.text

    reopened = transact(
        store,
        key.group_chat_id,
        lambda view: manager.open_ballot(view, OpenBallotEvent(key, 1)),
    )
    assert reopened.ok
    assert session.ballot.ballot_id == 2
    assert session.ballot.open is True
    assert session.ballot.votes == {}


def test_invalid_stored_target_closes_safely_without_indexing_or_terminal_result():
    store, manager, key = make_voting()
    session = store.get(key.group_chat_id)
    session.ballot.votes[1] = 999_999

    result = cast(store, manager, key, 2, 1)

    assert result.ok and result.reason == "invalid_ballot"
    assert store.get(key.group_chat_id) is session
    assert session.phase == RoundPhase.DISCUSSION
    assert session.ballot.open is False
    assert session.ballot.votes == {}
    assert all(player.active for player in session.players.values())
    assert "هدف غير صالح" in result.effects[0].payload.text


def test_unique_citizen_result_awards_spy_then_terminal_scrubs():
    store, manager, key = make_voting()
    retained = store.get(key.group_chat_id)
    assert cast(store, manager, key, 1, 2).ok
    assert cast(store, manager, key, 2, 2).ok

    result = cast(store, manager, key, 3, 1)

    assert result.ok and result.reason == "citizen_eliminated"
    assert result.public_snapshot.winner == "spy"
    assert result.public_snapshot.state == GameState.COMPLETED
    assert store.get(key.group_chat_id) is None
    assert store.tombstone_for(key).outcome == result.public_snapshot
    assert retained.ballot is None
    assert retained.spy_user_id is None
    assert retained.secret_location_word is None
    assert retained.spy_guess_options == ()
    assert all(not player.active and not player.is_spy for player in retained.players.values())
    assert tuple(effect.kind for effect in result.effects[-2:]) == (
        "cancel_timer",
        "cancel_tasks",
    )

def test_unique_spy_result_opens_fixed_generation_bound_guess_opportunity():
    store, manager, key = make_voting()

    result = enter_spy_guess(store, manager, key)

    session = store.get(key.group_chat_id)
    assert session.phase == RoundPhase.SPY_GUESS
    assert session.ballot.open is False
    assert session.spy_guess_options == tuple(OPTIONS)
    assert session.spy_guess_used is False
    assert all(player.active for player in session.players.values())
    buttons = result.effects[-1].payload.buttons
    payloads = [decode_callback(button["callback_data"]) for row in buttons for button in row]
    assert all(isinstance(payload, CallbackPayload) for payload in payloads)
    assert [(payload.generation, payload.action, payload.arg) for payload in payloads] == [
        (key.generation, "sg", 0),
        (key.generation, "sg", 1),
        (key.generation, "sg", 2),
    ]


@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("wrong_actor", "not_spy"),
        ("inactive_spy", "inactive_spy"),
        ("wrong_phase", "not_in_spy_guess"),
        ("invalid_option", "invalid_guess_option"),
        ("stale_generation", "stale_generation"),
        ("repeat", "spy_guess_already_used"),
    ],
)
def test_submit_spy_guess_authorization_phase_option_generation_once_matrix(case, reason):
    store, manager, key = make_voting(group_id=13100)
    enter_spy_guess(store, manager, key)
    session = store.get(key.group_chat_id)
    actor_id = 1
    choice = OPTIONS[0]
    event_key = key
    if case == "wrong_actor":
        actor_id = 2
    elif case == "inactive_spy":
        session.players[1].active = False
    elif case == "wrong_phase":
        session.round_phase = RoundPhase.DISCUSSION
    elif case == "invalid_option":
        choice = "ليست ضمن الخيارات"
    elif case == "stale_generation":
        event_key = SessionKey(key.group_chat_id, key.generation + 1)
    else:
        session.spy_guess_used = True
    before = state_snapshot(session)

    result = submit(store, manager, SpyGuessEvent(event_key, actor_id, choice))

    assert not result.ok
    assert result.reason == reason
    assert result.effects == ()
    assert state_snapshot(store.get(key.group_chat_id)) == before

@pytest.mark.parametrize(
    ("choice", "winner", "reason"),
    [
        (OPTIONS[0], "spy", "spy_guess_correct"),
        (OPTIONS[1], "citizens", "spy_guess_incorrect"),
    ],
)
def test_authorized_spy_guess_terminates_once_with_winner_and_scrub(
    choice, winner, reason
):
    store, manager, key = make_voting(group_id=13200)
    enter_spy_guess(store, manager, key)
    retained = store.get(key.group_chat_id)

    result = submit(store, manager, SpyGuessEvent(key, 1, choice))

    assert result.ok and result.reason == reason
    assert result.public_snapshot.winner == winner
    assert result.public_snapshot.state == GameState.COMPLETED
    assert store.get(key.group_chat_id) is None
    assert retained.ballot is None
    assert retained.spy_user_id is None
    assert retained.secret_location_name is None
    assert retained.secret_location_word is None
    assert retained.spy_guess_options == ()
    assert retained.spy_guess_used is False
    assert retained.votes == {}
    assert all(not player.active and not player.is_spy for player in retained.players.values())
    assert tuple(effect.kind for effect in result.effects) == (
        "telegram",
        "cancel_timer",
        "cancel_tasks",
    )

    repeated = submit(store, manager, SpyGuessEvent(key, 1, choice))
    assert not repeated.ok
    assert repeated.reason == "stale_generation"
    assert repeated.effects == ()
    assert store.tombstone_for(key).outcome == result.public_snapshot
