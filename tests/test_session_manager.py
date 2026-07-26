"""Unit and integration tests for SessionManager (start_session, leave_session, etc.)."""

from photo_guess_game.models import GameState, Player
from photo_guess_game.photo_distributor import PhotoDistributor
from photo_guess_game.session_manager import SessionManager
from photo_guess_game.session_store import SessionStore
from photo_guess_game.timer_service import TimerService


class FakeScheduler:
    def __init__(self):
        self.callbacks = []

    def __call__(self, delay, callback):
        handle = FakeHandle(self, callback)
        self.callbacks.append((delay, handle))
        return handle

    def fire_all(self):
        for delay, handle in sorted(self.callbacks, key=lambda x: x[0]):
            if not handle.cancelled:
                handle.callback()


class FakeHandle:
    def __init__(self, scheduler, callback):
        self.scheduler = scheduler
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


def test_start_session_success():
    store = SessionStore()
    timer_service = TimerService(scheduler=FakeScheduler())
    sm = SessionManager(store, timer_service=timer_service)

    sm.create_session(group_chat_id=10, host_id=1, host_name="Alice")
    sm.join_session(group_chat_id=10, user_id=2, display_name="Bob")
    sm.join_session(group_chat_id=10, user_id=3, display_name="Carol")

    # Submit photos for all players
    pd = PhotoDistributor(store)
    pd.submit_photo(1, "photo1")
    pd.submit_photo(2, "photo2")
    pd.submit_photo(3, "photo3")

    res = sm.start_session(group_chat_id=10, requester_id=1)
    assert res.ok is True, res
    assert res.session.state == GameState.GUESSING
    assert len(res.session.labels) == 3
    assert len(res.notifications) > 0


def test_start_session_rejections():
    store = SessionStore()
    sm = SessionManager(store)

    sm.create_session(group_chat_id=10, host_id=1, host_name="Alice")

    # Rejection 1: Below minimum players (1 < 2)
    res = sm.start_session(10, requester_id=1)
    assert res.ok is False
    assert res.reason == "below_minimum"

    sm.join_session(group_chat_id=10, user_id=2, display_name="Bob")

    # Rejection 2: Non-host start
    res = sm.start_session(10, requester_id=2)
    assert res.ok is False
    assert res.reason == "not_host"

    # Rejection 3: Missing photos
    res = sm.start_session(10, requester_id=1)
    assert res.ok is False
    assert res.reason == "missing_photos"


def test_guessing_state_leave_below_minimum_cancels():
    store = SessionStore()
    sm = SessionManager(store)

    sm.create_session(10, 1, "Alice")
    sm.join_session(10, 2, "Bob")

    pd = PhotoDistributor(store)
    pd.submit_photo(1, "p1")
    pd.submit_photo(2, "p2")

    sm.start_session(10, 1)

    # Bob leaves during guessing -> active players fall to 1 < 2 -> cancelled
    res = sm.leave_session(10, user_id=2)
    assert res.ok is True
    assert res.session.state == GameState.CANCELLED
    assert store.get(10).state == GameState.CANCELLED



def test_guessing_state_leave_with_enough_active_players():
    store = SessionStore()
    sm = SessionManager(store)

    sm.create_session(10, 1, "Alice")
    sm.join_session(10, 2, "Bob")
    sm.join_session(10, 3, "Carol")
    sm.join_session(10, 4, "Dan")

    pd = PhotoDistributor(store)
    for i in range(1, 5):
        pd.submit_photo(i, f"p{i}")

    sm.start_session(10, 1)

    res = sm.leave_session(10, user_id=4)
    assert res.ok is True
    # Active players 3 >= min_players 3 -> session remains in GUESSING
    assert res.session.state == GameState.GUESSING
    assert res.session.players[4].active is False
