"""Core data types shared across all game-logic components.

Implements exactly the dataclasses/enum specified in the design document's
"Data types shared across components" section (Components and Interfaces).

Requirements: Glossary (Game_Session, Player, Photo_Submission, Label,
Guess, Score)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class GameState(Enum):
    """The state a Game_Session progresses through over its lifecycle."""

    LOBBY = "lobby"
    GUESSING = "guessing"
    REVEAL = "reveal"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass
class Player:
    """A Telegram user who has joined a Game_Session.

    ``photo_file_id`` represents the Player's Photo_Submission (the
    Telegram file_id of their photo); ``None`` means not yet submitted.
    ``active`` becomes False once the Player is marked inactive after
    leaving during the Guessing state (Req 10.4).
    """

    user_id: int
    display_name: str
    photo_file_id: str | None = None
    secret_word: str | None = None
    active: bool = True


@dataclass
class GameSession:
    """One instance of the photo-guessing game tied to a single Group_Chat."""

    group_chat_id: int
    host_id: int
    state: GameState
    players: dict[int, Player]
    min_players: int = 2
    max_players: int = 15
    guessing_timeout_seconds: int = 300
    labels: dict[str, int] = field(default_factory=dict)
    guesses: dict[int, dict[str, int]] = field(default_factory=dict)
    created_at: float = 0.0
    current_turn_user_id: int | None = None
    turn_order: list[int] = field(default_factory=list)
    pending_guess_user_id: int | None = None


@dataclass
class Notification:
    """An outbound message a component wants the Telegram Adapter to send."""

    channel: Literal["group", "dm"]
    target_id: int
    text: str
    photo_file_id: str | None = None
    buttons: list[list[dict[str, str]]] | None = None



@dataclass
class OperationResult:
    """The uniform return type for every public component method."""

    ok: bool
    reason: str | None = None
    notifications: list[Notification] = field(default_factory=list)
    session: GameSession | None = None
