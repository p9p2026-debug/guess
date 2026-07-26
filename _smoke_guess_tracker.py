"""Manual smoke test for GuessTracker.record_guess (task 6.1).

Not part of the test suite (no tests/ directory or pytest wiring exists
yet); this is a throwaway script to verify task 6.1's behavior before
the property tests in 6.2/6.3 are written. Deleted after verification.

Covers: valid guess recorded, overwrite of a prior guess for the same
label, and each rejection reason (invalid_label, invalid_target,
self_guess, guessing_closed) leaving state unchanged.
"""

from photo_guess_game.guess_tracker import GuessTracker
from photo_guess_game.models import GameSession, GameState, Player


def _session(state=GameState.GUESSING):
    players = {
        1: Player(user_id=1, display_name="Alice"),
        2: Player(user_id=2, display_name="Bob"),
        3: Player(user_id=3, display_name="Carol"),
    }
    return GameSession(
        group_chat_id=100,
        host_id=1,
        state=state,
        players=players,
        labels={"A": 1, "B": 2, "C": 3},
    )


def test_valid_guess_recorded():
    tracker = GuessTracker()
    session = _session()

    result = tracker.record_guess(session, guesser_id=1, label="B", target_id=2)

    assert result.ok, result
    assert result.reason is None, result
    assert session.guesses == {1: {"B": 2}}, session.guesses
    print("PASS: valid guess recorded")


def test_later_guess_overwrites_same_label():
    tracker = GuessTracker()
    session = _session()

    tracker.record_guess(session, guesser_id=1, label="B", target_id=2)
    result = tracker.record_guess(session, guesser_id=1, label="B", target_id=3)

    assert result.ok, result
    # Exactly one live guess for (guesser=1, label="B"), overwritten to 3.
    assert session.guesses == {1: {"B": 3}}, session.guesses
    print("PASS: later guess for the same label overwrites")


def test_invalid_label_rejected_without_mutation():
    tracker = GuessTracker()
    session = _session()
    tracker.record_guess(session, guesser_id=1, label="A", target_id=2)
    before = {g: dict(m) for g, m in session.guesses.items()}

    result = tracker.record_guess(session, guesser_id=1, label="Z", target_id=2)

    assert not result.ok and result.reason == "invalid_label", result
    assert session.guesses == before, session.guesses
    print("PASS: invalid_label rejected, state unchanged")


def test_invalid_target_rejected_without_mutation():
    tracker = GuessTracker()
    session = _session()
    tracker.record_guess(session, guesser_id=1, label="A", target_id=2)
    before = {g: dict(m) for g, m in session.guesses.items()}

    result = tracker.record_guess(session, guesser_id=1, label="B", target_id=999)

    assert not result.ok and result.reason == "invalid_target", result
    assert session.guesses == before, session.guesses
    print("PASS: invalid_target rejected, state unchanged")


def test_self_guess_rejected_without_mutation():
    tracker = GuessTracker()
    session = _session()
    tracker.record_guess(session, guesser_id=1, label="B", target_id=2)
    before = {g: dict(m) for g, m in session.guesses.items()}

    result = tracker.record_guess(session, guesser_id=1, label="C", target_id=1)

    assert not result.ok and result.reason == "self_guess", result
    assert session.guesses == before, session.guesses
    print("PASS: self_guess rejected, state unchanged")


def test_guessing_closed_rejected_without_mutation():
    tracker = GuessTracker()
    session = _session(state=GameState.LOBBY)

    result = tracker.record_guess(session, guesser_id=1, label="B", target_id=2)

    assert not result.ok and result.reason == "guessing_closed", result
    assert session.guesses == {}, session.guesses
    print("PASS: guessing_closed rejected, state unchanged")


if __name__ == "__main__":
    test_valid_guess_recorded()
    test_later_guess_overwrites_same_label()
    test_invalid_label_rejected_without_mutation()
    test_invalid_target_rejected_without_mutation()
    test_self_guess_rejected_without_mutation()
    test_guessing_closed_rejected_without_mutation()
    print("\nAll smoke tests passed.")
