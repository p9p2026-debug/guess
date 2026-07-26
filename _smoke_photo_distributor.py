"""Manual smoke test for PhotoDistributor.submit_photo (task 4.1).

Not part of the test suite; a throwaway script to verify task 4.1's
behavior before the property tests in 4.2/4.3 are written. Deleted after
verification.

Covers:
  - successful submit with confirmation DM (single Lobby membership)
  - replacing an existing submission
  - no_open_session rejection (no current Lobby membership)
"""

from photo_guess_game.models import GameSession, GameState, Player
from photo_guess_game.session_store import SessionStore
from photo_guess_game.photo_distributor import PhotoDistributor


def _lobby_session(store, group_chat_id, user_id):
    session = GameSession(
        group_chat_id=group_chat_id,
        host_id=user_id,
        state=GameState.LOBBY,
        players={user_id: Player(user_id=user_id, display_name="Alice")},
    )
    store.put(session)
    return session


def test_successful_submit_with_confirmation_dm():
    store = SessionStore()
    _lobby_session(store, group_chat_id=10, user_id=1)
    distributor = PhotoDistributor(store)

    result = distributor.submit_photo(user_id=1, file_id="photo-abc")

    assert result.ok is True, result
    assert store.get(10).players[1].photo_file_id == "photo-abc"
    assert len(result.notifications) == 1
    dm = result.notifications[0]
    assert dm.channel == "dm" and dm.target_id == 1, dm
    print("PASS: successful submit stores photo and returns a DM confirmation")


def test_replacing_an_existing_submission():
    store = SessionStore()
    _lobby_session(store, group_chat_id=10, user_id=1)
    distributor = PhotoDistributor(store)

    distributor.submit_photo(user_id=1, file_id="photo-first")
    result = distributor.submit_photo(user_id=1, file_id="photo-second")

    assert result.ok is True, result
    assert store.get(10).players[1].photo_file_id == "photo-second"
    print("PASS: a later submission replaces the previous Photo_Submission")


def test_no_open_session_rejection():
    store = SessionStore()
    distributor = PhotoDistributor(store)

    # User with no Lobby membership at all.
    result = distributor.submit_photo(user_id=99, file_id="photo-xyz")
    assert result.ok is False and result.reason == "no_open_session", result

    # User whose only session has moved past Lobby (e.g. Guessing) -> no
    # current Lobby membership, so still rejected.
    session = GameSession(
        group_chat_id=20,
        host_id=2,
        state=GameState.GUESSING,
        players={2: Player(user_id=2, display_name="Bob")},
    )
    store.put(session)
    result2 = distributor.submit_photo(user_id=2, file_id="photo-late")
    assert result2.ok is False and result2.reason == "no_open_session", result2
    assert store.get(20).players[2].photo_file_id is None
    print("PASS: submit is rejected with no_open_session when no current Lobby membership exists")


if __name__ == "__main__":
    test_successful_submit_with_confirmation_dm()
    test_replacing_an_existing_submission()
    test_no_open_session_rejection()
    print("\nAll smoke tests passed.")
