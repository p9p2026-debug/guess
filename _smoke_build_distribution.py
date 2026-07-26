"""Manual smoke test for PhotoDistributor.build_distribution (task 4.6).

Not part of the test suite; a throwaway script to verify task 4.6's
behavior before the property tests in 4.7/4.8/4.9 are written.

Covers:
  - session.labels is written with a join-order-based scheme
  - the same Label is used for the same submitter across every recipient
  - each recipient receives exactly (n - 1) DM notifications, one per other Player
  - a recipient never receives their own photo or Label
  - every notification uses the "dm" channel (Req 3.5)
  - each notification carries the other Player's photo_file_id
  - OperationResult.ok is True and session is updated in the store
"""

from photo_guess_game.models import GameSession, GameState, Player
from photo_guess_game.session_store import SessionStore
from photo_guess_game.photo_distributor import PhotoDistributor


def _guessing_ready_session(players_in_join_order):
    """Build a Lobby-state session with the given players, each with a photo."""
    players = {}
    for user_id, name, file_id in players_in_join_order:
        players[user_id] = Player(
            user_id=user_id, display_name=name, photo_file_id=file_id
        )
    host_id = players_in_join_order[0][0]
    return GameSession(
        group_chat_id=100,
        host_id=host_id,
        state=GameState.LOBBY,
        players=players,
    )


def test_labels_assigned_in_join_order():
    store = SessionStore()
    session = _guessing_ready_session(
        [
            (1, "Alice", "photo-alice"),
            (2, "Bob", "photo-bob"),
            (3, "Carol", "photo-carol"),
        ]
    )
    store.put(session)
    distributor = PhotoDistributor(store)

    result = distributor.build_distribution(session)

    assert result.ok is True, result
    assert session.labels == {
        "Photo A": 1,
        "Photo B": 2,
        "Photo C": 3,
    }, session.labels
    print("PASS: labels assigned by join order (Photo A -> Alice, B -> Bob, C -> Carol)")


def test_each_recipient_gets_every_other_photo_but_not_their_own():
    store = SessionStore()
    session = _guessing_ready_session(
        [
            (1, "Alice", "photo-alice"),
            (2, "Bob", "photo-bob"),
            (3, "Carol", "photo-carol"),
            (4, "Dan", "photo-dan"),
        ]
    )
    store.put(session)
    distributor = PhotoDistributor(store)

    result = distributor.build_distribution(session)

    # Group notifications by recipient.
    per_recipient = {}
    for notification in result.notifications:
        assert notification.channel == "dm", notification
        per_recipient.setdefault(notification.target_id, []).append(notification)

    # Every player is a recipient and receives exactly (n - 1) DMs.
    assert set(per_recipient) == {1, 2, 3, 4}, per_recipient.keys()
    for recipient_id, dms in per_recipient.items():
        assert len(dms) == 3, (recipient_id, dms)
        # The recipient's own photo/Label must never appear in their DMs.
        own_photo = session.players[recipient_id].photo_file_id
        own_label = next(
            label for label, uid in session.labels.items() if uid == recipient_id
        )
        for dm in dms:
            assert dm.photo_file_id != own_photo, (recipient_id, dm)
            assert own_label not in dm.text, (recipient_id, dm)
        # The (n - 1) DMs collectively cover every OTHER player's photo,
        # each exactly once.
        received_photos = {dm.photo_file_id for dm in dms}
        expected_photos = {
            p.photo_file_id for uid, p in session.players.items() if uid != recipient_id
        }
        assert received_photos == expected_photos, (recipient_id, received_photos)
    print("PASS: each recipient gets every other player's photo, none of their own")


def test_same_label_for_same_submitter_across_recipients():
    store = SessionStore()
    session = _guessing_ready_session(
        [
            (1, "Alice", "photo-alice"),
            (2, "Bob", "photo-bob"),
            (3, "Carol", "photo-carol"),
        ]
    )
    store.put(session)
    distributor = PhotoDistributor(store)

    result = distributor.build_distribution(session)

    # For every recipient, extract the (Label, submitter_photo) pairs the
    # recipient sees. Across recipients, the same Label must always map
    # to the same photo_file_id (Req 5.4).
    label_to_photo_by_recipient = {}
    for notification in result.notifications:
        # Extract label from text prefix "Photo X \u2014 ..."
        label = notification.text.split(" \u2014 ")[0]
        label_to_photo_by_recipient.setdefault(notification.target_id, {})[label] = (
            notification.photo_file_id
        )

    # Merge across recipients: every recipient that saw a given label
    # must have seen the same photo_file_id.
    global_map = {}
    for recipient_id, per_label in label_to_photo_by_recipient.items():
        for label, photo in per_label.items():
            assert global_map.setdefault(label, photo) == photo, (
                recipient_id, label, photo, global_map[label],
            )
    print("PASS: the same Label carries the same photo_file_id across every recipient")


def test_session_labels_persist_in_store():
    store = SessionStore()
    session = _guessing_ready_session(
        [
            (1, "Alice", "photo-alice"),
            (2, "Bob", "photo-bob"),
            (3, "Carol", "photo-carol"),
        ]
    )
    store.put(session)
    distributor = PhotoDistributor(store)

    distributor.build_distribution(session)

    stored = store.get(100)
    assert stored is session, "build_distribution should update the same session"
    assert stored.labels == {"Photo A": 1, "Photo B": 2, "Photo C": 3}, stored.labels
    print("PASS: session.labels is persisted in the store")


if __name__ == "__main__":
    test_labels_assigned_in_join_order()
    test_each_recipient_gets_every_other_photo_but_not_their_own()
    test_same_label_for_same_submitter_across_recipients()
    test_session_labels_persist_in_store()
    print("\nAll smoke tests passed.")
