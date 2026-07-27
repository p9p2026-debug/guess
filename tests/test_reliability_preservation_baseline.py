"""Preservation baseline captured observation-first from the unfixed application.

The golden values below were observed before these assertions were written. Tests
cover only healthy, sequential flows outside ``isBugCondition``. No network,
real sleep, or application-code mutation is used.

Property 2: Preservation — observable behavior outside the bug condition.
**Validates: Requirements 3.1-3.12**
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import patch

from hypothesis import given, seed, settings, strategies as st

from photo_guess_game.models import GameState, Notification
from photo_guess_game.session_manager import SessionManager
from photo_guess_game.session_store import SessionStore
from photo_guess_game.telegram_adapter import TelegramAdapter
from photo_guess_game.timer_service import TimerService

SEED = 20250309
LOCATION = {"name": "المستشفى", "word": "مستشفى"}
LOBBY_BUTTONS = [
    [
        {"text": "➕ انضمام للعبة", "callback_data": "join_game"},
        {"text": "🚪 مغادرة اللعبة", "callback_data": "leave_game"},
    ],
    [
        {"text": "🚀 بدء اللعبة", "callback_data": "start_game"},
        {"text": "❌ إلغاء اللعبة", "callback_data": "cancel_game"},
    ],
    [{"text": "📌 أظهر لوحة الأزرار بالأسفل", "callback_data": "refresh_panel"}],
]


def preservation_property(test):
    configured = settings(max_examples=25, deadline=None, database=None)(test)
    return seed(SEED)(configured)


class FakeHandle:
    def __init__(self, callback):
        self.callback = callback
        self.cancelled = False
        self.fired = False

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if not self.cancelled and not self.fired:
            self.fired = True
            self.callback()


class FakeClockScheduler:
    def __init__(self):
        self.calls = []

    def __call__(self, delay, callback):
        handle = FakeHandle(callback)
        self.calls.append((delay, handle))
        return handle

    def fire_delay(self, delay):
        for scheduled_delay, handle in self.calls:
            if scheduled_delay == delay:
                handle.fire()


class SuccessfulTransport:
    def __init__(self):
        self.calls = []

    async def send_message(self, target_id, text, reply_markup=None):
        self.calls.append(
            {
                "kind": "message",
                "target": target_id,
                "text": text,
                "buttons": (reply_markup or {}).get("inline_keyboard"),
            }
        )
        return {"ok": True}

    async def send_photo(self, target_id, file_id, text, reply_markup=None):
        self.calls.append(
            {
                "kind": "photo",
                "target": target_id,
                "file_id": file_id,
                "text": text,
                "buttons": (reply_markup or {}).get("inline_keyboard"),
            }
        )
        return {"ok": True}


def normalized_notification(notification):
    return {
        "channel": notification.channel,
        "target": notification.target_id,
        "text": notification.text,
        "buttons": deepcopy(notification.buttons),
    }


def normalized_session(session):
    return {
        "phase": session.state.value,
        "host": session.host_id,
        "roster": [
            {
                "id": player.user_id,
                "name": player.display_name,
                "active": player.active,
                "role": "spy" if player.is_spy else "citizen",
            }
            for player in session.players.values()
        ],
        "spy": session.spy_user_id,
        "secret_name": session.secret_location_name,
        "votes": dict(sorted(session.votes.items())),
        "voting": session.voting_active,
        "spy_guessing": session.spy_guessing_active,
    }


def expected_transport_call(notification):
    observable = normalized_notification(notification)
    return {
        "kind": "photo" if notification.photo_file_id else "message",
        "target": observable["target"],
        "text": observable["text"],
        "buttons": observable["buttons"],
    }


GOLDEN_LOBBY = {
    "phase": "lobby",
    "host": 2,
    "roster": [
        {"id": 2, "name": "Bob", "active": True, "role": "citizen"},
    ],
    "memberships": {1: [], 2: [1001], 3: []},
    "texts": [
        (
            "🕵️ <b>Alice</b> بدأ لعبة <b>الجاسوس والكلمة السرية</b>!\n\n"
            "<blockquote expandable>\n<b>💡 فكرة اللعبة:</b>\n"
            "البوت يرسل كلمة سرية واحدة بالخاص لجميع اللاعبين، ولكنه يختار شخصاً واحداً "
            "ليكون <b>الجاسوس 🕵️</b> (لا يعرف الكلمة!).\n"
            "اطرحوا أسئلة على بعضكم في المحادثة لاكتشاف الجاسوس دون إفشاء الكلمة السرية!\n"
            "</blockquote>\n\n<b>📋 طريقة اللعب:</b>\n"
            "1️⃣ اضغط <b>➕ انضمام للعبة</b> أدناه.\n"
            "2️⃣ اضغط <b>🚀 بدء اللعبة</b> للتصويت وتلقي الكلمات السرية بالخاص!\n\n"
            "👥 <b>اللوبي مفتوح الآن:</b> (1/15 لاعبين)"
        ),
        "👤 <b>Bob</b> انضم للعبة! (2/15 لاعبين)",
        "👤 <b>Carol</b> انضم للعبة! (3/15 لاعبين)",
        "🚪 Carol غادر اللعبة. (2/15 لاعبين)",
        "🚪 Alice غادر اللعبة.\nأصبح <b>Bob</b> منشئ اللعبة (Host) الآن.",
    ],
}

GOLDEN_START = {
    "phase": "guessing",
    "host": 1,
    "roster": [
        {"id": 1, "name": "Alice", "active": True, "role": "spy"},
        {"id": 2, "name": "Bob", "active": True, "role": "citizen"},
        {"id": 3, "name": "Carol", "active": True, "role": "citizen"},
    ],
    "spy": 1,
    "secret_name": "المستشفى",
    "votes": {},
    "voting": False,
    "spy_guessing": False,
    "timer_delays": [150.0, 300],
}

GOLDEN_START_TEXTS = [
    (
        "🕵️ <b>تم توزيع الكلمات السرية بالخاص لجميع اللاعبين!</b>\n\n"
        "• هناك <b>جاسوس واحد</b> بينكم لا يعرف الكلمة السرية!\n"
        "• ابدأوا النقاش والأسئلة فوراً في المحادثة.\n"
        "• عند الجاهزية، اضغطوا <b>🗳️ بدء التصويت على الجاسوس</b> أدناه."
    ),
    (
        "🚨 <b>أنت الجاسوس الوحيد في هذه الجولة! 🕵️‍♂️</b>\n\n"
        "❌ أنت لا تعرف الكلمة السرية للموقع!\n"
        "💡 تظاهر بأنك تعرف الكلمة واستمع لأسئلة المنافسين في المحادثة بذكاء حتى تكتشف المكان!"
    ),
    (
        "👥 <b>أنت مواطن شريف! (لست الجاسوس) ✅</b>\n\n"
        "🤫 <b>الكلمة السرية للموقع هي: المستشفى</b>\n\n"
        "احذر أن يكتشفك الجاسوس! اسأل أسئلة ذكية في المحادثة لاكتشاف الجاسوس دون كشف الكلمة السرية."
    ),
]


GOLDEN_PANEL = {
    "channel": "group",
    "target": 2001,
    "text": (
        "⚙️ <b>لوحة التحكم الحالية للعبة:</b>\n\n"
        "🕵️ <b>لوحة التحكم للجولة النشطة:</b>\n"
        "الأسئلة مستمرة في المجموعة! عند الجاهزية اضغط زر التصويت أدناه."
    ),
    "buttons": [
        [{"text": "🗳️ بدء التصويت على الجاسوس", "callback_data": "start_voting"}],
        [{"text": "💡 تخمين الكلمة السرية (الجاسوس)", "callback_data": "spy_guess_menu"}],
        [{"text": "📌 أظهر لوحة الأزرار بالأسفل", "callback_data": "refresh_panel"}],
    ],
}

GOLDEN_VOTE = {
    "open_text": (
        "🗳️ <b>بدأ التصويت على الجاسوس!</b>\n\n"
        "اضغط على اسم اللاعب الذي تشك أنه الجاسوس أدناه:"
    ),
    "open_buttons": [
        [
            {"text": "👤 Alice", "callback_data": "vote:1"},
            {"text": "👤 Bob", "callback_data": "vote:2"},
        ],
        [{"text": "👤 Carol", "callback_data": "vote:3"}],
    ],
    "first_phase": "guessing",
    "first_votes": {1: 2},
    "first_text": "🗳️ قام <b>Alice</b> بالتصويت! (1/3 أصوات)",
    "final_phase": "completed",
    "final_votes": {1: 2, 2: 2, 3: 2},
    "final_texts": [
        "🗳️ قام <b>Carol</b> بالتصويت! (3/3 أصوات)",
        (
            "🎉 <b>فاز الجاسوس! 🕵️🏆</b>\n\n"
            "قام الجميع بطرد خاطئ لـ <b>Bob</b>!\n"
            "بينما الجاسوس الحقيقي <b>Alice</b> نجح بالتمويه والمكر وخدع الجميع!\n"
            "📍 المكان السري كان: <b>المستشفى</b>"
        ),
    ],
}

GOLDEN_CANCEL = {
    "phase": "cancelled",
    "memberships": {1: [], 2: []},
    "text": "❌ قام <b>Alice</b> بإلغاء اللعبة.",
}

GOLDEN_TIMEOUT = {
    "delays": [150.0, 300],
    "current_phase": "completed",
    "current_texts": [
        "🎭 <b>انتهى الوقت! إليك أصحاب الصور الحقيقيين:</b>",
        (
            "🏆 <b>الترتيب والنتائج النهائية:</b>\n"
            "  👤 Alice: <b>0</b> نقطة\n"
            "  👤 Bob: <b>0</b> نقطة\n\n"
            "🎉 <b>الفائزون بالمركز الأول (تعادل بـ 0 نقاط): Alice, Bob!</b>"
        ),
    ],
    "cancelled_phase": "cancelled",
}


def test_golden_lobby_create_join_leave_and_host_transfer():
    store = SessionStore()
    manager = SessionManager(store)

    results = [
        manager.create_session(1001, 1, "Alice"),
        manager.join_session(1001, 2, "Bob"),
        manager.join_session(1001, 3, "Carol"),
        manager.leave_session(1001, 3),
        manager.leave_session(1001, 1),
    ]

    observed_state = normalized_session(store.get(1001))
    assert all(result.ok for result in results)
    assert observed_state["phase"] == GOLDEN_LOBBY["phase"]
    assert observed_state["host"] == GOLDEN_LOBBY["host"]
    assert observed_state["roster"] == GOLDEN_LOBBY["roster"]
    assert [result.notifications[0].text for result in results] == GOLDEN_LOBBY["texts"]
    assert all(result.notifications[0].buttons == LOBBY_BUTTONS for result in results)
    assert {
        user_id: sorted(store.group_chat_ids_for_user(user_id))
        for user_id in (1, 2, 3)
    } == GOLDEN_LOBBY["memberships"]


def test_golden_start_success_panel_first_vote_and_unique_resolution():
    async def scenario():
        store = SessionStore()
        scheduler = FakeClockScheduler()
        timer = TimerService(scheduler)
        manager = SessionManager(store, timer_service=timer)
        transport = SuccessfulTransport()
        adapter = TelegramAdapter(
            store,
            session_manager=manager,
            timer_service=timer,
            send_message_fn=transport.send_message,
            send_photo_fn=transport.send_photo,
        )
        await adapter.handle_newgame(2001, 1, "Alice")
        await adapter.handle_join(2001, 2, "Bob")
        await adapter.handle_join(2001, 3, "Carol")
        transport.calls.clear()

        start = await adapter.handle_startgame(2001, 1)
        start_state = deepcopy(normalized_session(store.get(2001)))
        start_notifications = deepcopy(start.notifications)
        panel = adapter._build_status_panel_notification(
            2001, "⚙️ <b>لوحة التحكم الحالية للعبة:</b>"
        )
        opened = manager.start_voting_panel(2001)
        first = manager.record_spy_vote(2001, 1, 2)
        first_state = deepcopy(normalized_session(store.get(2001)))
        manager.record_spy_vote(2001, 2, 2)
        final = manager.record_spy_vote(2001, 3, 2)
        final_state = deepcopy(normalized_session(store.get(2001)))
        return locals()

    with patch("photo_guess_game.session_manager.get_random_location", return_value=LOCATION), patch(
        "photo_guess_game.session_manager.random.choice", side_effect=lambda values: values[0]
    ):
        observed = asyncio.run(scenario())

    assert observed["start"].ok is True
    assert observed["start_state"] == {
        key: value for key, value in GOLDEN_START.items() if key != "timer_delays"
    }
    assert [delay for delay, _ in observed["scheduler"].calls] == GOLDEN_START["timer_delays"]
    assert [n.text for n in observed["start_notifications"]] == [
        GOLDEN_START_TEXTS[0], GOLDEN_START_TEXTS[1], GOLDEN_START_TEXTS[2], GOLDEN_START_TEXTS[2]
    ]
    assert [n.target_id for n in observed["start_notifications"]] == [2001, 1, 2, 3]
    assert observed["transport"].calls == [
        expected_transport_call(notification)
        for notification in observed["start_notifications"]
    ]
    assert normalized_notification(observed["panel"]) == GOLDEN_PANEL

    opened_notification = observed["opened"].notifications[0]
    assert opened_notification.text == GOLDEN_VOTE["open_text"]
    assert opened_notification.buttons == GOLDEN_VOTE["open_buttons"]
    assert observed["first_state"]["phase"] == GOLDEN_VOTE["first_phase"]
    assert observed["first_state"]["votes"] == GOLDEN_VOTE["first_votes"]
    assert observed["first"].notifications[0].text == GOLDEN_VOTE["first_text"]
    assert observed["final_state"]["phase"] == GOLDEN_VOTE["final_phase"]
    assert observed["final_state"]["votes"] == GOLDEN_VOTE["final_votes"]
    assert [n.text for n in observed["final"].notifications] == GOLDEN_VOTE["final_texts"]


def test_golden_groups_shared_membership_and_host_cancellation():
    store = SessionStore()
    manager = SessionManager(store)
    assert manager.create_session(3001, 10, "AHost").ok
    assert manager.join_session(3001, 99, "Shared").ok
    assert manager.create_session(3002, 20, "BHost").ok
    before_a = deepcopy(normalized_session(store.get(3001)))
    assert manager.join_session(3002, 99, "Shared").ok
    assert manager.join_session(3002, 21, "BOnly").ok

    assert normalized_session(store.get(3001)) == before_a
    assert sorted(store.group_chat_ids_for_user(99)) == [3001, 3002]
    assert [player["name"] for player in normalized_session(store.get(3002))["roster"]] == [
        "BHost", "Shared", "BOnly"
    ]

    cancel_store = SessionStore()
    cancel_manager = SessionManager(cancel_store)
    assert cancel_manager.create_session(4001, 1, "Alice").ok
    assert cancel_manager.join_session(4001, 2, "Bob").ok
    cancelled = cancel_manager.cancel_session(4001, 1)
    assert cancelled.ok is True
    assert normalized_session(cancelled.session)["phase"] == GOLDEN_CANCEL["phase"]
    assert cancelled.notifications[0].text == GOLDEN_CANCEL["text"]
    assert {
        user_id: sorted(cancel_store.group_chat_ids_for_user(user_id))
        for user_id in (1, 2)
    } == GOLDEN_CANCEL["memberships"]


def _run_timeout_flow(group_id, cancel_before_expiry):
    observed_notifications = []
    store = SessionStore()
    scheduler = FakeClockScheduler()
    timer = TimerService(scheduler)
    manager = SessionManager(
        store,
        timer_service=timer,
        on_notification_cb=lambda notifications: observed_notifications.extend(notifications),
    )
    assert manager.create_session(group_id, 1, "Alice").ok
    assert manager.join_session(group_id, 2, "Bob").ok
    assert manager.start_session(group_id, 1).ok
    if cancel_before_expiry:
        assert manager.cancel_session(group_id, 1).ok
    scheduler.fire_delay(300)
    scheduler.fire_delay(300)
    return store, scheduler, observed_notifications


def test_golden_current_and_cancelled_timeout_without_real_sleep():
    with patch("photo_guess_game.session_manager.get_random_location", return_value=LOCATION), patch(
        "photo_guess_game.session_manager.random.choice", side_effect=lambda values: values[0]
    ):
        current_store, current_scheduler, current_notifications = _run_timeout_flow(5001, False)
        cancelled_store, cancelled_scheduler, cancelled_notifications = _run_timeout_flow(5002, True)

    assert [delay for delay, _ in current_scheduler.calls] == GOLDEN_TIMEOUT["delays"]
    assert normalized_session(current_store.get(5001))["phase"] == GOLDEN_TIMEOUT["current_phase"]
    assert [notification.text for notification in current_notifications] == GOLDEN_TIMEOUT["current_texts"]
    assert normalized_session(cancelled_store.get(5002))["phase"] == GOLDEN_TIMEOUT["cancelled_phase"]
    assert cancelled_notifications == []
    assert all(handle.cancelled for _, handle in cancelled_scheduler.calls)


def test_golden_successful_telegram_send_occurs_once():
    async def scenario():
        transport = SuccessfulTransport()
        adapter = TelegramAdapter(
            SessionStore(),
            send_message_fn=transport.send_message,
            send_photo_fn=transport.send_photo,
        )
        notification = Notification(
            channel="group",
            target_id=6001,
            text="baseline-once",
            buttons=[[{"text": "OK", "callback_data": "ok"}]],
        )
        await adapter.dispatch_notifications([notification])
        return transport.calls

    assert asyncio.run(scenario()) == [
        {
            "kind": "message",
            "target": 6001,
            "text": "baseline-once",
            "buttons": [[{"text": "OK", "callback_data": "ok"}]],
        }
    ]


@preservation_property
@given(user_ids=st.lists(st.integers(min_value=1, max_value=10000), min_size=2, max_size=8, unique=True))
def test_property_2_valid_sequential_lobby_flow_matches_reference(user_ids):
    """Property 2: valid sequential create/join preserves roster and messages.

    **Validates: Requirements 3.1, 3.2, 3.12**
    """
    group_id = 7001
    store = SessionStore()
    manager = SessionManager(store)
    names = {user_id: f"P{index}" for index, user_id in enumerate(user_ids, start=1)}

    created = manager.create_session(group_id, user_ids[0], names[user_ids[0]])
    joins = [
        manager.join_session(group_id, user_id, names[user_id])
        for user_id in user_ids[1:]
    ]

    assert created.ok and all(result.ok for result in joins)
    assert list(store.get(group_id).players) == user_ids
    assert store.get(group_id).host_id == user_ids[0]
    assert created.notifications[0].target_id == group_id
    assert created.notifications[0].buttons == LOBBY_BUTTONS
    assert [result.notifications[0].text for result in joins] == [
        f"👤 <b>{names[user_id]}</b> انضم للعبة! ({count}/15 لاعبين)"
        for count, user_id in enumerate(user_ids[1:], start=2)
    ]
    assert all(store.group_chat_ids_for_user(user_id) == frozenset({group_id}) for user_id in user_ids)


@preservation_property
@given(ids=st.lists(st.integers(min_value=1, max_value=100000), min_size=6, max_size=6, unique=True))
def test_property_2_cross_group_interleaving_preserves_projection_and_shared_membership(ids):
    """Property 2: healthy group-B events cannot change group-A observables.

    **Validates: Requirements 3.4, 3.11, 3.12**
    """
    group_a, group_b, host_a, host_b, shared_user, b_only = ids
    store = SessionStore()
    manager = SessionManager(store)
    assert manager.create_session(group_a, host_a, "AHost").ok
    assert manager.join_session(group_a, shared_user, "Shared").ok
    projection_a = deepcopy(normalized_session(store.get(group_a)))

    assert manager.create_session(group_b, host_b, "BHost").ok
    assert manager.join_session(group_b, shared_user, "Shared").ok
    assert manager.join_session(group_b, b_only, "BOnly").ok

    assert normalized_session(store.get(group_a)) == projection_a
    assert store.group_chat_ids_for_user(shared_user) == frozenset({group_a, group_b})
    assert set(store.get(group_a).players).isdisjoint({host_b, b_only})
    assert set(store.get(group_b).players).isdisjoint({host_a})


@preservation_property
@given(player_count=st.integers(min_value=2, max_value=10))
def test_property_2_all_success_start_preserves_roles_word_and_single_delivery(player_count):
    """Property 2: healthy all-success start keeps one spy and one send each.

    **Validates: Requirements 3.3, 3.9, 3.10**
    """
    store = SessionStore()
    scheduler = FakeClockScheduler()
    timer = TimerService(scheduler)
    manager = SessionManager(store, timer_service=timer)
    assert manager.create_session(8001, 1, "P1").ok
    for user_id in range(2, player_count + 1):
        assert manager.join_session(8001, user_id, f"P{user_id}").ok

    with patch("photo_guess_game.session_manager.get_random_location", return_value=LOCATION), patch(
        "photo_guess_game.session_manager.random.choice", side_effect=lambda values: values[0]
    ):
        result = manager.start_session(8001, 1)

    assert result.ok is True
    assert result.session.state == GameState.GUESSING
    assert result.session.spy_user_id == 1
    assert sum(player.is_spy for player in result.session.players.values()) == 1
    assert result.notifications[0].channel == "group"
    dm_notifications = result.notifications[1:]
    assert [notification.target_id for notification in dm_notifications] == list(
        range(1, player_count + 1)
    )
    assert len(dm_notifications) == player_count
    assert "أنت الجاسوس الوحيد" in dm_notifications[0].text
    assert all("الكلمة السرية للموقع هي: المستشفى" in notification.text for notification in dm_notifications[1:])
    assert [delay for delay, _ in scheduler.calls] == [150.0, 300]
