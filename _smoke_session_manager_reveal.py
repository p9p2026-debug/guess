"""Manual smoke test for SessionManager.enter_reveal (task 14.1).

Covers the design's Reveal behavior:
- Req 9.1: Group_Chat disclosure names each Label's true submitter.
- Req 9.2: Ranking notification lists Scores in descending order.
- Req 9.3: All top scorers are announced as winners on a tie.
- Req 9.4: Session transitions to Completed unconditionally.
"""

from photo_guess_game.models import GameSession, GameState, Player
from photo_guess_game.score_tracker import ScoreTracker
from photo_guess_game.session_manager import SessionManager
from photo_guess_game.session_store import SessionStore


def make_guessing_session(players, labels, guesses, group_chat_id=1, host_id=1):
    session = GameSession(
        group_chat_id=group_chat_id,
        host_id=host_id,
        state=GameState.GUESSING,
        players=players,
        labels=labels,
        guesses=guesses,
    )
    return session


def scenario_1_single_winner():
    """Bob wins outright; disclosure names every submitter."""
    store = SessionStore()
    sm = SessionManager(store)

    players = {
        1: Player(user_id=1, display_name="Alice"),
        2: Player(user_id=2, display_name="Bob"),
        3: Player(user_id=3, display_name="Cara"),
    }
    labels = {"A": 1, "B": 2, "C": 3}
    guesses = {
        1: {"B": 2, "C": 1},   # Alice: 1 correct
        2: {"A": 1, "C": 3},   # Bob: 2 correct -> winner
        3: {"A": 2},           # Cara: 0 correct
    }
    session = make_guessing_session(players, labels, guesses)
    store.put(session)

    result = sm.enter_reveal(1)

    assert result.ok, result
    assert result.session is session
    assert session.state == GameState.COMPLETED, session.state
    assert len(result.notifications) == 2, result.notifications

    disclosure, ranking = result.notifications
    assert disclosure.channel == "group" and disclosure.target_id == 1
    assert ranking.channel == "group" and ranking.target_id == 1

    # Req 9.1: every label's submitter named.
    for label, submitter_id in labels.items():
        name = players[submitter_id].display_name
        assert f"Photo {label}: {name}" in disclosure.text, disclosure.text

    # Req 9.2: descending order Bob(2) > Alice(1) > Cara(0).
    lines = ranking.text.splitlines()
    score_lines = [line for line in lines if line.startswith("  ")]
    assert score_lines == [
        "  Bob: 2",
        "  Alice: 1",
        "  Cara: 0",
    ], score_lines

    # Req 9.3: single winner (Bob).
    assert "Winner: Bob!" in ranking.text, ranking.text

    # Store reflects terminal state; cross-group index dropped it.
    assert store.get(1) is session and session.state == GameState.COMPLETED
    assert store.group_chat_ids_for_user(1) == frozenset()

    print("Scenario 1 (single winner) OK")
    print("  disclosure:\n" + disclosure.text)
    print("  ranking:\n" + ranking.text)


def scenario_2_tied_winners():
    """Alice and Bob tie at the top; both are announced as winners (Req 9.3)."""
    store = SessionStore()
    sm = SessionManager(store)

    players = {
        1: Player(user_id=1, display_name="Alice"),
        2: Player(user_id=2, display_name="Bob"),
        3: Player(user_id=3, display_name="Cara"),
    }
    labels = {"A": 1, "B": 2, "C": 3}
    guesses = {
        1: {"B": 2, "C": 3},   # Alice: 2 correct
        2: {"A": 1, "C": 3},   # Bob:   2 correct
        3: {"A": 2},           # Cara: 0
    }
    store.put(make_guessing_session(players, labels, guesses, group_chat_id=42))

    result = sm.enter_reveal(42)
    assert result.ok, result
    disclosure, ranking = result.notifications

    # Winners listing preserves join order.
    assert "Winners (tied at 2): Alice, Bob!" in ranking.text, ranking.text

    # Descending order still holds; join order breaks the tie deterministically.
    score_lines = [line for line in ranking.text.splitlines() if line.startswith("  ")]
    assert score_lines == [
        "  Alice: 2",
        "  Bob: 2",
        "  Cara: 0",
    ], score_lines

    assert result.session.state == GameState.COMPLETED
    print("Scenario 2 (tied winners) OK")


def scenario_3_inactive_submitter_excluded_from_scoring():
    """An inactive Player's Label is still disclosed but does not score anyone."""
    store = SessionStore()
    sm = SessionManager(store)

    players = {
        1: Player(user_id=1, display_name="Alice"),
        2: Player(user_id=2, display_name="Bob"),
        3: Player(user_id=3, display_name="Cara", active=False),
    }
    labels = {"A": 1, "B": 2, "C": 3}
    guesses = {
        1: {"B": 2, "C": 3},   # C correct but excluded (inactive submitter)
        2: {"A": 1, "C": 3},   # A correct -> Bob: 1; C excluded
    }
    store.put(make_guessing_session(players, labels, guesses, group_chat_id=7, host_id=1))

    result = sm.enter_reveal(7)
    assert result.ok
    disclosure, ranking = result.notifications

    # Req 9.1: label C still discloses Cara even though she's inactive.
    assert "Photo C: Cara" in disclosure.text, disclosure.text

    # Only active players appear in the ranking (matches ScoreTracker output).
    score_lines = [line for line in ranking.text.splitlines() if line.startswith("  ")]
    assert "Cara" not in "\n".join(score_lines), score_lines
    # Alice: 1 (B correct), Bob: 1 (A correct) -> tied at 1.
    assert score_lines == ["  Alice: 1", "  Bob: 1"], score_lines
    assert "Winners (tied at 1): Alice, Bob!" in ranking.text, ranking.text
    print("Scenario 3 (inactive submitter) OK")


def scenario_4_unconditional_completion():
    """Even if the adapter would fail to deliver, enter_reveal already
    transitioned to Completed by the time it returned (Req 9.4)."""
    store = SessionStore()
    sm = SessionManager(store)

    players = {
        1: Player(user_id=1, display_name="Alice"),
        2: Player(user_id=2, display_name="Bob"),
        3: Player(user_id=3, display_name="Cara"),
    }
    labels = {"A": 1, "B": 2, "C": 3}
    guesses = {}  # nobody guessed
    store.put(make_guessing_session(players, labels, guesses, group_chat_id=9))

    result = sm.enter_reveal(9)
    # Simulate the adapter losing/ignoring the notifications entirely.
    _ = result.notifications  # noqa: F841 -- deliberately not "sent" anywhere.

    assert result.ok
    assert result.session.state == GameState.COMPLETED, result.session.state
    assert store.get(9).state == GameState.COMPLETED
    print("Scenario 4 (unconditional completion) OK")


def scenario_5_stale_timer_callback_is_noop():
    """A stale Timer_Service callback firing after cancellation is a no-op."""
    store = SessionStore()
    sm = SessionManager(store)

    session = GameSession(
        group_chat_id=99,
        host_id=1,
        state=GameState.CANCELLED,  # already terminal
        players={1: Player(user_id=1, display_name="Alice")},
    )
    store.put(session)

    result = sm.enter_reveal(99)
    assert not result.ok
    assert result.reason == "not_in_guessing", result.reason
    assert session.state == GameState.CANCELLED, session.state

    # Also: unknown group chat id.
    result2 = sm.enter_reveal(12345)
    assert not result2.ok
    assert result2.reason == "not_in_guessing"
    assert result2.session is None
    print("Scenario 5 (stale/unknown callback) OK")


def scenario_6_custom_score_tracker_injected():
    """SessionManager accepts a ScoreTracker in its constructor."""
    calls = []

    class RecordingScoreTracker(ScoreTracker):
        def compute_scores(self, session):
            calls.append(session.group_chat_id)
            return super().compute_scores(session)

    store = SessionStore()
    sm = SessionManager(store, score_tracker=RecordingScoreTracker())

    players = {
        1: Player(user_id=1, display_name="Alice"),
        2: Player(user_id=2, display_name="Bob"),
        3: Player(user_id=3, display_name="Cara"),
    }
    labels = {"A": 1, "B": 2, "C": 3}
    guesses = {1: {"B": 2}}
    store.put(make_guessing_session(players, labels, guesses, group_chat_id=5))

    result = sm.enter_reveal(5)
    assert result.ok
    assert calls == [5], calls
    print("Scenario 6 (custom ScoreTracker injected) OK")


if __name__ == "__main__":
    scenario_1_single_winner()
    print()
    scenario_2_tied_winners()
    print()
    scenario_3_inactive_submitter_excluded_from_scoring()
    print()
    scenario_4_unconditional_completion()
    print()
    scenario_5_stale_timer_callback_is_noop()
    print()
    scenario_6_custom_score_tracker_injected()
    print("\nAll smoke scenarios passed.")
