"""Baseline exploration for Property 1: Bug Condition.

These tests intentionally assert the fixed behavior against the unfixed baseline.
Each defect branch is a separate pytest item so one failure cannot hide another.
No real clock, sleep, Telegram request, or application-code modification is used.

Stable exploration seed: 20250308. Explicit baseline counterexamples:
R old Lobby: group=7101, age=3601s -> replaced.
A lock/I/O: group=7102 -> every send starts while its group lock is held.
D/T partial DM: group=7103, failed recipient=2 -> GUESSING and success announced.
G stale timer/callback: groups=7104/7105 -> old work mutates the new game.
M private photo: user=71, file_id='photo-71' -> help only; photo handler not called.
V vote guards: group=7106 -> repeated/openless/inactive/duplicate votes mutate tally.
Tie/target: group=7107 -> tie eliminates first max; target=999 raises KeyError.
S spy guess: group=7108 -> non-spy/out-of-opportunity guess completes game.
B cleanup: base group=7200, cycles=2 -> terminal sessions and locks remain retained.

**Validates: Requirements 1.1-1.16, 2.1-2.16**
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
import time

import pytest
from hypothesis import example, given, seed, settings, strategies as st

from photo_guess_game.models import GameState
from photo_guess_game.session_manager import SessionManager
from photo_guess_game.session_store import SessionStore
from photo_guess_game.telegram_adapter import TelegramAdapter
from photo_guess_game.timer_service import TimerService
from run_bot import TelegramBotRunner

SEED = 20250308


def exploration(test):
    configured = settings(max_examples=5, deadline=None, database=None)(test)
    return seed(SEED)(configured)


class FakeHandle:
    def __init__(self, callback):
        self.callback = callback
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeScheduler:
    def __init__(self):
        self.calls = []

    def __call__(self, delay, callback):
        handle = FakeHandle(callback)
        self.calls.append((delay, handle))
        return handle


class ProbeTransport:
    def __init__(self, store, failed_recipient=None):
        self.store = store
        self.failed_recipient = failed_recipient
        self.messages = []
        self.lock_states = []

    async def send_message(self, target_id, text, reply_markup=None):
        self.lock_states.append(any(lock.locked() for lock in self.store._locks.values()))
        outcome = {"ok": target_id != self.failed_recipient}
        self.messages.append((target_id, text, outcome))
        return outcome

    async def send_photo(self, target_id, file_id, text, reply_markup=None):
        self.lock_states.append(any(lock.locked() for lock in self.store._locks.values()))
        return {"ok": target_id != self.failed_recipient}


def snapshot(session):
    return deepcopy(session)


def make_guessing(group_id, player_count=3, *, store=None, scheduler=None):
    store = store or SessionStore()
    scheduler = scheduler or FakeScheduler()
    timer = TimerService(scheduler=scheduler)
    manager = SessionManager(store, timer_service=timer)
    assert manager.create_session(group_id, 1, "P1").ok
    for user_id in range(2, player_count + 1):
        assert manager.join_session(group_id, user_id, f"P{user_id}").ok
    assert manager.start_session(group_id, 1).ok
    return store, manager, timer, scheduler


def fail_message(branch, counterexample, before, after, observed):
    return (
        f"branch={branch}; exact_counterexample={counterexample!r}; "
        f"before={before!r}; after={after!r}; observed={observed!r}; seed={SEED}"
    )


@exploration
@given(
    group_id=st.integers(min_value=7100, max_value=7199),
    old_age=st.floats(min_value=3600.001, max_value=7200, allow_nan=False),
)
@example(group_id=7101, old_age=3601.0)
def test_bug_condition_active_lobby_is_never_replaced(group_id, old_age):
    store = SessionStore()
    manager = SessionManager(store)
    assert manager.create_session(group_id, 1, "original-host").ok
    original = store.get(group_id)
    original.created_at = time.time() - old_age
    before = snapshot(original)

    result = manager.create_session(group_id, 2, "replacement-host")
    after = snapshot(store.get(group_id))

    legal = not result.ok and store.get(group_id) is original and after == before
    assert legal, fail_message(
        "R_active_lobby_replacement",
        {"group_id": group_id, "old_age": old_age, "requester": 2},
        before,
        after,
        {"result_ok": result.ok, "reason": result.reason},
    )


@exploration
@given(group_id=st.integers(min_value=7100, max_value=7199))
@example(group_id=7102)
def test_bug_condition_telegram_io_never_starts_under_group_lock(group_id):
    async def scenario():
        store = SessionStore()
        scheduler = FakeScheduler()
        timer = TimerService(scheduler=scheduler)
        manager = SessionManager(store, timer_service=timer)
        manager.create_session(group_id, 1, "P1")
        manager.join_session(group_id, 2, "P2")
        transport = ProbeTransport(store)
        adapter = TelegramAdapter(
            store,
            session_manager=manager,
            timer_service=timer,
            send_message_fn=transport.send_message,
            send_photo_fn=transport.send_photo,
        )
        await adapter.handle_startgame(group_id, 1)
        return transport.lock_states

    lock_states = asyncio.run(scenario())
    assert lock_states and not any(lock_states), fail_message(
        "A_telegram_await_under_lock",
        {"group_id": group_id},
        "lock released before effects",
        lock_states,
        {"send_count": len(lock_states), "locked_during_send": lock_states},
    )


@exploration
@given(
    group_id=st.integers(min_value=7100, max_value=7199),
    failed_recipient=st.sampled_from([1, 2]),
)
@example(group_id=7103, failed_recipient=2)
def test_bug_condition_partial_role_dm_keeps_round_unplayable(group_id, failed_recipient):
    async def scenario():
        store = SessionStore()
        scheduler = FakeScheduler()
        timer = TimerService(scheduler=scheduler)
        manager = SessionManager(store, timer_service=timer)
        manager.create_session(group_id, 1, "P1")
        manager.join_session(group_id, 2, "P2")
        transport = ProbeTransport(store, failed_recipient=failed_recipient)
        adapter = TelegramAdapter(
            store,
            session_manager=manager,
            timer_service=timer,
            send_message_fn=transport.send_message,
            send_photo_fn=transport.send_photo,
        )
        result = await adapter.handle_startgame(group_id, 1)
        return store, result, transport

    store, result, transport = asyncio.run(scenario())
    current = store.get(group_id)
    success_announced = any(
        target == group_id and "تم توزيع الكلمات السرية" in text
        for target, text, _ in transport.messages
    )
    failed_outcomes = [item for item in transport.messages if item[2]["ok"] is False]
    legal = not result.ok and current.state != GameState.GUESSING and not success_announced
    assert legal, fail_message(
        "D_T_partial_role_delivery",
        {"group_id": group_id, "failed_recipient": failed_recipient},
        {"state": "non-playable", "success_announced": False},
        {"state": current.state.value, "success_announced": success_announced},
        {"result_ok": result.ok, "failed_outcomes": failed_outcomes},
    )


@exploration
@given(group_id=st.integers(min_value=7100, max_value=7199))
@example(group_id=7104)
def test_bug_condition_stale_timer_cannot_complete_new_game(group_id):
    store = SessionStore()
    scheduler = FakeScheduler()
    _, manager, _, _ = make_guessing(group_id, store=store, scheduler=scheduler)
    old_expiry = max(scheduler.calls, key=lambda call: call[0])[1]
    assert manager.cancel_session(group_id, 1).ok
    store.remove(group_id)

    assert manager.create_session(group_id, 11, "N1").ok
    assert manager.join_session(group_id, 12, "N2").ok
    assert manager.start_session(group_id, 11).ok
    before = snapshot(store.get(group_id))
    old_expiry.callback()
    after = snapshot(store.get(group_id))

    assert after == before, fail_message(
        "G_stale_timer_generation",
        {"group_id": group_id, "old_callback": "expired"},
        before,
        after,
        {"new_state_after_old_timeout": after.state.value},
    )


@exploration
@given(group_id=st.integers(min_value=7100, max_value=7199))
@example(group_id=7105)
def test_bug_condition_stale_callback_cannot_open_new_game_ballot(group_id):
    store, manager, _, _ = make_guessing(group_id)
    old_panel_callback_data = "start_voting"
    assert manager.cancel_session(group_id, 1).ok
    store.remove(group_id)
    assert manager.create_session(group_id, 11, "N1").ok
    assert manager.join_session(group_id, 12, "N2").ok
    assert manager.start_session(group_id, 11).ok
    before = snapshot(store.get(group_id))

    assert old_panel_callback_data == "start_voting"
    result = manager.start_voting_panel(group_id)
    after = snapshot(store.get(group_id))

    legal = not result.ok and after == before
    assert legal, fail_message(
        "G_stale_callback_generation",
        {"group_id": group_id, "callback_data": old_panel_callback_data},
        before,
        after,
        {"result_ok": result.ok, "voting_active": after.voting_active},
    )


@exploration
@given(
    user_id=st.integers(min_value=1, max_value=9999),
    file_suffix=st.integers(min_value=1, max_value=9999),
)
@example(user_id=71, file_suffix=71)
def test_bug_condition_private_photo_routes_exactly_once(user_id, file_suffix):
    async def scenario():
        runner = TelegramBotRunner(token="test-token")
        calls = {"photo": [], "help": []}

        async def handle_photo(uid, file_id):
            calls["photo"].append((uid, file_id))

        async def send_help(target_id, text, **kwargs):
            calls["help"].append((target_id, text))

        runner.adapter.handle_dm_photo = handle_photo
        runner.send_message = send_help
        await runner.process_update(
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": user_id, "type": "private"},
                    "from": {"id": user_id, "first_name": "User"},
                    "photo": [{"file_id": f"photo-{file_suffix}"}],
                },
            }
        )
        return calls

    calls = asyncio.run(scenario())
    legal = calls["photo"] == [(user_id, f"photo-{file_suffix}")] and not calls["help"]
    assert legal, fail_message(
        "M_private_photo_routing",
        {"user_id": user_id, "file_id": f"photo-{file_suffix}"},
        {"photo_calls": 0, "help_calls": 0},
        calls,
        {"expected_handler_count": 1},
    )


@pytest.mark.parametrize(
    "guard_case",
    ["repeat_open", "before_open", "inactive_voter", "inactive_target", "duplicate"],
)
@exploration
@given(group_id=st.integers(min_value=7100, max_value=7199))
@example(group_id=7106)
def test_bug_condition_vote_guards_reject_without_mutation(guard_case, group_id):
    store, manager, _, _ = make_guessing(group_id)
    session = store.get(group_id)
    session.spy_user_id = 3

    if guard_case == "repeat_open":
        assert manager.start_voting_panel(group_id).ok
        assert manager.record_spy_vote(group_id, 1, 2).ok
        before = snapshot(session)
        result = manager.start_voting_panel(group_id)
    elif guard_case == "before_open":
        before = snapshot(session)
        result = manager.record_spy_vote(group_id, 1, 2)
    elif guard_case == "inactive_voter":
        session.players[1].active = False
        before = snapshot(session)
        result = manager.record_spy_vote(group_id, 1, 2)
    elif guard_case == "inactive_target":
        session.players[2].active = False
        before = snapshot(session)
        result = manager.record_spy_vote(group_id, 1, 2)
    else:
        assert manager.start_voting_panel(group_id).ok
        assert manager.record_spy_vote(group_id, 1, 2).ok
        before = snapshot(session)
        result = manager.record_spy_vote(group_id, 1, 3)

    after = snapshot(store.get(group_id))
    legal = not result.ok and after == before
    assert legal, fail_message(
        "V_vote_guard",
        {"group_id": group_id, "case": guard_case, "voter": 1, "target": 2},
        before,
        after,
        {"result_ok": result.ok, "reason": result.reason, "votes": after.votes},
    )


@exploration
@given(group_id=st.integers(min_value=7100, max_value=7199))
@example(group_id=7107)
def test_bug_condition_tie_eliminates_nobody(group_id):
    store, manager, _, _ = make_guessing(group_id, player_count=4)
    session = store.get(group_id)
    session.spy_user_id = 4
    manager.start_voting_panel(group_id)
    manager.record_spy_vote(group_id, 1, 1)
    manager.record_spy_vote(group_id, 2, 1)
    manager.record_spy_vote(group_id, 3, 2)
    result = manager.record_spy_vote(group_id, 4, 2)
    after = snapshot(store.get(group_id))

    legal = result.ok and after.state == GameState.GUESSING and not after.voting_active
    assert legal, fail_message(
        "V_tie_resolution",
        {"group_id": group_id, "votes": {1: 1, 2: 1, 3: 2, 4: 2}},
        {"state": "guessing", "eliminated": None},
        after,
        {"state": after.state.value, "notifications": [n.text for n in result.notifications]},
    )


@exploration
@given(group_id=st.integers(min_value=7100, max_value=7199))
@example(group_id=7107)
def test_bug_condition_invalid_vote_target_never_raises_or_mutates(group_id):
    store, manager, _, _ = make_guessing(group_id, player_count=2)
    manager.start_voting_panel(group_id)
    before = snapshot(store.get(group_id))
    raised = None
    try:
        manager.record_spy_vote(group_id, 1, 999)
        manager.record_spy_vote(group_id, 2, 999)
    except Exception as exc:  # Exploration records the uncaught baseline failure.
        raised = f"{type(exc).__name__}: {exc}"
    after = snapshot(store.get(group_id))

    legal = raised is None and after == before
    assert legal, fail_message(
        "V_invalid_target",
        {"group_id": group_id, "votes": {1: 999, 2: 999}},
        before,
        after,
        {"uncaught": raised},
    )


@pytest.mark.parametrize("guard_case", ["non_spy", "outside_opportunity"])
@exploration
@given(group_id=st.integers(min_value=7100, max_value=7199))
@example(group_id=7108)
def test_bug_condition_spy_guess_guards_reject_without_mutation(guard_case, group_id):
    store, manager, _, _ = make_guessing(group_id)
    session = store.get(group_id)
    session.spy_user_id = 1
    session.secret_location_word = "hospital"
    session.spy_guessing_active = guard_case == "non_spy"
    actor = 2 if guard_case == "non_spy" else 1
    before = snapshot(session)

    result = manager.submit_spy_location_guess(group_id, actor, "hospital")
    after = snapshot(store.get(group_id))

    legal = not result.ok and after == before
    assert legal, fail_message(
        "S_spy_guess_guard",
        {"group_id": group_id, "case": guard_case, "actor": actor},
        before,
        after,
        {"result_ok": result.ok, "state": after.state.value},
    )


@exploration
@given(
    base_group=st.integers(min_value=7200, max_value=7290),
    cycles=st.integers(min_value=1, max_value=3),
)
@example(base_group=7200, cycles=2)
def test_bug_condition_terminal_resources_are_bounded_and_scrubbed(base_group, cycles):
    store = SessionStore()
    manager = SessionManager(store)
    terminal_sessions = []
    for offset in range(cycles):
        group_id = base_group + offset
        assert manager.create_session(group_id, 1, "host").ok
        session = store.get(group_id)
        session.secret_location_word = "secret"
        session.voting_active = True
        session.spy_guessing_active = True
        session.votes[1] = 1
        store.lock_for(group_id)
        assert manager.cancel_session(group_id, 1).ok
        terminal_sessions.append(session)

    retained = {
        "sessions": len(store._sessions),
        "locks": len(store._locks),
        "live_secrets": sum(s.secret_location_word is not None for s in terminal_sessions),
        "live_flags": sum(s.voting_active or s.spy_guessing_active for s in terminal_sessions),
    }
    legal = retained == {"sessions": 0, "locks": 0, "live_secrets": 0, "live_flags": 0}
    assert legal, fail_message(
        "B_terminal_resource_growth",
        {"base_group": base_group, "cycles": cycles},
        "no terminal retention",
        retained,
        retained,
    )
