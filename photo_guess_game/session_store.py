"""In-memory thread-safe session store with automatic cleanup."""

from __future__ import annotations

import asyncio
import time
from .models import GameSession, GameState

_NON_TERMINAL_STATES = (
    GameState.LOBBY,
    GameState.DEALING,
    GameState.DISCUSSION,
    GameState.VOTING,
    GameState.SPY_LAST_GUESS,
)


class SessionStore:
    """In-memory GameSession store with cross-group membership indexing and locks."""

    def __init__(self) -> None:
        self._sessions: dict[int, GameSession] = {}
        self._user_memberships: dict[int, set[int]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def get(self, group_chat_id: int) -> GameSession | None:
        """Return the session for group_chat_id, or None if absent."""
        return self._sessions.get(group_chat_id)

    def put(self, session: GameSession) -> None:
        """Store or update a session and refresh the membership index."""
        session.last_activity_at = time.time()
        self._sessions[session.group_chat_id] = session
        self._reindex_membership(session)

    def remove(self, group_chat_id: int) -> GameSession | None:
        """Remove and return the session for group_chat_id."""
        session = self._sessions.pop(group_chat_id, None)
        self._locks.pop(group_chat_id, None)
        if session is not None:
            for user_id in session.players:
                if user_id in self._user_memberships:
                    self._user_memberships[user_id].discard(group_chat_id)
                    if not self._user_memberships[user_id]:
                        del self._user_memberships[user_id]
        return session

    def lock_for(self, group_chat_id: int) -> asyncio.Lock:
        """Return the per-group asyncio.Lock."""
        if group_chat_id not in self._locks:
            self._locks[group_chat_id] = asyncio.Lock()
        return self._locks[group_chat_id]

    def _reindex_membership(self, session: GameSession) -> None:
        gid = session.group_chat_id
        for user_id, groups in list(self._user_memberships.items()):
            groups.discard(gid)
            if not groups:
                del self._user_memberships[user_id]

        if session.state in _NON_TERMINAL_STATES:
            for user_id, player in session.players.items():
                if player.active:
                    self._user_memberships.setdefault(user_id, set()).add(gid)

    def cleanup_expired(self, max_age_seconds: float = 3600.0) -> int:
        """Purge terminal sessions and unused locks older than max_age_seconds."""
        now = time.time()
        expired_gids = [
            gid for gid, session in self._sessions.items()
            if session.state not in _NON_TERMINAL_STATES
            and (now - session.last_activity_at) > max_age_seconds
        ]
        for gid in expired_gids:
            self.remove(gid)
        return len(expired_gids)
