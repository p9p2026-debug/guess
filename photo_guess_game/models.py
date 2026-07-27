"""Core data types and models for Telegram Spy Game."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


@dataclass(frozen=True, slots=True)
class SessionKey:
    """Generation-bound identity for a session (group_chat_id + generation)."""

    group_chat_id: int
    generation: int


class GameState(Enum):
    """Explicit state machine states for GameSession.

    ``GUESSING`` is the single post-start playing state.  Voting and the spy's
    final guess are tracked as sub-flags (``voting_active`` /
    ``spy_guessing_active``) on the session rather than as separate top-level
    states, so a game stays observably in ``GUESSING`` while a ballot is open.
    """

    LOBBY = "lobby"
    DEALING = "dealing"
    GUESSING = "guessing"
    DISCUSSION = "discussion"
    VOTING = "voting"
    SPY_LAST_GUESS = "spy_last_guess"
    REVEAL = "reveal"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


#: States after which a session accepts no further transitions.  Kept here (not
#: in session_store) so ``GameSession.terminal`` needs no cross-module import.
TERMINAL_STATES: frozenset[GameState] = frozenset(
    {GameState.COMPLETED, GameState.CANCELLED}
)


@dataclass
class Player:
    """A Telegram user participating in a GameSession."""

    user_id: int
    display_name: str
    joined_at: float = 0.0
    dm_ready: bool = False
    active: bool = True
    is_spy: bool = False
    secret_word: str | None = None


@dataclass
class GameSession:
    """An active or historical Spy Game session bound to a single group chat."""

    game_id: str
    group_chat_id: int
    host_id: int
    state: GameState
    players: dict[int, Player]
    created_at: float = 0.0
    last_activity_at: float = 0.0
    round_number: int = 1
    vote_round: int = 1
    control_message_id: int | None = None
    secret_location_name: str | None = ""
    secret_location_word: str | None = ""
    secret_category: str = ""
    spy_user_id: int | None = None
    votes: dict[int, int] = field(default_factory=dict)  # voter_id -> target_user_id
    eligible_vote_targets: list[int] = field(default_factory=list)
    #: Secret words offered on the spy's final-guess ballot, generated once per
    #: round so reopening the menu cannot reroll easier distractors.
    spy_guess_options: list[str] = field(default_factory=list)
    #: Display labels parallel to ``spy_guess_options`` (same index order).
    spy_guess_labels: list[str] = field(default_factory=list)
    spy_guess_attempted: bool = False
    voting_active: bool = False
    spy_guessing_active: bool = False
    min_players: int = 3
    max_players: int = 15
    #: Monotonic generation for this group chat.  A new session in the same
    #: group always gets a higher generation, so timers and callbacks issued by
    #: a previous game can be rejected instead of mutating the new one.
    generation: int = 1
    #: Bumped on every committed state change; a cheap audit counter.
    revision: int = 0

    @property
    def session_key(self) -> SessionKey:
        """Generation-bound identity used by TimerService and callback guards."""
        return SessionKey(self.group_chat_id, self.generation)

    @property
    def terminal(self) -> bool:
        """Whether this session accepts no further transitions."""
        return self.state in TERMINAL_STATES

    def touch(self) -> int:
        """Bump and return the revision counter after a committed change."""
        self.revision += 1
        return self.revision


@dataclass
class Notification:
    """Outbound Telegram message or update instruction."""

    channel: Literal["group", "dm"]
    target_id: int
    text: str
    buttons: list[list[dict[str, str]]] | None = None
    edit_message_id: int | None = None
    disable_previous_message_id: int | None = None


@dataclass
class OperationResult:
    """Synchronous outcome of a session decision."""

    ok: bool
    reason: str | None = None
    alert_text: str | None = None
    show_alert: bool = False
    notifications: list[Notification] = field(default_factory=list)
    session: GameSession | None = None
