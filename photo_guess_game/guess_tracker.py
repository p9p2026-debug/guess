"""Guess_Tracker: validates and records Guesses.

Implements the ``GuessTracker`` component described in the design
document's "Components and Interfaces" section. Like the other
game-logic components, ``record_guess`` is synchronous and
side-effect-free beyond mutating the ``session`` object passed to it;
persisting the mutated session back to the store is the caller's
responsibility.
"""

from __future__ import annotations

from .models import GameSession, GameState, OperationResult


class GuessTracker:
    """Validates and records Guesses against a Game_Session."""

    def record_guess(
        self, session: GameSession, guesser_id: int, label: str, target_id: int
    ) -> OperationResult:
        """Record ``guesser_id``'s Guess that ``label`` belongs to ``target_id``.

        Stores/overwrites ``session.guesses[guesser_id][label] = target_id``
        on success (Req 6.1, 6.5).

        Rejects the request without mutating ``session.guesses`` when:
        - the session is not in the Guessing state
          (``reason="guessing_closed"``, Req 6.6);
        - ``label`` is not in ``session.labels``
          (``reason="invalid_label"``, Req 6.2);
        - ``target_id`` is not a current Player of the session
          (``reason="invalid_target"``, Req 6.3);
        - ``target_id`` equals ``guesser_id``
          (``reason="self_guess"``, Req 6.4).

        Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6
        """
        if session.state != GameState.GUESSING:
            return OperationResult(ok=False, reason="guessing_closed", session=session)

        if label not in session.labels:
            return OperationResult(ok=False, reason="invalid_label", session=session)

        if target_id not in session.players:
            return OperationResult(ok=False, reason="invalid_target", session=session)

        if target_id == guesser_id:
            return OperationResult(ok=False, reason="self_guess", session=session)

        session.guesses.setdefault(guesser_id, {})[label] = target_id
        return OperationResult(ok=True, session=session)

    def discard_player_guesses(self, session: GameSession, player_id: int) -> None:
        """Discard every Guess previously submitted by ``player_id``.

        Removes ``player_id`` as a guesser from ``session.guesses`` so that
        none of the Guesses that Player made about others remain. Called when
        a Player is marked inactive during the Guessing state (Req 10.6).

        Guesses made by *other* Players about ``player_id``'s Label are left
        untouched; Score_Tracker excludes them separately via the ``active``
        flag (Req 10.5), so they are never counted.

        This is a no-op when ``player_id`` has no recorded Guesses.

        Requirements: 10.6
        """
        session.guesses.pop(player_id, None)
