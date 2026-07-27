"""End-to-end coverage for the Spy Game flow against a fake Telegram API.

Each test drives the real ``TelegramAdapter`` / ``SessionManager`` /
``TimerService`` wiring that ``run_bot.py`` builds, substituting only the HTTP
boundary.  Several tests are regressions for defects found during the audit and
are labelled as such.

Runnable with ``pytest tests/`` or directly with ``python tests/test_end_to_end.py``.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from photo_guess_game.models import GameState, SessionKey  # noqa: E402
from photo_guess_game.session_manager import SessionManager  # noqa: E402
from photo_guess_game.session_store import SessionStore  # noqa: E402
from photo_guess_game.telegram_adapter import TelegramAdapter  # noqa: E402
from photo_guess_game.timer_service import TimerService  # noqa: E402

GROUP = -1001234567890


class FakeAPI:
    """Records outbound calls and mimics Bot API envelopes."""

    def __init__(self, blocked_users: set[int] | None = None) -> None:
        self.blocked = blocked_users or set()
        self.sent: list[dict[str, Any]] = []
        self.edited: list[dict[str, Any]] = []
        self.markup_edits: list[dict[str, Any]] = []
        #: Single chronological log. ``sent`` and ``edited`` are separate views,
        #: so concatenating them does not preserve call order.
        self.log: list[dict[str, Any]] = []
        self._next_message_id = 1000

    async def send_message(self, chat_id, text, reply_markup=None):
        if chat_id in self.blocked:
            return {
                "ok": False,
                "error_code": 403,
                "description": "Forbidden: bot was blocked by the user",
            }
        self._next_message_id += 1
        call = {
            "kind": "send",
            "chat_id": chat_id,
            "text": text,
            "reply_markup": reply_markup,
        }
        self.sent.append(call)
        self.log.append(call)
        return {"ok": True, "result": {"message_id": self._next_message_id}}

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        call = {
            "kind": "edit",
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": reply_markup,
        }
        self.edited.append(call)
        self.log.append(call)
        return {"ok": True, "result": {"message_id": message_id}}

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.markup_edits.append({"chat_id": chat_id, "message_id": message_id})
        return {"ok": True, "result": {"message_id": message_id}}

    # -- assertions helpers -------------------------------------------
    def texts_to(self, chat_id: int) -> list[str]:
        return [call["text"] for call in self.sent if call["chat_id"] == chat_id]

    def group_traffic(self) -> list[dict[str, Any]]:
        """Every group-facing call, sends and edits, in chronological order."""
        return [call for call in self.log if call["chat_id"] == GROUP]

    def last_group_markup(self) -> dict[str, Any] | None:
        traffic = self.group_traffic()
        return traffic[-1]["reply_markup"] if traffic else None

    def all_texts(self) -> str:
        return "\n".join(call["text"] for call in self.sent) + "\n".join(
            call["text"] for call in self.edited
        )


class ManualScheduler:
    """Deterministic ``call_later`` replacement; nothing fires until asked."""

    class Handle:
        def __init__(self) -> None:
            self.cancelled = False

        def cancel(self) -> None:
            self.cancelled = True

    def __init__(self) -> None:
        self.jobs: list[tuple[float, Any, ManualScheduler.Handle]] = []

    def __call__(self, delay, callback):
        handle = self.Handle()
        self.jobs.append((delay, callback, handle))
        return handle

    def fire_longest(self) -> bool:
        """Fire the live job with the largest delay (the round deadline)."""
        live = [job for job in self.jobs if not job[2].cancelled]
        if not live:
            return False
        delay, callback, _ = max(live, key=lambda job: job[0])
        callback()
        return True

    @property
    def live_count(self) -> int:
        return sum(1 for job in self.jobs if not job[2].cancelled)


def build(blocked: set[int] | None = None, *, round_seconds: int = 300):
    """Assemble the same object graph run_bot.py wires up."""
    api = FakeAPI(blocked)
    store = SessionStore()
    scheduler = ManualScheduler()
    collected: list[Any] = []
    timers = TimerService(
        scheduler=scheduler,
        session_lookup=lambda key: store.get(key.group_chat_id),
    )
    manager = SessionManager(
        store,
        timer_service=timers,
        on_notification_cb=collected.extend,
        round_seconds=round_seconds,
    )
    adapter = TelegramAdapter(
        store,
        session_manager=manager,
        timer_service=timers,
        send_message_fn=api.send_message,
        edit_message_fn=api.edit_message_text,
        edit_markup_fn=api.edit_message_reply_markup,
    )
    return api, store, adapter, manager, timers, scheduler, collected


async def open_lobby(adapter, players: list[tuple[int, str]]):
    host_id, host_name = players[0]
    await adapter.handle_newgame(GROUP, host_id, host_name)
    for user_id, name in players[1:]:
        await adapter.handle_join(GROUP, user_id, name)


# ----------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------
def test_full_round_spy_caught_then_guesses_correctly():
    async def scenario():
        api, store, adapter, manager, timers, scheduler, _ = build()
        players = [(1, "سالم"), (2, "ليان"), (3, "عمر")]
        await open_lobby(adapter, players)

        session = store.get(GROUP)
        assert session.state is GameState.LOBBY
        assert len(session.players) == 3

        res = await adapter.handle_startgame(GROUP, 1)
        assert res.ok, res.reason
        assert session.state is GameState.GUESSING

        # Every active player received exactly one role DM.
        for user_id, _ in players:
            assert len(api.texts_to(user_id)) == 1, user_id

        spy_id = session.spy_user_id
        citizens = [uid for uid, _ in players if uid != spy_id]
        # The spy is told they are the spy; citizens receive the secret word.
        assert "أنت الجاسوس" in api.texts_to(spy_id)[0]
        for uid in citizens:
            assert session.secret_location_name in api.texts_to(uid)[0]

        # Two round timers are armed for this exact generation.
        assert timers.pending_for(session.session_key) == 2

        assert (await adapter.handle_start_voting(GROUP)).ok
        assert session.voting_active is True

        # Everyone votes for the spy; the ballot resolves on the last vote.
        for uid, _ in players:
            vote_res = await adapter.handle_spy_vote(GROUP, uid, spy_id)
            assert vote_res.ok, vote_res.reason

        assert session.spy_guessing_active is True
        assert "تم كشف الجاسوس" in api.all_texts()

        menu = await adapter.handle_spy_guess_menu(GROUP, spy_id)
        assert menu.ok, menu.reason
        options = list(session.spy_guess_options)
        assert len(options) == 4
        correct_index = options.index(session.secret_location_word)

        guess = await adapter.handle_spy_guess_option(GROUP, spy_id, correct_index)
        assert guess.ok, guess.reason
        assert session.state is GameState.COMPLETED
        assert "تخمين عبقري" in api.all_texts()
        # Terminal sessions must release their timers.
        assert timers.pending_for(SessionKey(GROUP, session.generation)) == 0

    asyncio.run(scenario())


def test_only_the_spy_may_open_the_guess_menu():
    async def scenario():
        api, store, adapter, *_ = build()
        players = [(1, "سالم"), (2, "ليان"), (3, "عمر")]
        await open_lobby(adapter, players)
        await adapter.handle_startgame(GROUP, 1)
        session = store.get(GROUP)
        spy_id = session.spy_user_id
        await adapter.handle_start_voting(GROUP)
        for uid, _ in players:
            await adapter.handle_spy_vote(GROUP, uid, spy_id)

        innocent = next(uid for uid, _ in players if uid != spy_id)
        denied = await adapter.handle_spy_guess_menu(GROUP, innocent)
        assert not denied.ok
        assert denied.reason == "not_spy"

        # And the ballot cannot be consumed twice.
        await adapter.handle_spy_guess_menu(GROUP, spy_id)
        assert len(session.spy_guess_options) == 4, "menu did not populate options"
        first = await adapter.handle_spy_guess_option(GROUP, spy_id, 0)
        assert first.ok
        second = await adapter.handle_spy_guess_option(GROUP, spy_id, 0)
        assert not second.ok, "a second guess was accepted"

    asyncio.run(scenario())


# ----------------------------------------------------------------------
# Regressions
# ----------------------------------------------------------------------
def test_timeout_reveals_spy_and_location_not_photo_owners():
    """Regression: enter_reveal used to disclose photo labels.

    ``session.labels`` is always empty in the spy game, so the timeout message
    was a bare "أصحاب الصور الحقيقيين" header with no spy and no location.
    """

    async def scenario():
        api, store, adapter, manager, timers, scheduler, collected = build()
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان"), (3, "عمر")])
        await adapter.handle_startgame(GROUP, 1)
        session = store.get(GROUP)
        spy_name = session.players[session.spy_user_id].display_name
        secret_name = session.secret_location_name

        assert scheduler.fire_longest(), "round deadline was never scheduled"

        assert session.state is GameState.COMPLETED
        text = "\n".join(n.text for n in collected)
        assert "انتهى الوقت ولم يُكشف الجاسوس" in text
        assert spy_name in text
        assert secret_name in text
        assert "فاز الجاسوس" in text
        assert "الصور" not in text, "photo-game wording leaked into the reveal"

    asyncio.run(scenario())


def test_failed_role_dm_rolls_round_back_to_lobby():
    """A blocked DM must leave the round unplayable, never announced as ready."""

    async def scenario():
        api, store, adapter, *_ = build(blocked={3})
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان"), (3, "عمر")])
        res = await adapter.handle_startgame(GROUP, 1)

        assert not res.ok
        assert res.reason == "role_delivery_failed"
        session = store.get(GROUP)
        assert session.state is GameState.LOBBY
        assert session.spy_user_id is None
        assert session.secret_location_name == ""
        assert all(not p.is_spy and p.secret_word is None for p in session.players.values())

        group_text = "\n".join(api.texts_to(GROUP)) + "\n".join(
            c["text"] for c in api.edited
        )
        assert "فشل إرسال الكلمات السرية" in group_text
        assert "تم توزيع الكلمات السرية" not in group_text

    asyncio.run(scenario())


def test_timer_from_previous_generation_cannot_touch_a_new_session():
    """Regression: the legacy timer API pinned generation 0, which never matched.

    Wiring ``session_lookup`` therefore used to reject every timer silently.
    Now timers carry the real generation and only stale ones are refused.
    """

    async def scenario():
        api, store, adapter, manager, timers, scheduler, collected = build()
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان"), (3, "عمر")])
        await adapter.handle_startgame(GROUP, 1)
        first = store.get(GROUP)
        first_key = first.session_key
        stale_deadline = max(
            (job for job in scheduler.jobs if not job[2].cancelled),
            key=lambda job: job[0],
        )

        # Cancel the game and start a brand-new one in the same group.
        await adapter.handle_cancelgame(GROUP, 1)
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان"), (3, "عمر")])
        await adapter.handle_startgame(GROUP, 1)
        second = store.get(GROUP)

        assert second.generation > first_key.generation
        assert second.session_key != first_key

        collected.clear()
        # Fire the *old* round's deadline callback directly.
        stale_deadline[1]()

        assert second.state is GameState.GUESSING, "stale timer ended the new round"
        assert collected == [], "stale timer produced output"

    asyncio.run(scenario())


def _button_labels(markup: dict[str, Any] | None) -> list[str]:
    if not markup:
        return []
    return [
        button["text"]
        for row in markup.get("inline_keyboard", [])
        for button in row
    ]


def test_a_panel_edit_never_strips_the_keyboard_mid_game():
    """Structural invariant: the round must always stay actionable.

    Regression for the whole class of bug behind "the buttons vanish forever".
    Editing a Telegram message without ``reply_markup`` deletes its keyboard, so
    any panel edit issued while the session is still live must carry one.
    """

    async def scenario():
        api, store, adapter, manager, timers, scheduler, _ = build()
        players = [(1, "سالم"), (2, "ليان"), (3, "عمر")]
        offenders: list[str] = []

        def audit(label, res):
            session = store.get(GROUP)
            for notif in res.notifications:
                if notif.channel != "group" or notif.edit_message_id is None:
                    continue
                alive = session is not None and not session.terminal
                if alive and not notif.buttons:
                    offenders.append(f"{label}: edit with no keyboard")

        audit("newgame", await adapter.handle_newgame(GROUP, 1, "سالم"))
        for uid, name in players[1:]:
            audit("join", await adapter.handle_join(GROUP, uid, name))
        audit("startgame", await adapter.handle_startgame(GROUP, 1))
        audit("start_voting", await adapter.handle_start_voting(GROUP))

        spy_id = store.get(GROUP).spy_user_id
        for uid, _ in players:
            audit("vote", await adapter.handle_spy_vote(GROUP, uid, spy_id))
        audit("guess_menu", await adapter.handle_spy_guess_menu(GROUP, spy_id))
        audit("refresh", await adapter.handle_refresh_panel(GROUP))

        assert offenders == [], offenders

    asyncio.run(scenario())


def test_ballot_survives_the_first_vote():
    """Regression: the first vote used to replace the ballot with a plain text
    edit, deleting the keyboard so nobody else could ever vote."""

    async def scenario():
        api, store, adapter, *_ = build()
        players = [(1, "سالم"), (2, "ليان"), (3, "عمر")]
        await open_lobby(adapter, players)
        await adapter.handle_startgame(GROUP, 1)
        await adapter.handle_start_voting(GROUP)
        session = store.get(GROUP)
        spy_id = session.spy_user_id

        await adapter.handle_spy_vote(GROUP, 1, spy_id)

        labels = _button_labels(api.last_group_markup())
        assert labels, "the ballot keyboard was destroyed by the first vote"
        # All three candidates are still votable.
        for _, name in players:
            assert any(name in label for label in labels), (name, labels)

        # And the remaining voters really can still vote.
        assert (await adapter.handle_spy_vote(GROUP, 2, spy_id)).ok
        assert (await adapter.handle_spy_vote(GROUP, 3, spy_id)).ok
        assert session.spy_guessing_active is True

    asyncio.run(scenario())


def test_started_round_announcement_carries_the_active_panel():
    """Regression: the message said "اضغطوا أدناه" while carrying no keyboard."""

    async def scenario():
        api, store, adapter, *_ = build()
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان"), (3, "عمر")])
        await adapter.handle_startgame(GROUP, 1)

        labels = _button_labels(api.last_group_markup())
        assert any("بدء التصويت" in label for label in labels), labels

    asyncio.run(scenario())


def test_discussion_panel_omits_the_dead_spy_guess_button():
    """The spy guess button only works once the spy is exposed, so it must not
    be offered during discussion where it could only ever answer "unavailable"."""

    async def scenario():
        api, store, adapter, *_ = build()
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان"), (3, "عمر")])
        await adapter.handle_startgame(GROUP, 1)
        labels = _button_labels(api.last_group_markup())
        # Guard against passing vacuously on an empty keyboard.
        assert labels, "the discussion panel has no keyboard at all"
        assert not any("تخمين" in label for label in labels), labels

        # It appears exactly when it becomes actionable.
        await adapter.handle_start_voting(GROUP)
        spy_id = store.get(GROUP).spy_user_id
        for uid in (1, 2, 3):
            await adapter.handle_spy_vote(GROUP, uid, spy_id)
        labels = _button_labels(api.last_group_markup())
        assert any("تخمين" in label for label in labels), labels

    asyncio.run(scenario())


def test_refreshing_the_panel_mid_ballot_keeps_vote_buttons():
    """Regression: refresh_panel hardcoded the discussion keyboard, so using it
    during a ballot swapped the vote buttons for "start voting"."""

    async def scenario():
        api, store, adapter, *_ = build()
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان"), (3, "عمر")])
        await adapter.handle_startgame(GROUP, 1)
        await adapter.handle_start_voting(GROUP)

        await adapter.handle_refresh_panel(GROUP)
        labels = _button_labels(api.last_group_markup())
        assert any("سالم" in label for label in labels), labels
        assert not any("بدء التصويت" in label for label in labels), labels

        # Voting through the refreshed panel still works.
        assert (await adapter.handle_spy_vote(GROUP, 1, store.get(GROUP).spy_user_id)).ok

    asyncio.run(scenario())


def test_tie_reopens_the_panel_instead_of_stranding_the_round():
    """Regression: the tie message dropped the keyboard, leaving no way to
    reopen voting and no way to finish the round."""

    async def scenario():
        api, store, adapter, *_ = build()
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان"), (3, "عمر")])
        await adapter.handle_startgame(GROUP, 1)
        await adapter.handle_start_voting(GROUP)

        # Three voters, three different targets -> a three-way tie.
        await adapter.handle_spy_vote(GROUP, 1, 2)
        await adapter.handle_spy_vote(GROUP, 2, 3)
        await adapter.handle_spy_vote(GROUP, 3, 1)

        session = store.get(GROUP)
        assert session.voting_active is False
        assert session.state is GameState.GUESSING, "a tie must not end the round"
        labels = _button_labels(api.last_group_markup())
        assert any("بدء التصويت" in label for label in labels), labels
        # Voting can genuinely be reopened.
        assert (await adapter.handle_start_voting(GROUP)).ok

    asyncio.run(scenario())


def test_rejected_command_carries_text_for_the_group_to_see():
    """A command rejection must be renderable as a message.

    alert_text is only displayable on a button press, so run_bot relays it as a
    group message. That relay is only possible if the manager supplies text.
    """

    async def scenario():
        api, store, adapter, *_ = build()
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان")])

        duplicate = await adapter.handle_newgame(GROUP, 9, "دخيل")
        assert not duplicate.ok
        assert duplicate.alert_text, "nothing to show the group; bot looks frozen"

        not_host = await adapter.handle_cancelgame(GROUP, 2)
        assert not not_host.ok
        assert not_host.alert_text

        too_few = await adapter.handle_start_voting(GROUP)
        assert not too_few.ok
        assert too_few.alert_text

    asyncio.run(scenario())


def test_control_message_id_is_recorded_from_send_response():
    """Regression: nothing ever assigned control_message_id, so edits no-oped."""

    async def scenario():
        api, store, adapter, *_ = build()
        await adapter.handle_newgame(GROUP, 1, "سالم")
        session = store.get(GROUP)
        assert session.control_message_id is not None

        before = session.control_message_id
        await adapter.handle_join(GROUP, 2, "ليان")
        # The lobby update targets the tracked panel via editMessageText.
        assert any(call["message_id"] == before for call in api.edited)

    asyncio.run(scenario())


def test_second_newgame_is_rejected_while_a_round_is_live():
    async def scenario():
        api, store, adapter, *_ = build()
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان")])
        res = await adapter.handle_newgame(GROUP, 9, "دخيل")
        assert not res.ok
        assert res.reason == "session_already_active"
        assert len(store.get(GROUP).players) == 2

    asyncio.run(scenario())


def test_location_library_has_no_padding_or_duplicate_words():
    """Regression: the library was padded to 500 with synthetic '#N' clones."""
    from photo_guess_game.locations import LOCATIONS

    words = [entry["word"] for entry in LOCATIONS]
    assert len(words) == len(set(words)), "duplicate secret words"
    assert not any("#" in entry["name"] for entry in LOCATIONS)
    assert not any("_" in entry["word"] and entry["word"][-1].isdigit() for entry in LOCATIONS)


def test_session_exposes_generation_bound_guard_fields():
    """timer_service reads .terminal/.session_key/.phase/.revision on sessions."""
    from photo_guess_game.models import RoundPhase

    store = SessionStore()
    manager = SessionManager(store)
    manager.create_session(GROUP, 1, "سالم")
    session = store.get(GROUP)

    assert session.session_key == SessionKey(GROUP, session.generation)
    assert session.terminal is False
    assert session.phase is RoundPhase.LOBBY
    assert session.revision >= 1

    manager.cancel_session(GROUP, 1)
    assert store.get(GROUP).terminal is True
    assert store.get(GROUP).phase is None


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            fn()
        except Exception as err:  # noqa: BLE001 - standalone runner
            failures += 1
            print(f"FAIL {name}: {type(err).__name__}: {err}")
            import traceback

            traceback.print_exc()
        else:
            print(f"ok   {name}")
    print(f"\n{failures} failure(s)")
    raise SystemExit(1 if failures else 0)
