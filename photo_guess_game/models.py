"""Core data types and models for Telegram Spy Game."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class GameState(Enum):
    """Explicit state machine states for GameSession."""

    LOBBY = "lobby"
    DEALING = "dealing"
    DISCUSSION = "discussion"
    VOTING = "voting"
    SPY_LAST_GUESS = "spy_last_guess"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


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
    secret_location_name: str = ""
    secret_location_word: str = ""
    secret_category: str = ""
    spy_user_id: int | None = None
    votes: dict[int, int] = field(default_factory=dict)  # voter_id -> target_user_id
    eligible_vote_targets: list[int] = field(default_factory=list)
    spy_guess_options: list[str] = field(default_factory=list)
    spy_guess_attempted: bool = False
    min_players: int = 3
    max_players: int = 15


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
