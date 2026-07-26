"""Manual smoke test for ScoreTracker.compute_scores (task 7.1)."""

from photo_guess_game.models import GameSession, GameState, Player
from photo_guess_game.score_tracker import ScoreTracker


def make_session(players, labels, guesses):
    return GameSession(
        group_chat_id=1,
        host_id=1,
        state=GameState.REVEAL,
        players=players,
        labels=labels,
        guesses=guesses,
    )


st = ScoreTracker()

# Scenario 1: correct, incorrect, and absent guesses among active players.
# Players 1,2,3 all active. Labels A->1, B->2, C->3.
players = {
    1: Player(user_id=1, display_name="Alice"),
    2: Player(user_id=2, display_name="Bob"),
    3: Player(user_id=3, display_name="Cara"),
}
labels = {"A": 1, "B": 2, "C": 3}
guesses = {
    # Alice: B correct(2), C wrong(1) -> 1 pt
    1: {"B": 2, "C": 1},
    # Bob: A correct(1), C correct(3) -> 2 pts; no guess for own? B is his own label
    2: {"A": 1, "C": 3},
    # Cara: A wrong(2), B absent -> 0 pts
    3: {"A": 2},
}
scores = st.compute_scores(make_session(players, labels, guesses))
assert scores == {1: 1, 2: 2, 3: 0}, scores
print("Scenario 1 (correct/incorrect/absent):", scores)

# Scenario 2: inactive submitter's label is excluded entirely.
# Cara (3) inactive submitter of label C. A guess about C earns nobody a point.
players2 = {
    1: Player(user_id=1, display_name="Alice"),
    2: Player(user_id=2, display_name="Bob"),
    3: Player(user_id=3, display_name="Cara", active=False),
}
labels2 = {"A": 1, "B": 2, "C": 3}
guesses2 = {
    1: {"B": 2, "C": 3},  # C correct but C's submitter inactive -> excluded; B correct -> 1
    2: {"A": 1, "C": 3},  # A correct -> 1; C excluded
}
scores2 = st.compute_scores(make_session(players2, labels2, guesses2))
# Inactive player 3 gets no entry; label C skipped.
assert scores2 == {1: 1, 2: 1}, scores2
print("Scenario 2 (inactive submitter excluded):", scores2)

# Scenario 3: inactive guesser gets no score entry and does not earn points.
players3 = {
    1: Player(user_id=1, display_name="Alice", active=False),
    2: Player(user_id=2, display_name="Bob"),
    3: Player(user_id=3, display_name="Cara"),
}
labels3 = {"A": 1, "B": 2, "C": 3}
guesses3 = {
    1: {"B": 2, "C": 3},  # Alice inactive -> excluded from results
    2: {"A": 1},          # Bob: A correct, but A's submitter (1) inactive -> excluded -> 0
    3: {"B": 2},          # Cara: B correct -> 1
}
scores3 = st.compute_scores(make_session(players3, labels3, guesses3))
assert 1 not in scores3, scores3
assert scores3 == {2: 0, 3: 1}, scores3
print("Scenario 3 (inactive guesser excluded):", scores3)

print("\nAll smoke scenarios passed.")
