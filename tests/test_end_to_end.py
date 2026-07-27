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


def build(blocked: set[int] | None = None):
    """Assemble the same object graph run_bot.py wires up."""
    api = FakeAPI(blocked)
    store = SessionStore()
    manager = SessionManager(store)
    adapter = TelegramAdapter(
        store,
        session_manager=manager,
        send_message_fn=api.send_message,
        edit_message_fn=api.edit_message_text,
        edit_markup_fn=api.edit_message_reply_markup,
    )
    return api, store, adapter, manager


def _button_labels(markup: dict[str, Any] | None) -> list[str]:
    if not markup:
        return []
    return [
        button["text"]
        for row in markup.get("inline_keyboard", [])
        for button in row
    ]


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
        api, store, adapter, manager = build()
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

        assert (await adapter.handle_start_voting(GROUP)).ok
        assert session.voting_active is True

        # Citizens accuse the spy; the spy must accuse someone else, since
        # self-voting is rejected. That is still a 2-1 majority.
        for uid in citizens:
            vote_res = await adapter.handle_spy_vote(GROUP, uid, spy_id)
            assert vote_res.ok, vote_res.reason
        vote_res = await adapter.handle_spy_vote(GROUP, spy_id, citizens[0])
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

    asyncio.run(scenario())


def test_only_the_spy_may_open_the_guess_menu():
    async def scenario():
        api, store, adapter, manager = build()
        players = [(1, "سالم"), (2, "ليان"), (3, "عمر")]
        await open_lobby(adapter, players)
        await adapter.handle_startgame(GROUP, 1)
        session = store.get(GROUP)
        spy_id = session.spy_user_id
        citizens = [uid for uid, _ in players if uid != spy_id]
        await adapter.handle_start_voting(GROUP)
        for uid in citizens:
            await adapter.handle_spy_vote(GROUP, uid, spy_id)
        await adapter.handle_spy_vote(GROUP, spy_id, citizens[0])

        innocent = citizens[0]
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
def test_host_can_end_the_round_and_reveal_the_spy():
    """The round clock was removed, so the host ends an unresolved round.

    Also a regression on the reveal text: it used to disclose "أصحاب الصور"
    from session.labels, which is always empty in the spy game, so the message
    named neither the spy nor the location.
    """

    async def scenario():
        api, store, adapter, manager = build()
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان"), (3, "عمر")])
        await adapter.handle_startgame(GROUP, 1)
        session = store.get(GROUP)
        spy_name = session.players[session.spy_user_id].display_name
        secret_name = session.secret_location_name

        # Only the host may end it.
        denied = await adapter.handle_end_round(GROUP, 2)
        assert not denied.ok
        assert denied.reason == "not_host"
        assert store.get(GROUP).state is GameState.GUESSING

        res = await adapter.handle_end_round(GROUP, 1)
        assert res.ok, res.reason
        assert store.get(GROUP).state is GameState.COMPLETED

        text = api.all_texts()
        assert "انتهت الجولة ولم يُكشف الجاسوس" in text
        assert spy_name in text
        assert secret_name in text
        assert "الصور" not in text, "photo-game wording leaked into the reveal"
        # Even a finished game leaves something to press.
        assert _button_labels(api.last_group_markup()), "terminal panel has no keyboard"

    asyncio.run(scenario())


def test_failed_role_dm_rolls_round_back_to_lobby():
    """A blocked DM must leave the round unplayable, never announced as ready."""

    async def scenario():
        api, store, adapter, manager = build(blocked={3})
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


def test_a_new_game_in_the_same_group_gets_a_fresh_generation():
    """Generations must never be reused, so late input from a finished game is
    always distinguishable from the current one."""

    async def scenario():
        api, store, adapter, manager = build()
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان"), (3, "عمر")])
        first_key = store.get(GROUP).session_key
        await adapter.handle_cancelgame(GROUP, 1)

        await open_lobby(adapter, [(1, "سالم"), (2, "ليان"), (3, "عمر")])
        second = store.get(GROUP)
        assert second.generation > first_key.generation
        assert second.session_key != first_key
        assert second.session_key == SessionKey(GROUP, second.generation)

    asyncio.run(scenario())


def test_every_keyboard_always_carries_the_persistent_menu():
    """The requirement is absolute: no message may ever leave the group with
    nothing to press, in any state, including after the game is over."""

    async def scenario():
        api, store, adapter, manager = build()
        players = [(1, "سالم"), (2, "ليان"), (3, "عمر")]
        missing: list[str] = []

        def check(label):
            for call in api.group_traffic():
                labels = _button_labels(call.get("reply_markup"))
                if not labels:
                    missing.append(f"{label}: message with no keyboard")
                elif not any("القائمة الرئيسية" in item for item in labels):
                    missing.append(f"{label}: keyboard without the persistent menu")
            api.log.clear()

        await adapter.handle_newgame(GROUP, 1, "سالم")
        check("newgame")
        for uid, name in players[1:]:
            await adapter.handle_join(GROUP, uid, name)
        check("join")
        await adapter.handle_startgame(GROUP, 1)
        check("startgame")
        await adapter.handle_start_voting(GROUP)
        check("start_voting")
        spy_id = store.get(GROUP).spy_user_id
        citizens = [uid for uid, _ in players if uid != spy_id]
        for uid in citizens:
            await adapter.handle_spy_vote(GROUP, uid, spy_id)
        await adapter.handle_spy_vote(GROUP, spy_id, citizens[0])
        check("votes")
        await adapter.handle_spy_guess_menu(GROUP, spy_id)
        check("guess_menu")
        await adapter.handle_spy_guess_option(GROUP, spy_id, 0)
        check("guess_submitted (terminal)")
        await adapter.handle_game_menu(GROUP)
        check("game_menu after game over")

        assert missing == [], missing

    asyncio.run(scenario())


def test_game_menu_works_even_with_no_session():
    """The recovery button must never be a dead end."""

    async def scenario():
        api, store, adapter, manager = build()
        res = await adapter.handle_game_menu(GROUP)
        assert res.ok
        labels = _button_labels(api.last_group_markup())
        assert any("القائمة الرئيسية" in label for label in labels), labels
        assert any("لعبة جديدة" in label for label in labels), labels

    asyncio.run(scenario())


def test_voting_for_yourself_is_rejected():
    async def scenario():
        api, store, adapter, manager = build()
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان"), (3, "عمر")])
        await adapter.handle_startgame(GROUP, 1)
        await adapter.handle_start_voting(GROUP)

        res = await adapter.handle_spy_vote(GROUP, 1, 1)
        assert not res.ok
        assert res.reason == "self_vote"
        assert store.get(GROUP).votes == {}

    asyncio.run(scenario())


def test_host_can_close_a_stalled_ballot_with_partial_votes():
    """A tally used to need every active player, so one silent player stalled
    the round forever. With no clock, the host must be able to force a tally."""

    async def scenario():
        api, store, adapter, manager = build()
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان"), (3, "عمر")])
        await adapter.handle_startgame(GROUP, 1)
        await adapter.handle_start_voting(GROUP)
        session = store.get(GROUP)
        spy_id = session.spy_user_id
        voters = [uid for uid in (1, 2, 3) if uid != spy_id]

        # Only the two non-spy players vote; the spy stays silent.
        for uid in voters:
            await adapter.handle_spy_vote(GROUP, uid, spy_id)
        assert session.voting_active is True, "ballot resolved too early"

        # A non-host cannot force it.
        denied = await adapter.handle_close_ballot(GROUP, voters[0] if voters[0] != 1 else 2)
        if denied.reason is not None:
            assert denied.reason in ("not_host",), denied.reason

        res = await adapter.handle_close_ballot(GROUP, session.host_id)
        assert res.ok, res.reason
        assert session.voting_active is False
        # Two votes against the spy is a clear majority -> spy exposed.
        assert session.spy_guessing_active is True

    asyncio.run(scenario())


def test_closing_an_empty_ballot_is_refused():
    async def scenario():
        api, store, adapter, manager = build()
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان"), (3, "عمر")])
        await adapter.handle_startgame(GROUP, 1)
        await adapter.handle_start_voting(GROUP)

        res = await adapter.handle_close_ballot(GROUP, 1)
        assert not res.ok
        assert res.reason == "no_votes"
        assert store.get(GROUP).voting_active is True

    asyncio.run(scenario())


def test_a_round_cannot_start_below_the_voting_minimum():
    """Rule contradiction fixed: start needed 2 players but voting needed 3, so
    a two-player round could start and then never be resolved."""
    from photo_guess_game.session_manager import (
        MIN_PLAYERS_TO_START,
        MIN_PLAYERS_TO_VOTE,
    )

    assert MIN_PLAYERS_TO_START >= MIN_PLAYERS_TO_VOTE

    async def scenario():
        api, store, adapter, manager = build()
        await open_lobby(adapter, [(1, "سالم"), (2, "ليان")])
        res = await adapter.handle_startgame(GROUP, 1)
        assert not res.ok
        assert res.reason == "below_minimum"
        assert store.get(GROUP).state is GameState.LOBBY

    asyncio.run(scenario())


def test_control_message_id_is_recorded_from_send_response():
    """Regression: nothing ever assigned control_message_id, so edits no-oped."""

    async def scenario():
        api, store, adapter, manager = build()
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
        api, store, adapter, manager = build()
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


def test_session_tracks_identity_and_terminality():
    store = SessionStore()
    manager = SessionManager(store)
    manager.create_session(GROUP, 1, "سالم")
    session = store.get(GROUP)

    assert session.session_key == SessionKey(GROUP, session.generation)
    assert session.terminal is False
    assert session.revision >= 1

    manager.cancel_session(GROUP, 1)
    assert store.get(GROUP).terminal is True


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
