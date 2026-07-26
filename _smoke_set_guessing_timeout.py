"""Manual smoke test for SessionManager.set_guessing_timeout (task 10.1).

Not part of the test suite (no tests/ directory or pytest wiring exists
yet); a throwaway script verifying task 10.1's behavior before the
property tests in 10.5 are written.

Covers: accept a positive custom timeout in Lobby, accept zero in Lobby,
reject non-Host with not_host, reject negative with invalid_timeout,
reject when no session exists / session is not in the Lobby state
(not_in_lobby), and confirm each rejection leaves
``session.guessing_timeout_seconds`` unchanged.
"""

from photo_guess_game.models import GameState
from photo_guess_game.session_manager import SessionManager
from photo_guess_game.session_store import SessionStore


def _fresh_manager_with_lobby():
    store = SessionStore()
    manager = SessionManager(store)
    manager.create_session(group_chat_id=100, host_id=1, host_name="Alice")
    manager.join_session(group_chat_id=100, user_id=2, display_name="Bob")
    manager.join_session(group_chat_id=100, user_id=3, display_name="Carol")
    return manager, store


def test_host_sets_positive_timeout_in_lobby():
    manager, store = _fresh_manager_with_lobby()

    result = manager.set_guessing_timeout(
        group_chat_id=100, requester_id=1, seconds=60
    )

    assert result.ok, result
    assert result.reason is None, result
    assert store.get(100).guessing_timeout_seconds == 60
    assert len(result.notifications) == 1
    assert result.notifications[0].channel == "group"
    assert result.notifications[0].target_id == 100
    print("PASS: host sets positive timeout in Lobby")


def test_host_sets_zero_timeout_in_lobby():
    manager, store = _fresh_manager_with_lobby()

    result = manager.set_guessing_timeout(
        group_chat_id=100, requester_id=1, seconds=0
    )

    assert result.ok, result
    assert store.get(100).guessing_timeout_seconds == 0
    print("PASS: host sets zero timeout in Lobby (Req 7.7 boundary)")


def test_non_host_rejected_with_not_host():
    manager, store = _fresh_manager_with_lobby()
    before = store.get(100).guessing_timeout_seconds

    result = manager.set_guessing_timeout(
        group_chat_id=100, requester_id=2, seconds=120
    )

    assert not result.ok and result.reason == "not_host", result
    assert store.get(100).guessing_timeout_seconds == before
    assert result.notifications == []
    print("PASS: non-Host rejected with not_host, value unchanged")


def test_negative_timeout_rejected():
    manager, store = _fresh_manager_with_lobby()
    before = store.get(100).guessing_timeout_seconds

    result = manager.set_guessing_timeout(
        group_chat_id=100, requester_id=1, seconds=-1
    )

    assert not result.ok and result.reason == "invalid_timeout", result
    assert store.get(100).guessing_timeout_seconds == before
    assert result.notifications == []
    print("PASS: negative timeout rejected with invalid_timeout")


def test_rejected_when_no_session():
    manager, _store = _fresh_manager_with_lobby()

    result = manager.set_guessing_timeout(
        group_chat_id=999, requester_id=1, seconds=60
    )

    assert not result.ok and result.reason == "not_in_lobby", result
    assert result.session is None
    print("PASS: no session rejected with not_in_lobby")


def test_rejected_when_not_in_lobby_state():
    manager, store = _fresh_manager_with_lobby()
    session = store.get(100)
    session.state = GameState.GUESSING  # simulate having started
    store.put(session)
    before = session.guessing_timeout_seconds

    result = manager.set_guessing_timeout(
        group_chat_id=100, requester_id=1, seconds=60
    )

    assert not result.ok and result.reason == "not_in_lobby", result
    assert store.get(100).guessing_timeout_seconds == before
    print("PASS: outside-Lobby rejected with not_in_lobby, value unchanged")


if __name__ == "__main__":
    test_host_sets_positive_timeout_in_lobby()
    test_host_sets_zero_timeout_in_lobby()
    test_non_host_rejected_with_not_host()
    test_negative_timeout_rejected()
    test_rejected_when_no_session()
    test_rejected_when_not_in_lobby_state()
    print("\nAll smoke tests passed.")
