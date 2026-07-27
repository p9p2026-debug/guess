"""Generation-bound, tracked timer delivery.

Timers own no game mutation.  A firing removes its handle first, validates the
session guard, and emits an immutable :class:`TimerEvent` to the shared event
executor.  The injected scheduler keeps tests deterministic and free of real
sleep.

Requirements: 2.3, 2.4, 2.12, 2.16, 3.10
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from .models import GameSession, RoundPhase, SessionKey


class CancelHandle(Protocol):
    """Minimal handle returned by ``asyncio.call_later``-style schedulers."""

    def cancel(self) -> Any: ...


Scheduler = Callable[[float, Callable[[], None]], CancelHandle]
EventExecutor = Callable[["TimerEvent"], object]
EventValidator = Callable[["TimerEvent"], bool]
SessionLookup = Callable[[SessionKey], GameSession | None]


@dataclass(frozen=True, slots=True)
class TimerEvent:
    """Immutable timeout input carrying every state-commit guard."""

    session_key: SessionKey
    timer_kind: str
    timer_id: str
    expected_phase: RoundPhase | None = None
    expected_revision: int | None = None

    def __post_init__(self) -> None:
        if not self.timer_kind:
            raise ValueError("timer_kind must not be empty")
        if not self.timer_id:
            raise ValueError("timer_id must not be empty")
        if self.expected_revision is not None and self.expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")

    def matches(
        self,
        session: GameSession | None,
        *,
        current_timer_id: str | None = None,
    ) -> bool:
        """Return whether this event may enter a state transaction.

        Executors must call this again against the session read inside their
        transaction.  ``TimerService`` performs the same check before
        registration and immediately before dispatch.
        """
        if session is None or session.terminal:
            return False
        if session.session_key != self.session_key:
            return False
        if self.expected_phase is not None and session.phase != self.expected_phase:
            return False
        if (
            self.expected_revision is not None
            and session.revision != self.expected_revision
        ):
            return False
        return current_timer_id is None or current_timer_id == self.timer_id


@dataclass(slots=True)
class _TimerRecord:
    event: TimerEvent
    executor: EventExecutor
    handle: CancelHandle | None = None


class TimerService:
    """Track exactly one current timer per ``(SessionKey, timer_kind)``.

    ``session_lookup`` is the guard bridge used by modern callers.  Looking up
    by the full :class:`SessionKey` rejects old generations, while
    ``TimerEvent.matches`` checks terminal state, phase, and revision.  A
    custom validator can add executor-specific guards.  Both checks are
    fail-closed.

    The legacy ``start(group_id, ...)`` and ``cancel(group_id)`` methods remain
    temporarily available while older SessionManager paths migrate.
    """

    def __init__(
        self,
        scheduler: Scheduler,
        event_executor: EventExecutor | None = None,
        *,
        session_lookup: SessionLookup | None = None,
        validator: EventValidator | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._event_executor = event_executor
        self._session_lookup = session_lookup
        self._validator = validator
        self._pending: dict[tuple[SessionKey, str, str], _TimerRecord] = {}
        self._current: dict[tuple[SessionKey, str], str] = {}
        self._legacy_sequence = 0
        self._accepting = True

    @property
    def accepting(self) -> bool:
        """Whether new timer handles may still be registered."""
        return self._accepting

    @property
    def pending_count(self) -> int:
        """Number of live, registered handles."""
        return len(self._pending)

    def pending_for(self, key: SessionKey, timer_kind: str | None = None) -> int:
        """Count timers owned by one exact session generation."""
        return sum(
            1
            for pending_key in self._pending
            if pending_key[0] == key
            and (timer_kind is None or pending_key[1] == timer_kind)
        )

    @property
    def pending_keys(self) -> frozenset[tuple[SessionKey, str, str]]:
        """Detached registry view intended for diagnostics and tests."""
        return frozenset(self._pending)

    def schedule(
        self,
        key: SessionKey,
        timer_kind: str,
        timer_id: str,
        delay: float,
        emit: EventExecutor | None = None,
        *,
        expected_phase: RoundPhase | None = None,
        expected_revision: int | None = None,
    ) -> bool:
        """Register a guarded timer, replacing the current timer of its kind.

        Returns ``False`` without touching existing timers when the session is
        already stale/terminal or a phase/revision guard does not match.  This
        ordering closes the race where a late schedule effect arrives after a
        terminal cancellation.
        """
        if not isinstance(key, SessionKey):
            raise TypeError("key must be a SessionKey")
        if not self._accepting:
            return False
        if not isinstance(delay, (int, float)) or not math.isfinite(delay):
            raise ValueError("delay must be a finite number")
        if delay < 0:
            raise ValueError("delay must be non-negative")
        executor = emit or self._event_executor
        if executor is None:
            raise TypeError("an event executor is required")

        event = TimerEvent(
            session_key=key,
            timer_kind=timer_kind,
            timer_id=timer_id,
            expected_phase=expected_phase,
            expected_revision=expected_revision,
        )
        if not self._event_is_current(event):
            return False

        # A new timer of the same kind supersedes only that exact generation's
        # previous timer. Other kinds and other generations remain independent.
        self.cancel(key, timer_kind)
        index = (key, timer_kind, timer_id)
        kind_index = (key, timer_kind)
        record = _TimerRecord(event=event, executor=executor)
        self._pending[index] = record
        self._current[kind_index] = timer_id

        def fire() -> None:
            current = self._pending.get(index)
            if current is not record:
                return
            if self._current.get(kind_index) != timer_id:
                return

            # Removal must precede validation and dispatch so re-entrancy,
            # executor failure, or duplicate scheduler callbacks cannot fire it
            # twice or retain a dead handle.
            self._pending.pop(index, None)
            self._current.pop(kind_index, None)
            if not self._event_is_current(event):
                return
            result = executor(event)
            # TimerService deliberately does not create orphan async tasks.
            if hasattr(result, "__await__"):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise TypeError("timer event executor must be synchronous")

        try:
            handle = self._scheduler(float(delay), fire)
        except BaseException:
            if self._pending.get(index) is record:
                self._pending.pop(index, None)
            if self._current.get(kind_index) == timer_id:
                self._current.pop(kind_index, None)
            raise

        record.handle = handle
        # Defend against an unusual scheduler that invokes zero-delay callbacks
        # synchronously before returning its handle.
        if self._pending.get(index) is not record:
            handle.cancel()
        return True

    def _event_is_current(self, event: TimerEvent) -> bool:
        try:
            if self._session_lookup is not None:
                if not event.matches(self._session_lookup(event.session_key)):
                    return False
            if self._validator is not None and not self._validator(event):
                return False
        except Exception:
            return False
        return True

    def cancel(
        self,
        target: SessionKey | int,
        timer_kind: str | None = None,
        timer_id: str | None = None,
    ) -> int:
        """Idempotently cancel matching timers and return the removed count.

        Passing an ``int`` preserves the old group-wide API.  Modern callers
        should pass an exact ``SessionKey`` (or use :meth:`cancel_session`).
        """
        if timer_id is not None and timer_kind is None:
            raise ValueError("timer_id requires timer_kind")

        matches: list[tuple[SessionKey, str, str]] = []
        for index in tuple(self._pending):
            key, kind, identifier = index
            owner_matches = (
                key == target
                if isinstance(target, SessionKey)
                else key.group_chat_id == target
            )
            if not owner_matches:
                continue
            if timer_kind is not None and kind != timer_kind:
                continue
            if timer_id is not None and identifier != timer_id:
                continue
            matches.append(index)

        for index in matches:
            record = self._pending.pop(index, None)
            if record is None:
                continue
            key, kind, identifier = index
            if self._current.get((key, kind)) == identifier:
                self._current.pop((key, kind), None)
            if record.handle is not None:
                record.handle.cancel()
        return len(matches)

    def cancel_session(self, key: SessionKey) -> int:
        """Cancel every timer for one exact generation; safe when repeated."""
        return self.cancel(key)

    def cancel_all(self) -> int:
        """Cancel every currently registered timer."""
        count = 0
        for key in {index[0] for index in tuple(self._pending)}:
            count += self.cancel_session(key)
        return count

    def shutdown(self) -> int:
        """Stop timer admission and synchronously release all handles.

        Removing records before cancelling their handles makes even a raced
        scheduler callback harmless.  No state lock or asynchronous wait is
        involved, so this is safe as the first cancellation step of shutdown.
        """
        self._accepting = False
        return self.cancel_all()

    # ------------------------------------------------------------------
    # Temporary compatibility surface for the original group-only timer.
    # ------------------------------------------------------------------
    def start(
        self,
        group_chat_id: int,
        duration_seconds: int,
        on_half_elapsed: Callable[[int], None],
        on_expired: Callable[[int], None],
    ) -> None:
        """Start the legacy half/expiry pair without real sleeps.

        Existing group-only timers use generation zero solely as a compatibility
        namespace. New code must use :meth:`schedule` with the real SessionKey.
        """
        if duration_seconds < 0:
            raise ValueError("duration_seconds must be non-negative")
        self.cancel(group_chat_id)
        if duration_seconds == 0:
            on_expired(group_chat_id)
            return

        self._legacy_sequence += 1
        sequence = self._legacy_sequence
        key = SessionKey(group_chat_id, 0)
        self.schedule(
            key,
            "half_elapsed",
            f"legacy-{sequence}-half",
            duration_seconds / 2,
            lambda event: on_half_elapsed(event.session_key.group_chat_id),
        )
        self.schedule(
            key,
            "expired",
            f"legacy-{sequence}-expired",
            duration_seconds,
            lambda event: on_expired(event.session_key.group_chat_id),
        )
