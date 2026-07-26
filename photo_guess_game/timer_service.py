"""Timer_Service: enforces time-based Guessing-state transitions.

Implements the ``TimerService`` component described in the design
document's "Timer_Service" section. Scheduling is delegated to an
injected scheduler callable so tests can supply a fake/synchronous
scheduler instead of real asyncio timers, and so ``TimerService`` itself
never sleeps or otherwise performs I/O.

Requirements: 7.1, 7.2, 7.3, 7.7
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol


class CancelHandle(Protocol):
    """A handle returned by a scheduled call, matching ``asyncio.TimerHandle``."""

    def cancel(self) -> Any: ...


Scheduler = Callable[[float, Callable[[], None]], CancelHandle]
"""An injectable scheduler dependency.

Given a delay in seconds and a zero-argument callback, schedules the
callback to run after the delay and returns a handle whose ``cancel()``
method, if called before the callback fires, prevents it from firing.
The real production scheduler is a bound ``asyncio`` event-loop
``call_later``; tests supply a fake/synchronous scheduler that never
sleeps in real time.
"""


@dataclass
class _PendingTimers:
    half_elapsed_handle: CancelHandle | None = None
    expired_handle: CancelHandle | None = None


class TimerService:
    """Schedules the half-time reminder and Guessing timeout per session."""

    def __init__(self, scheduler: Scheduler) -> None:
        self._scheduler = scheduler
        self._pending: dict[int, _PendingTimers] = {}

    def start(
        self,
        group_chat_id: int,
        duration_seconds: int,
        on_half_elapsed: Callable[[int], None],
        on_expired: Callable[[int], None],
    ) -> None:
        """Start the Guessing-state countdown for ``group_chat_id``.

        Schedules ``on_half_elapsed(group_chat_id)`` at
        ``duration_seconds / 2`` (Req 7.2) and ``on_expired(group_chat_id)``
        at ``duration_seconds`` (Req 7.1, 7.3). When ``duration_seconds``
        is zero, ``on_expired`` is invoked synchronously and immediately,
        and the half-elapsed reminder is never scheduled at all (Req 7.7).

        Any timers previously pending for ``group_chat_id`` are cancelled
        first, so calling ``start`` again for the same group chat cannot
        leave a stale timer from an earlier round pending.
        """
        self.cancel(group_chat_id)

        if duration_seconds == 0:
            on_expired(group_chat_id)
            return

        pending = _PendingTimers(
            half_elapsed_handle=self._scheduler(
                duration_seconds / 2, lambda: on_half_elapsed(group_chat_id)
            ),
            expired_handle=self._scheduler(
                duration_seconds, lambda: on_expired(group_chat_id)
            ),
        )
        self._pending[group_chat_id] = pending

    def cancel(self, group_chat_id: int) -> None:
        """Cancel any timers pending for ``group_chat_id``, if any.

        Called whenever a session leaves the Guessing state through any
        path other than its own timeout (cancellation, or falling below
        Minimum_Players), so a stale timer never fires against a session
        that has moved on.
        """
        pending = self._pending.pop(group_chat_id, None)
        if pending is None:
            return
        if pending.half_elapsed_handle is not None:
            pending.half_elapsed_handle.cancel()
        if pending.expired_handle is not None:
            pending.expired_handle.cancel()
