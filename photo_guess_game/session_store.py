"""In-memory session store.

Implements the storage layer described in the design document's "Session
Store" section: a store of ``GameSession`` objects keyed by
``group_chat_id``, a secondary ``user_id -> set[group_chat_id]``
cross-group membership index covering only non-terminal
(Lobby/Guessing/Reveal) sessions, and a per-``group_chat_id`` lock used to
serialize concurrent operations against a single session's mutable state
so that, e.g., a join and a start cannot interleave into an inconsistent
state.

The membership index is what lets Photo_Distributor answer "how many open
sessions is this DM sender currently a Player of" without scanning every
session (Req 11.2-11.4).

Requirements: 11.1, 11.2
"""

from __future__ import annotations

import asyncio

from .models import GameSession, GameState

_NON_TERMINAL_STATES = (GameState.LOBBY, GameState.GUESSING, GameState.REVEAL)


class SessionStore:
    """In-memory ``GameSession`` store with a cross-group membership index.

    Callers are responsible for calling :meth:`put` again after mutating a
    session's ``players`` or ``state`` in place (the design's components
    operate on a mutable ``GameSession``), so the membership index stays
    consistent with the session's current state and player set.
    """

    def __init__(self) -> None:
        self._sessions: dict[int, GameSession] = {}
        self._user_memberships: dict[int, set[int]] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def get(self, group_chat_id: int) -> GameSession | None:
        """Return the session for ``group_chat_id``, or ``None`` if absent."""
        return self._sessions.get(group_chat_id)

    def put(self, session: GameSession) -> None:
        """Store (or update) a session and refresh the membership index.

        Non-terminal sessions (Lobby/Guessing/Reveal) contribute their
        current players to the ``user_id -> group_chat_id`` index;
        terminal sessions (Completed/Cancelled) are removed from it.
        """
        self._sessions[session.group_chat_id] = session
        self._reindex_membership(session)

    def remove(self, group_chat_id: int) -> GameSession | None:
        """Remove and return the session for ``group_chat_id``, if any.

        Also drops the group chat from the membership index and discards
        its lock, freeing the ``group_chat_id`` for a future session
        (Req 12.4).
        """
        session = self._sessions.pop(group_chat_id, None)
        self._drop_membership(group_chat_id)
        self._locks.pop(group_chat_id, None)
        return session

    def group_chat_ids_for_user(self, user_id: int) -> frozenset[int]:
        """Group chats where ``user_id`` is currently a Player of a
        non-terminal Game_Session (Req 11.2-11.4)."""
        return frozenset(self._user_memberships.get(user_id, ()))

    def lock_for(self, group_chat_id: int) -> asyncio.Lock:
        """Return the per-``group_chat_id`` lock, creating it on first use.

        Callers should hold this lock for the duration of any operation
        that reads and then mutates a single session's state, so
        concurrent commands against the same Group_Chat cannot interleave
        into an inconsistent state.
        """
        lock = self._locks.get(group_chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[group_chat_id] = lock
        return lock

    def _reindex_membership(self, session: GameSession) -> None:
        group_chat_id = session.group_chat_id
        self._drop_membership(group_chat_id)
        if session.state in _NON_TERMINAL_STATES:
            for user_id in session.players:
                self._user_memberships.setdefault(user_id, set()).add(group_chat_id)

    def _drop_membership(self, group_chat_id: int) -> None:
        for user_id in list(self._user_memberships):
            memberships = self._user_memberships[user_id]
            memberships.discard(group_chat_id)
            if not memberships:
                del self._user_memberships[user_id]
