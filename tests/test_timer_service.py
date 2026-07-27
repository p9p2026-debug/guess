"""Deterministic TimerService tests using a fake clock only.

Validates current/stale delivery, idempotent cancellation, remove-before-fire,
replacement, and the late-schedule-after-terminal race.

**Validates: Requirements 2.3, 2.4, 2.12, 2.16, 3.10**
"""

from __future__ import annotations

from dataclasses import dataclass

from photo_guess_game.models import GameSession, GameState, Player, RoundPhase, SessionKey
from photo_guess_game.timer_service import TimerEvent, TimerService


@dataclass
class FakeHandle:
    due: float
    callback: object
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True

    def invoke(self, *, ignore_cancel: bool = False) -> None:
        if ignore_cancel or not self.cancelled:
            self.callback()


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.handles: list[FakeHandle] = []

    def call_later(self, delay: float, callback) -> FakeHandle:
        handle = FakeHandle(self.now + delay, callback)
        self.handles.append(handle)
        return handle

    def advance(self, seconds: float) -> None:
        self.now += seconds
        for handle in sorted(tuple(self.handles), key=lambda item: item.due):
            if handle.due <= self.now:
                handle.invoke()


def make_session(group_id: int, generation: int, revision: int = 4) -> GameSession:
    return GameSession(
        group_chat_id=group_id,
        host_id=1,
        state=GameState.GUESSING,
        players={1: Player(1, "Host")},
        generation=generation,
        revision=revision,
        round_phase=RoundPhase.DISCUSSION,
    )


def test_current_timer_is_removed_before_one_event_is_emitted() -> None:
    clock = FakeClock()
    session = make_session(10, 7)
    sessions = {10: session}
    observed: list[TimerEvent] = []
    service: TimerService

    def execute(event: TimerEvent) -> None:
        assert service.pending_count == 0
        assert event.matches(sessions[10], current_timer_id="round-1")
        observed.append(event)

    service = TimerService(
        clock.call_later,
        execute,
        session_lookup=lambda key: sessions.get(key.group_chat_id),
    )
    assert service.schedule(
        session.session_key,
        "guessing_timeout",
        "round-1",
        30,
        expected_phase=RoundPhase.DISCUSSION,
        expected_revision=4,
    )

    clock.advance(30)

    assert [event.timer_id for event in observed] == ["round-1"]
    assert service.pending_count == 0


def test_old_generation_and_changed_revision_are_stale_at_fire_time() -> None:
    clock = FakeClock()
    current = make_session(20, 1)
    sessions = {20: current}
    observed: list[TimerEvent] = []
    service = TimerService(
        clock.call_later,
        observed.append,
        session_lookup=lambda key: (
            session
            if (session := sessions.get(key.group_chat_id)) is not None
            and session.session_key == key
            else None
        ),
    )
    assert service.schedule(
        current.session_key,
        "timeout",
        "old-generation",
        5,
        expected_phase=RoundPhase.DISCUSSION,
        expected_revision=4,
    )

    sessions[20] = make_session(20, 2)
    clock.advance(5)
    assert observed == []
    assert service.pending_count == 0

    replacement = sessions[20]
    assert service.schedule(
        replacement.session_key,
        "timeout",
        "old-revision",
        5,
        expected_phase=RoundPhase.DISCUSSION,
        expected_revision=4,
    )
    replacement.revision = 5
    clock.advance(5)
    assert observed == []
    assert service.pending_count == 0


def test_cancel_is_idempotent_and_suppresses_firing() -> None:
    clock = FakeClock()
    key = SessionKey(30, 3)
    observed: list[TimerEvent] = []
    service = TimerService(clock.call_later, observed.append)
    assert service.schedule(key, "timeout", "cancel-me", 10)

    assert service.cancel_session(key) == 1
    assert service.cancel_session(key) == 0
    clock.advance(10)

    assert observed == []
    assert service.pending_count == 0
    assert clock.handles[0].cancelled


def test_replacement_and_duplicate_scheduler_callback_fire_only_current_once() -> None:
    clock = FakeClock()
    key = SessionKey(40, 8)
    observed: list[str] = []
    service = TimerService(clock.call_later)
    assert service.schedule(
        key, "timeout", "first", 10, lambda event: observed.append(event.timer_id)
    )
    first_handle = clock.handles[-1]
    assert service.schedule(
        key, "timeout", "second", 10, lambda event: observed.append(event.timer_id)
    )
    second_handle = clock.handles[-1]

    assert first_handle.cancelled
    assert service.pending_count == 1
    # Simulate a scheduler race that invokes a cancelled callback anyway.
    first_handle.invoke(ignore_cancel=True)
    second_handle.invoke()
    second_handle.invoke()

    assert observed == ["second"]
    assert service.pending_count == 0


def test_late_schedule_after_terminal_cancel_is_rejected_without_handle() -> None:
    clock = FakeClock()
    session = make_session(50, 9)
    sessions = {50: session}
    observed: list[TimerEvent] = []
    service = TimerService(
        clock.call_later,
        observed.append,
        session_lookup=lambda key: (
            candidate
            if (candidate := sessions.get(key.group_chat_id)) is not None
            and candidate.session_key == key
            else None
        ),
    )
    assert service.schedule(
        session.session_key,
        "timeout",
        "before-terminal",
        10,
        expected_phase=RoundPhase.DISCUSSION,
        expected_revision=4,
    )

    assert service.cancel_session(session.session_key) == 1
    session.mark_terminal(GameState.CANCELLED)
    sessions.pop(50)
    scheduled_handle_count = len(clock.handles)

    assert not service.schedule(
        session.session_key,
        "timeout",
        "late-effect",
        10,
        expected_phase=RoundPhase.DISCUSSION,
        expected_revision=4,
    )
    assert len(clock.handles) == scheduled_handle_count
    assert service.pending_count == 0
    clock.advance(10)
    assert observed == []


def test_wrong_phase_or_revision_is_rejected_before_registration() -> None:
    clock = FakeClock()
    session = make_session(60, 11)
    service = TimerService(
        clock.call_later,
        lambda event: None,
        session_lookup=lambda key: session if session.session_key == key else None,
    )

    assert not service.schedule(
        session.session_key,
        "timeout",
        "wrong-phase",
        1,
        expected_phase=RoundPhase.VOTING,
        expected_revision=4,
    )
    assert not service.schedule(
        session.session_key,
        "timeout",
        "wrong-revision",
        1,
        expected_phase=RoundPhase.DISCUSSION,
        expected_revision=3,
    )
    assert clock.handles == []
    assert service.pending_count == 0


def test_shutdown_rejects_new_timers_and_raced_handles_cannot_emit() -> None:
    """Task 3.16: shutdown releases handles before any raced callback.

    **Validates: Requirements 2.3, 2.12, 2.16**
    """
    clock = FakeClock()
    key = SessionKey(70, 12)
    observed: list[TimerEvent] = []
    service = TimerService(clock.call_later, observed.append)
    assert service.schedule(key, "timeout", "during-shutdown", 10)
    handle = clock.handles[-1]

    assert service.shutdown() == 1
    assert service.shutdown() == 0
    assert service.accepting is False
    assert service.pending_count == 0
    assert handle.cancelled is True
    assert not service.schedule(key, "timeout", "late", 1)

    handle.invoke(ignore_cancel=True)
    assert observed == []
    assert service.pending_count == 0
