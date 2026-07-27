"""In-memory thread-safe session store with automatic cleanup."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from .models import GameSession, GameState

_NON_TERMINAL_STATES = (
    GameState.LOBBY,
    GameState.DEALING,
    GameState.GUESSING,
    GameState.DISCUSSION,
    GameState.VOTING,
    GameState.SPY_LAST_GUESS,
    GameState.REVEAL,
)

# Bounded number of recently-terminal sessions retained for late lookups
# (e.g. a timer or callback firing just after a game ended).  Terminal sessions
# never live in ``_sessions`` so active-resource counts stay tied to activity.
_TOMBSTONE_LIMIT = 256


class SessionStore:
    """In-memory GameSession store with cross-group membership indexing and locks."""

    def __init__(self) -> None:
        self._sessions: dict[int, GameSession] = {}
        self._tombstones: "OrderedDict[int, GameSession]" = OrderedDict()
        self._user_memberships: dict[int, set[int]] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._generations: dict[int, int] = {}

    def next_generation(self, group_chat_id: int) -> int:
        """Reserve the next generation number for a group chat.

        Monotonic for the process lifetime and never reused, even after a
        session is removed, so a timer or callback bound to an older generation
        can never be mistaken for the current one.
        """
        generation = self._generations.get(group_chat_id, 0) + 1
        self._generations[group_chat_id] = generation
        return generation

    def get(self, group_chat_id: int) -> GameSession | None:
        """Return the active or recently-terminal session for group_chat_id."""
        session = self._sessions.get(group_chat_id)
        if session is not None:
            return session
        return self._tombstones.get(group_chat_id)

    def put(self, session: GameSession) -> None:
        """Store or update a session.

        Non-terminal sessions live in ``_sessions`` and are indexed for
        membership.  Terminal sessions are retired to the bounded tombstone map
        and release their active resources (lock + membership) immediately.
        """
        session.last_activity_at = time.time()
        session.touch()
        gid = session.group_chat_id
        if session.state in _NON_TERMINAL_STATES:
            self._tombstones.pop(gid, None)
            self._sessions[gid] = session
            self._reindex_membership(session)
        else:
            self._retire(session)

    def _retire(self, session: GameSession) -> None:
        gid = session.group_chat_id
        self._sessions.pop(gid, None)
        self._locks.pop(gid, None)
        self._discard_membership(session)
        self._tombstones.pop(gid, None)
        self._tombstones[gid] = session
        while len(self._tombstones) > _TOMBSTONE_LIMIT:
            self._tombstones.popitem(last=False)

    def remove(self, group_chat_id: int) -> GameSession | None:
        """Fully remove a session (active or tombstoned) and its lock."""
        session = self._sessions.pop(group_chat_id, None)
        if session is None:
            session = self._tombstones.pop(group_chat_id, None)
        else:
            self._tombstones.pop(group_chat_id, None)
        self._locks.pop(group_chat_id, None)
        if session is not None:
            self._discard_membership(session)
        return session

    def lock_for(self, group_chat_id: int) -> asyncio.Lock:
        """Return the per-group asyncio.Lock."""
        if group_chat_id not in self._locks:
            self._locks[group_chat_id] = asyncio.Lock()
        return self._locks[group_chat_id]

    def group_chat_ids_for_user(self, user_id: int) -> frozenset[int]:
        """Return the set of active group chat ids the user is a member of."""
        return frozenset(self._user_memberships.get(user_id, set()))

    def _discard_membership(self, session: GameSession) -> None:
        gid = session.group_chat_id
        for user_id in list(session.players):
            groups = self._user_memberships.get(user_id)
            if groups is not None:
                groups.discard(gid)
                if not groups:
                    del self._user_memberships[user_id]

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
        """Purge stale tombstones older than max_age_seconds."""
        now = time.time()
        expired_gids = [
            gid for gid, session in self._tombstones.items()
            if (now - session.last_activity_at) > max_age_seconds
        ]
        for gid in expired_gids:
            self._tombstones.pop(gid, None)
            self._locks.pop(gid, None)
        return len(expired_gids)
