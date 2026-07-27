"""Safe ballot opening and vote recording tests for task 3.11.

Property 7: Voting Safety — a generation-bound ballot records one vote only.
**Validates: Requirements 2.8, 2.9, 2.16, 3.5**
"""

from __future__ import annotations

import asyncio

import pytest
from hypothesis import given, settings, strategies as st

from photo_guess_game.callback_codec import CallbackPayload, decode_callback
from photo_guess_game.models import GameState, RoundPhase, SessionKey
from photo_guess_game.session_manager import (
    OpenBallotEvent,
    SessionManager,
    VoteEvent,
)
from photo_guess_game.session_store import SessionStore


def transact(store, group_id, operation):
    return asyncio.run(store.transact(group_id, operation))


def make_discussion(player_count=3, *, group_id=12001):
    store = SessionStore()
    manager = SessionManager(store)
    manager.create_session(group_id, 1, "P1")
    for user_id in range(2, player_count + 1):
        manager.join_session(group_id, user_id, f"P{user_id}")
    session = store.get(group_id)
    session.state = GameState.GUESSING
    session.round_phase = RoundPhase.DISCUSSION
    store.put(session)
    return store, manager, session.session_key


def open_ballot(store, manager, key, requester_id=1):
    return transact(
        store,
        key.group_chat_id,
        lambda view: manager.open_ballot(view, OpenBallotEvent(key, requester_id)),
    )


def cast_vote(store, manager, key, ballot_id, voter_id, target_id):
    return transact(
        store,
        key.group_chat_id,
        lambda view: manager.cast_vote(
            view, VoteEvent(key, ballot_id, voter_id, target_id)
        ),
    )


def ballot_snapshot(session):
    ballot = session.ballot
    return (
        session.revision,
        session.phase,
        session.ballot_sequence,
        None
        if ballot is None
        else (
            ballot.ballot_id,
            ballot.open,
            ballot.eligible_voters,
            ballot.eligible_targets,
            dict(ballot.votes),
        ),
        dict(session.votes),
    )


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("non_host", "not_host"),
        ("inactive_host", "inactive_host"),
        ("wrong_phase", "not_in_discussion"),
        ("stale_generation", "stale_generation"),
    ],
)
def test_open_ballot_actor_phase_and_generation_matrix(case, expected_reason):
    store, manager, key = make_discussion()
    session = store.get(key.group_chat_id)
    requester = 1
    event_key = key
    if case == "non_host":
        requester = 2
    elif case == "inactive_host":
        session.players[1].active = False
    elif case == "wrong_phase":
        session.round_phase = RoundPhase.DELIVERING
    else:
        event_key = SessionKey(key.group_chat_id, key.generation + 10_000)
    before = ballot_snapshot(session)

    result = open_ballot(store, manager, event_key, requester)

    assert not result.ok
    assert result.reason == expected_reason
    assert ballot_snapshot(store.get(key.group_chat_id)) == before


def test_open_ballot_snapshots_active_eligibility_and_generation_bound_buttons():
    store, manager, key = make_discussion(player_count=4)
    store.get(key.group_chat_id).players[4].active = False

    result = open_ballot(store, manager, key)

    assert result.ok
    session = store.get(key.group_chat_id)
    assert session.phase == RoundPhase.VOTING
    assert session.ballot.ballot_id == 1
    assert session.ballot.eligible_voters == frozenset({1, 2, 3})
    assert session.ballot.eligible_targets == frozenset({1, 2, 3})
    callbacks = [
        button["callback_data"]
        for row in result.effects[0].payload.buttons
        for button in row
    ]
    decoded = [decode_callback(value) for value in callbacks]
    assert all(isinstance(value, CallbackPayload) for value in decoded)
    assert {(value.generation, value.phase_or_ballot, value.arg) for value in decoded} == {
        (key.generation, 1, 1),
        (key.generation, 1, 2),
        (key.generation, 1, 3),
    }


def test_repeated_open_preserves_ballot_id_votes_and_revision():
    store, manager, key = make_discussion()
    assert open_ballot(store, manager, key).ok
    assert cast_vote(store, manager, key, 1, 1, 2).ok
    before = ballot_snapshot(store.get(key.group_chat_id))

    repeated = open_ballot(store, manager, key)

    assert not repeated.ok
    assert repeated.reason == "ballot_already_open"
    assert repeated.effects == ()
    assert ballot_snapshot(store.get(key.group_chat_id)) == before


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("wrong_phase", "not_in_voting"),
        ("closed", "ballot_not_open"),
        ("stale_generation", "stale_generation"),
        ("stale_ballot", "stale_ballot"),
        ("ineligible_voter", "ineligible_voter"),
        ("ineligible_target", "ineligible_target"),
        ("duplicate", "already_voted"),
    ],
)
def test_cast_vote_phase_generation_ballot_and_eligibility_matrix(
    case, expected_reason
):
    store, manager, key = make_discussion()
    assert open_ballot(store, manager, key).ok
    session = store.get(key.group_chat_id)
    event_key = key
    ballot_id = session.ballot.ballot_id
    voter_id, target_id = 1, 2
    if case == "wrong_phase":
        session.round_phase = RoundPhase.DISCUSSION
    elif case == "closed":
        session.ballot.open = False
    elif case == "stale_generation":
        event_key = SessionKey(key.group_chat_id, key.generation + 10_000)
    elif case == "stale_ballot":
        ballot_id += 1
    elif case == "ineligible_voter":
        voter_id = 99
    elif case == "ineligible_target":
        target_id = 99
    else:
        assert cast_vote(store, manager, key, ballot_id, voter_id, target_id).ok
    before = ballot_snapshot(session)

    result = cast_vote(
        store, manager, event_key, ballot_id, voter_id, target_id
    )

    assert not result.ok
    assert result.reason == expected_reason
    assert result.effects == ()
    assert ballot_snapshot(store.get(key.group_chat_id)) == before


def test_valid_vote_records_once_and_reports_snapshot_electorate_progress():
    store, manager, key = make_discussion(player_count=4)
    store.get(key.group_chat_id).players[4].active = False
    assert open_ballot(store, manager, key).ok

    result = cast_vote(store, manager, key, 1, 2, 3)

    assert result.ok
    session = store.get(key.group_chat_id)
    assert session.ballot.votes == {2: 3}
    assert session.votes == {2: 3}
    assert len(result.effects) == 1
    progress = result.effects[0].payload
    assert "P2" in progress.text
    assert "(1/3 أصوات)" in progress.text
    assert session.phase == RoundPhase.VOTING


def test_eligibility_change_closes_and_clears_ballot_then_allows_fresh_open():
    store, manager, key = make_discussion(player_count=3)
    assert open_ballot(store, manager, key).ok
    assert cast_vote(store, manager, key, 1, 1, 2).ok

    left = manager.leave_session(key.group_chat_id, 3)

    assert left.ok
    session = store.get(key.group_chat_id)
    assert session.phase == RoundPhase.DISCUSSION
    assert session.ballot.ballot_id == 1
    assert not session.ballot.open
    assert session.ballot.votes == {}
    assert session.votes == {}

    reopened = open_ballot(store, manager, key)
    assert reopened.ok
    session = store.get(key.group_chat_id)
    assert session.ballot.ballot_id == 2
    assert session.ballot.eligible_voters == frozenset({1, 2})


def test_vote_detects_out_of_band_eligibility_change_and_requests_new_ballot():
    store, manager, key = make_discussion(player_count=3)
    assert open_ballot(store, manager, key).ok
    assert cast_vote(store, manager, key, 1, 1, 2).ok
    store.get(key.group_chat_id).players[3].active = False

    result = cast_vote(store, manager, key, 1, 2, 1)

    assert not result.ok
    assert result.reason == "eligibility_changed"
    assert len(result.effects) == 1
    assert "اقتراع جديد" in result.effects[0].payload.text
    session = store.get(key.group_chat_id)
    assert session.phase == RoundPhase.DISCUSSION
    assert not session.ballot.open
    assert session.ballot.votes == {}
    assert session.votes == {}


@settings(max_examples=25, deadline=None, database=None)
@given(
    player_count=st.integers(min_value=2, max_value=8),
    voter_seed=st.integers(min_value=0, max_value=1000),
    target_seed=st.integers(min_value=0, max_value=1000),
)
def test_property_7_only_first_valid_vote_changes_tally(
    player_count, voter_seed, target_seed
):
    """Property 7: one current eligible vote is accepted exactly once.

    **Validates: Requirements 2.8, 2.9**
    """
    store, manager, key = make_discussion(
        player_count=player_count, group_id=12100 + player_count
    )
    opened = open_ballot(store, manager, key)
    assert opened.ok
    voter_id = voter_seed % player_count + 1
    target_id = target_seed % player_count + 1

    first = cast_vote(store, manager, key, 1, voter_id, target_id)
    after_first = ballot_snapshot(store.get(key.group_chat_id))
    duplicate = cast_vote(store, manager, key, 1, voter_id, target_id)

    assert first.ok
    assert not duplicate.ok
    assert duplicate.reason == "already_voted"
    assert ballot_snapshot(store.get(key.group_chat_id)) == after_first
    assert store.get(key.group_chat_id).ballot.votes == {voter_id: target_id}
