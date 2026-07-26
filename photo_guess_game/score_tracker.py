"""Score_Tracker: calculates Scores from Guesses.

Implements the ``ScoreTracker`` component described in the design
document's "Components and Interfaces" section. ``compute_scores`` is a
pure, side-effect-free function of a ``GameSession``'s ``labels`` and
``guesses`` maps.
"""

from __future__ import annotations

from .models import GameSession


class ScoreTracker:
    """Computes each active Player's Score from recorded Guesses."""

    def compute_scores(self, session: GameSession) -> dict[int, int]:
        """Return each active Player's total Score for ``session``.

        For every Label whose submitter is an active Player, every
        active Player is awarded one point when their recorded Guess
        for that Label equals the Label's true submitter, and zero
        points otherwise (including when they have no recorded Guess
        for that Label). Points are summed per Player across every
        eligible Label.

        Inactive Players receive no entry in the returned mapping, and
        Labels belonging to an inactive submitter are skipped entirely
        for every guesser (Req 10.5): a guess *about* an inactive
        Player's Label earns no one a point.

        Requirements: 8.1, 8.2, 8.3, 8.4, 8.5
        """
        active_player_ids = [
            user_id for user_id, player in session.players.items() if player.active
        ]

        eligible_labels = [
            label
            for label, submitter_id in session.labels.items()
            if submitter_id in session.players
            and session.players[submitter_id].active
        ]

        scores: dict[int, int] = {user_id: 0 for user_id in active_player_ids}

        for label in eligible_labels:
            true_submitter = session.labels[label]
            for guesser_id in active_player_ids:
                guessed_target = session.guesses.get(guesser_id, {}).get(label)
                if guessed_target == true_submitter:
                    scores[guesser_id] += 1

        return scores
