"""Manual smoke test for SessionManager.cancel_session (task 12.1).

Not part of the test suite; a throwaway script to verify task 12.1's
behavior before the property tests in 12.2/12.3 are written. Deleted
after verification.

Covers:
  - Host cancels a Lobby-state session: state -> Cancelled, photos
    cleared, labels/guesses cleared, group notification returned
  - Host cancels a Guessing-state session with labels + guesses recorded:
    same cleanup happens
  - Non-Host requester is rejected with reason="not_host" and no state
    changes
  - Rejection when no session exists (reason="no_session")
  - Rejection when the session is already terminal (Completed or
    Cancelled), with reason="already_terminal" and no mutation
"""

from photo_guess_game.models import GameSession, GameState, Player
from photo_guess_game.session_store import SessionStore
from photo_guess_game.session_manager import SessionManager


def _session(store, group_chat_id, host_id, state, players):
    session = GameSession(
        group_chat_id=group_chat_id,
        host_id=host_id,
        state=state,
        players={p.user_id: p for p in players},
    )
    store.put(session)
    return session


def test_host_cancels_lobby_session():
    store = SessionStore()
    manager = SessionManager(store)
    _session(
        store,
        group_chat_id=10,
        host_id=1,
        state=GameState.LOBBY,
        players=[
            Player(user_id=1, display_name="Alice", photo_file_id="p-1"),
            Player(user_id=2, display_name="Bob", photo_file_id="p-2"),
        ],
    )

    result = manager.cancel_session(group_chat_id=10, user_id=1)

    assert result.ok is True, result
    assert result.reason is None
    session = store.get(10)
    assert session.state == GameState.CANCELLED
    assert all(p.photo_file_id is None for p in session.players.values())
    assert session.labels == {}
    assert session.guesses == {}
    assert len(result.notifications) == 1
    notif = result.notifications[0]
    assert notif.channel == "group" and notif.target_id == 10
    assert "Alice" in notif.text and ("إلغاء" in notif.text or "cancel" in notif.text.lower())

    print("PASS: Host can cancel a Lobby-state session; photos cleared and group notified")


def test_host_cancels_guessing_session_discards_labels_and_guesses():
    store = SessionStore()
    manager = SessionManager(store)
    session = _session(
        store,
        group_chat_id=20,
        host_id=1,
        state=GameState.GUESSING,
        players=[
            Player(user_id=1, display_name="Alice", photo_file_id="p-1"),
            Player(user_id=2, display_name="Bob", photo_file_id="p-2"),
            Player(user_id=3, display_name="Carol", photo_file_id="p-3"),
        ],
    )
    session.labels = {"A": 1, "B": 2, "C": 3}
    session.guesses = {
        1: {"B": 2, "C": 3},
        2: {"A": 1},
        3: {"A": 2, "B": 1},
    }
    store.put(session)

    result = manager.cancel_session(group_chat_id=20, user_id=1)

    assert result.ok is True, result
    session_after = store.get(20)
    assert session_after.state == GameState.CANCELLED
    assert all(p.photo_file_id is None for p in session_after.players.values())
    assert session_after.labels == {}
    assert session_after.guesses == {}
    print("PASS: Cancelling a Guessing-state session discards every Photo_Submission, Label, and Guess")


def test_non_host_requester_is_rejected_without_mutation():
    store = SessionStore()
    manager = SessionManager(store)
    session = _session(
        store,
        group_chat_id=30,
        host_id=1,
        state=GameState.GUESSING,
        players=[
            Player(user_id=1, display_name="Alice", photo_file_id="p-1"),
            Player(user_id=2, display_name="Bob", photo_file_id="p-2"),
        ],
    )
    session.labels = {"A": 1, "B": 2}
    session.guesses = {2: {"A": 1}}
    store.put(session)

    # Player who is not the host
    result = manager.cancel_session(group_chat_id=30, user_id=2)
    assert result.ok is False and result.reason == "not_host", result

    # Complete outsider
    result_outsider = manager.cancel_session(group_chat_id=30, user_id=999)
    assert result_outsider.ok is False and result_outsider.reason == "not_host", result_outsider

    session_after = store.get(30)
    assert session_after.state == GameState.GUESSING
    assert session_after.players[1].photo_file_id == "p-1"
    assert session_after.players[2].photo_file_id == "p-2"
    assert session_after.labels == {"A": 1, "B": 2}
    assert session_after.guesses == {2: {"A": 1}}
    print("PASS: Non-Host requesters are rejected with reason='not_host' and no state changes")


def test_rejects_when_no_session_exists():
    store = SessionStore()
    manager = SessionManager(store)

    result = manager.cancel_session(group_chat_id=404, user_id=1)

    assert result.ok is False and result.reason == "no_session", result
    assert result.session is None
    print("PASS: Cancel with no session returns reason='no_session'")


def test_rejects_terminal_session_without_mutation():
    for terminal_state in (GameState.CANCELLED, GameState.COMPLETED):
        store = SessionStore()
        manager = SessionManager(store)
        session = _session(
            store,
            group_chat_id=50,
            host_id=1,
            state=terminal_state,
            players=[Player(user_id=1, display_name="Alice", photo_file_id="p-1")],
        )
        # Even a stale label/guess record on a terminal session must remain
        # untouched by a rejected cancel.
        session.labels = {"A": 1}
        session.guesses = {1: {"A": 1}}
        store.put(session)

        result = manager.cancel_session(group_chat_id=50, user_id=1)

        assert result.ok is False and result.reason == "already_terminal", (
            terminal_state,
            result,
        )
        session_after = store.get(50)
        assert session_after.state == terminal_state
        assert session_after.players[1].photo_file_id == "p-1"
        assert session_after.labels == {"A": 1}
        assert session_after.guesses == {1: {"A": 1}}
    print("PASS: Cancel on a terminal session is rejected with reason='already_terminal' and no mutation")


if __name__ == "__main__":
    test_host_cancels_lobby_session()
    test_host_cancels_guessing_session_discards_labels_and_guesses()
    test_non_host_requester_is_rejected_without_mutation()
    test_rejects_when_no_session_exists()
    test_rejects_terminal_session_without_mutation()
    print("\nAll smoke tests passed.")
