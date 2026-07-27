"""Tests for generation-bound session identity, phases, and snapshots.

**Validates: Requirements 2.1, 2.4, 2.12, 2.16, 3.1, 3.3**
"""

from dataclasses import FrozenInstanceError

import pytest
from hypothesis import given, strategies as st

from photo_guess_game.models import (
    Ballot,
    DeliveryEntry,
    DeliveryStatus,
    Effect,
    GameSession,
    GameState,
    Player,
    PublicSessionSnapshot,
    RoleAssignment,
    RoundPhase,
    SessionKey,
    SessionResultSnapshot,
    TransitionResult,
)


def make_session(*, generation: int = 7) -> GameSession:
    return GameSession(
        group_chat_id=-1001,
        host_id=1,
        state=GameState.LOBBY,
        players={1: Player(1, "Alice"), 2: Player(2, "Bob")},
        generation=generation,
    )


@given(
    group_a=st.integers(),
    group_b=st.integers(),
    generation_a=st.integers(min_value=0),
    generation_b=st.integers(min_value=0),
)
def test_session_key_identity_is_exact_group_generation_pair(
    group_a, group_b, generation_a, generation_b
):
    """Identity equality is equivalent to tuple equality."""
    left = SessionKey(group_a, generation_a)
    right = SessionKey(group_b, generation_b)
    assert (left == right) == (
        group_a == group_b and generation_a == generation_b
    )
    assert len({left, right}) == (1 if left == right else 2)


def test_session_key_is_immutable_and_rejects_negative_generation():
    key = SessionKey(-1001, 3)
    with pytest.raises(FrozenInstanceError):
        key.generation = 4
    with pytest.raises(ValueError, match="non-negative"):
        SessionKey(-1001, -1)

def test_round_phase_legal_transitions_and_terminal_guard():
    session = make_session()
    assert session.phase == RoundPhase.LOBBY
    assert session.revision == 0

    for expected_revision, phase in enumerate(
        (RoundPhase.DELIVERING, RoundPhase.DISCUSSION, RoundPhase.VOTING),
        start=1,
    ):
        session.transition_phase(phase)
        assert session.phase == phase
        assert session.revision == expected_revision

    session.transition_phase(RoundPhase.DISCUSSION)  # tie-safe fresh ballot path
    session.transition_phase(RoundPhase.VOTING)
    session.transition_phase(RoundPhase.SPY_GUESS)
    assert session.spy_guessing_active is True
    assert session.voting_active is False

    session.mark_terminal(GameState.COMPLETED)
    assert session.terminal is True
    with pytest.raises(ValueError, match="terminal"):
        session.transition_phase(RoundPhase.DISCUSSION)


def test_illegal_phase_transition_is_rejected_without_mutation():
    session = make_session()
    before = (session.phase, session.state, session.revision)
    with pytest.raises(ValueError, match="illegal round phase transition"):
        session.transition_phase(RoundPhase.VOTING)
    assert (session.phase, session.state, session.revision) == before


def test_legacy_phase_flags_are_computed_and_cannot_conflict():
    session = make_session()
    session.voting_active = True
    assert session.phase == RoundPhase.VOTING
    assert session.voting_active is True
    assert session.spy_guessing_active is False

    session.spy_guessing_active = True
    assert session.phase == RoundPhase.SPY_GUESS
    assert session.voting_active is False
    assert session.spy_guessing_active is True


def test_public_and_result_snapshots_are_frozen_detached_values():
    session = make_session(generation=11)
    session.transition_phase(RoundPhase.DELIVERING)
    public = session.public_snapshot()
    result = session.result_snapshot(winner="citizens", reason="spy_found")

    assert isinstance(public, PublicSessionSnapshot)
    assert isinstance(result, SessionResultSnapshot)
    assert public.session_key == SessionKey(-1001, 11)
    assert result.players == public.players
    assert result.winner == "citizens"

    session.players[1].display_name = "Changed"
    session.players[3] = Player(3, "Carol")
    session.revision += 1
    assert [player.display_name for player in public.players] == ["Alice", "Bob"]
    assert [player.user_id for player in result.players] == [1, 2]
    assert public.revision == 1

    with pytest.raises(FrozenInstanceError):
        public.host_id = 99
    with pytest.raises(FrozenInstanceError):
        result.reason = "changed"


def test_delivery_models_have_safe_defaults_and_session_compatibility():
    """Delivery state starts pending without changing legacy session creation.

    **Validates: Requirements 2.6, 2.7, 3.3**
    """
    assignment = RoleAssignment(1, "spy", "TOP SECRET ROLE MESSAGE")
    entry = DeliveryEntry(assignment)
    session = make_session()

    assert entry.status is DeliveryStatus.PENDING
    assert entry.attempts == 0
    assert entry.last_error_code is None
    assert session.delivery == {}
    assert session.ballot_sequence == 0
    assert session.ballot is None
    assert session.spy_guess_used is False

    with pytest.raises(ValueError, match="role"):
        RoleAssignment(1, "observer", "invalid")
    with pytest.raises(ValueError, match="attempts"):
        DeliveryEntry(assignment, attempts=-1)


@given(
    voters=st.sets(st.integers(), max_size=20),
    targets=st.sets(st.integers(), max_size=20),
)
def test_ballot_eligible_sets_are_detached_immutable_snapshots(voters, targets):
    """Ballot eligibility never follows later mutations to input sets.

    **Validates: Requirements 2.8, 2.9, 3.5**
    """
    expected_voters = frozenset(voters)
    expected_targets = frozenset(targets)
    ballot = Ballot(3, voters, targets)

    voters.clear()
    targets.clear()

    assert ballot.eligible_voters == expected_voters
    assert ballot.eligible_targets == expected_targets
    assert isinstance(ballot.eligible_voters, frozenset)
    assert isinstance(ballot.eligible_targets, frozenset)
    assert ballot.votes == {}
    assert ballot.open is True


def test_ballot_copies_initial_votes_and_validates_identifier():
    """Votes remain mutable locally while caller aliases and bad IDs are rejected.

    **Validates: Requirements 2.8, 2.9**
    """
    initial_votes = {1: 2}
    ballot = Ballot(0, frozenset({1}), frozenset({2}), initial_votes)
    initial_votes[1] = 99

    assert ballot.votes == {1: 2}
    ballot.votes[3] = 2
    assert ballot.votes == {1: 2, 3: 2}

    with pytest.raises(ValueError, match="ballot_id"):
        Ballot(-1, frozenset(), frozenset())


def test_effect_and_transition_are_immutable_and_trace_safe():
    """Post-commit plans are tuples and traces redact private payloads.

    **Validates: Requirements 2.6, 2.14, 3.3**
    """
    assignment = RoleAssignment(7, "citizen", "location: SECRET HARBOR")
    effect = Effect(
        effect_id="role-dm-7",
        session_key=SessionKey(-1001, 4),
        expected_revision=2,
        kind="telegram",
        payload=assignment,
    )
    supplied_effects = [effect]
    result = TransitionResult(
        ok=True,
        reason=None,
        session_key=effect.session_key,
        committed_revision=2,
        effects=supplied_effects,
        public_snapshot=assignment,
    )
    supplied_effects.clear()

    assert result.effects == (effect,)
    assert "SECRET HARBOR" not in repr(assignment)
    assert "citizen" not in repr(assignment)
    assert "SECRET HARBOR" not in repr(effect)
    assert "SECRET HARBOR" not in repr(result)

    with pytest.raises(FrozenInstanceError):
        assignment.role = "spy"
    with pytest.raises(FrozenInstanceError):
        effect.kind = "cancel_tasks"
    with pytest.raises(FrozenInstanceError):
        result.ok = False


def test_public_snapshots_never_expose_delivery_roles_or_secrets():
    """Public projections contain no role, delivery, or secret fields.

    **Validates: Requirements 2.6, 2.14, 3.3**
    """
    session = make_session()
    session.players[1].is_spy = True
    session.players[2].secret_word = "SECRET HARBOR"
    session.secret_location_name = "SECRET HARBOR"
    session.delivery[1] = DeliveryEntry(
        RoleAssignment(1, "spy", "You are the spy at SECRET HARBOR")
    )

    public = session.public_snapshot()
    result = session.result_snapshot(winner="citizens")

    for snapshot in (public, result):
        rendered = repr(snapshot)
        assert "SECRET HARBOR" not in rendered
        assert "delivery" not in rendered
        assert "is_spy" not in rendered
        assert "secret" not in rendered
