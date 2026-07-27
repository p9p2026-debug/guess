"""Unit tests for Telegram Spy Game."""

import pytest
from photo_guess_game.models import GameState
from photo_guess_game.session_store import SessionStore
from photo_guess_game.session_manager import SessionManager
from photo_guess_game.telegram_adapter import TelegramAdapter


@pytest.fixture
def store():
    return SessionStore()


@pytest.fixture
def manager(store):
    return SessionManager(store)


@pytest.fixture
def adapter(store, manager):
    return TelegramAdapter(store, session_manager=manager)


def test_create_and_join_session(manager, store):
    res = manager.create_session(group_chat_id=100, host_id=1, host_name="Host")
    assert res.ok
    session = store.get(100)
    assert session is not None
    assert session.state == GameState.LOBBY
    assert len(session.players) == 1

    join_res = manager.join_session(group_chat_id=100, user_id=2, display_name="P2")
    assert join_res.ok
    assert len(session.players) == 2

    join_res3 = manager.join_session(group_chat_id=100, user_id=3, display_name="P3")
    assert join_res3.ok
    assert len(session.players) == 3


def test_start_session_requires_dm_ready(manager, store):
    manager.create_session(group_chat_id=100, host_id=1, host_name="Host")
    manager.join_session(group_chat_id=100, user_id=2, display_name="P2")
    manager.join_session(group_chat_id=100, user_id=3, display_name="P3")

    # Start should fail because players have not enabled DM
    start_res = manager.start_session(group_chat_id=100, requester_id=1)
    assert not start_res.ok
    assert start_res.reason == "dm_unready"

    # Mark all DM ready
    manager.mark_dm_ready(1)
    manager.mark_dm_ready(2)
    manager.mark_dm_ready(3)

    start_res2 = manager.start_session(group_chat_id=100, requester_id=1)
    assert start_res2.ok
    session = store.get(100)
    assert session.state == GameState.DEALING
    assert session.spy_user_id in (1, 2, 3)


def test_voting_and_tie_breaking(manager, store):
    manager.create_session(group_chat_id=100, host_id=1, host_name="Host")
    manager.join_session(group_chat_id=100, user_id=2, display_name="P2")
    manager.join_session(group_chat_id=100, user_id=3, display_name="P3")
    manager.mark_dm_ready(1)
    manager.mark_dm_ready(2)
    manager.mark_dm_ready(3)

    manager.start_session(group_chat_id=100, requester_id=1)
    manager.complete_role_dealing(group_chat_id=100)

    # Start Voting
    vote_panel_res = manager.start_voting_panel(group_chat_id=100, requester_id=1)
    assert vote_panel_res.ok
    session = store.get(100)
    assert session.state == GameState.VOTING

    # Test Self-Voting Prohibition
    self_vote = manager.record_spy_vote(100, voter_id=1, target_id=1, game_id=session.game_id, vote_round=1)
    assert not self_vote.ok
    assert self_vote.reason == "self_voting_prohibited"

    # Vote 1 -> 2, 2 -> 1 (Tie)
    manager.record_spy_vote(100, voter_id=1, target_id=2, game_id=session.game_id, vote_round=1)
    tie_res = manager.record_spy_vote(100, voter_id=2, target_id=1, game_id=session.game_id, vote_round=1)
    # Vote 3 -> 2
    tie_final = manager.record_spy_vote(100, voter_id=3, target_id=2, game_id=session.game_id, vote_round=1)
    assert tie_final.ok


def test_spy_guess_flow(manager, store):
    manager.create_session(group_chat_id=100, host_id=1, host_name="Host")
    manager.join_session(group_chat_id=100, user_id=2, display_name="P2")
    manager.join_session(group_chat_id=100, user_id=3, display_name="P3")
    manager.mark_dm_ready(1)
    manager.mark_dm_ready(2)
    manager.mark_dm_ready(3)

    manager.start_session(group_chat_id=100, requester_id=1)
    manager.complete_role_dealing(group_chat_id=100)

    session = store.get(100)
    spy_id = session.spy_user_id
    non_spy_id = 2 if spy_id != 2 else 1

    # Non-spy cannot open guess menu
    menu_fail = manager.handle_spy_guess_menu(100, user_id=non_spy_id, game_id=session.game_id)
    assert not menu_fail.ok
    assert menu_fail.reason == "not_spy"

    # Spy opens menu
    menu_ok = manager.handle_spy_guess_menu(100, user_id=spy_id, game_id=session.game_id)
    assert menu_ok.ok
    assert session.state == GameState.SPY_LAST_GUESS

    # Submit spy guess
    guess_res = manager.submit_spy_location_guess(100, spy_id=spy_id, option_index=0, game_id=session.game_id)
    assert guess_res.ok
    assert session.state == GameState.COMPLETED
