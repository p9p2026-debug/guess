"""Manual smoke test for TimerService using a fake scheduler.

Not part of the test suite (no tests/ directory or pytest wiring exists
yet); this is a throwaway script to verify task 8.1's behavior before
the property tests in 8.2/8.3 are written. Deleted after verification.
"""

from photo_guess_game.timer_service import TimerService


class FakeHandle:
    def __init__(self, entry):
        self._entry = entry
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeScheduler:
    """Records scheduled (delay, callback) pairs; call fire_all() to run
    every non-cancelled callback in the order they were scheduled."""

    def __init__(self):
        self.calls = []  # list of (delay, callback, handle)

    def __call__(self, delay, callback):
        handle = FakeHandle((delay, callback))
        self.calls.append((delay, callback, handle))
        return handle

    def fire_all(self):
        for delay, callback, handle in self.calls:
            if not handle.cancelled:
                callback()


def test_normal_duration_schedules_both_callbacks():
    scheduler = FakeScheduler()
    timer = TimerService(scheduler)
    half_calls = []
    expired_calls = []

    timer.start(100, 300, lambda gid: half_calls.append(gid), lambda gid: expired_calls.append(gid))

    assert len(scheduler.calls) == 2, scheduler.calls
    delays = sorted(delay for delay, _, _ in scheduler.calls)
    assert delays == [150.0, 300], delays
    assert half_calls == []
    assert expired_calls == []

    scheduler.fire_all()
    assert half_calls == [100]
    assert expired_calls == [100]
    print("PASS: normal duration schedules both callbacks correctly")


def test_zero_duration_triggers_expired_synchronously():
    scheduler = FakeScheduler()
    timer = TimerService(scheduler)
    half_calls = []
    expired_calls = []

    timer.start(200, 0, lambda gid: half_calls.append(gid), lambda gid: expired_calls.append(gid))

    assert scheduler.calls == [], scheduler.calls
    assert expired_calls == [200]
    assert half_calls == []
    print("PASS: zero duration triggers on_expired synchronously with no half-elapsed reminder")


def test_cancel_prevents_both_callbacks():
    scheduler = FakeScheduler()
    timer = TimerService(scheduler)
    half_calls = []
    expired_calls = []

    timer.start(300, 60, lambda gid: half_calls.append(gid), lambda gid: expired_calls.append(gid))
    timer.cancel(300)
    scheduler.fire_all()

    assert half_calls == []
    assert expired_calls == []
    assert all(h.cancelled for _, _, h in scheduler.calls)
    print("PASS: cancel prevents both callbacks from firing")


def test_restart_cancels_previous_pending_timers():
    scheduler = FakeScheduler()
    timer = TimerService(scheduler)
    expired_calls = []

    timer.start(400, 60, lambda gid: None, lambda gid: expired_calls.append(("first", gid)))
    first_calls = list(scheduler.calls)
    timer.start(400, 60, lambda gid: None, lambda gid: expired_calls.append(("second", gid)))

    assert all(h.cancelled for _, _, h in first_calls)
    scheduler.fire_all()
    assert expired_calls == [("second", 400)]
    print("PASS: restarting cancels previously pending timers for the same group chat")


if __name__ == "__main__":
    test_normal_duration_schedules_both_callbacks()
    test_zero_duration_triggers_expired_synchronously()
    test_cancel_prevents_both_callbacks()
    test_restart_cancels_previous_pending_timers()
    print("\nAll smoke tests passed.")
