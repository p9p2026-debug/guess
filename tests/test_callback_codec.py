"""Focused tests for generation-bound callback payloads."""

from dataclasses import fields

import pytest

from photo_guess_game.callback_codec import (
    ALLOWED_ACTIONS,
    MALFORMED_CALLBACK,
    MAX_CALLBACK_BYTES,
    MAX_NUMERIC_COMPONENT,
    CallbackPayload,
    decode_callback,
    encode_callback,
)
from photo_guess_game.models import GameSession, GameState, Player
from photo_guess_game.session_store import SessionStore
from photo_guess_game.telegram_adapter import TelegramAdapter


@pytest.mark.parametrize(
    ("generation", "phase_or_ballot", "action", "arg"),
    [
        (1, 0, "jn", 0),
        (35, 36, "vt", 123_456_789),
        (36, 35, "sg", 1),
        (MAX_NUMERIC_COMPONENT, MAX_NUMERIC_COMPONENT, "rt", MAX_NUMERIC_COMPONENT),
        *[(7, 4, action, 0) for action in sorted(ALLOWED_ACTIONS)],
    ],
)
def test_callback_codec_round_trip(generation, phase_or_ballot, action, arg):
    encoded = encode_callback(generation, phase_or_ballot, action, arg)

    assert len(encoded.encode("utf-8")) <= MAX_CALLBACK_BYTES
    assert decode_callback(encoded) == CallbackPayload(
        generation=generation,
        phase_or_ballot=phase_or_ballot,
        action=action,
        arg=arg,
    )
    assert decode_callback(encoded.encode("utf-8")) == decode_callback(encoded)


def test_callback_codec_has_canonical_base36_wire_format():
    assert encode_callback(35, 36, "vt", 123_456_789) == "v1|z|10|vt|21i3v9"


def test_callback_payload_never_carries_actor_identity():
    assert [field.name for field in fields(CallbackPayload)] == [
        "generation",
        "phase_or_ballot",
        "action",
        "arg",
    ]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        1,
        bytearray(b"v1|1|0|vt|1"),
        b"\xff",
        "",
        "v1|1|0|vt",
        "v1|1|0|vt|1|extra",
        "v2|1|0|vt|1",
        "v1||0|vt|1",
        "v1|0|0|vt|1",
        "v1|01|0|vt|1",
        "v1|A|0|vt|1",
        "v1|+1|0|vt|1",
        "v1|1_0|0|vt|1",
        "v1|1||vt|1",
        "v1|1|-1|vt|1",
        "v1|1|0|unknown|1",
        "v1|1|0|VT|1",
        "v1|1|0|vt|",
        "v1|1|0|vt|-1",
        "v1|1|0|vt|01",
        "v1|1|0|vt|zzzzzzzzzzzzzz",
        "v1|1|0|vt|\ud800",
        "🙂" * 20,
        b"x" * (MAX_CALLBACK_BYTES + 1),
    ],
)
def test_decode_is_total_for_malformed_payloads(payload):
    assert decode_callback(payload) == MALFORMED_CALLBACK


@pytest.mark.parametrize(
    ("generation", "phase_or_ballot", "action", "arg"),
    [
        (0, 0, "jn", 0),
        (-1, 0, "jn", 0),
        (True, 0, "jn", 0),
        (1, -1, "jn", 0),
        (1, True, "jn", 0),
        (1, 0, "unknown", 0),
        (1, 0, "jn", -1),
        (1, 0, "jn", MAX_NUMERIC_COMPONENT + 1),
    ],
)
def test_encode_rejects_out_of_range_or_non_allowlisted_fields(
    generation, phase_or_ballot, action, arg
):
    with pytest.raises(ValueError):
        encode_callback(generation, phase_or_ballot, action, arg)


async def _unused_send(*args, **kwargs):
    raise AssertionError("golden label rendering must not perform Telegram I/O")


def _visible_labels(notification):
    return [[button["text"] for button in row] for row in notification.buttons]


def test_control_panel_visible_labels_match_preserved_goldens():
    store = SessionStore()
    session = GameSession(
        group_chat_id=-1001,
        host_id=1,
        state=GameState.LOBBY,
        players={
            1: Player(1, "Alice"),
            2: Player(2, "Bob"),
        },
    )
    store.create(session)
    adapter = TelegramAdapter(
        store=store,
        send_message_fn=_unused_send,
        send_photo_fn=_unused_send,
    )

    lobby = adapter._build_status_panel_notification(-1001, "status")
    assert _visible_labels(lobby) == [
        ["➕ انضمام للعبة", "🚪 مغادرة اللعبة"],
        ["🚀 بدء اللعبة", "❌ إلغاء اللعبة"],
        ["📌 أظهر لوحة الأزرار بالأسفل"],
    ]

    session.state = GameState.GUESSING
    discussion = adapter._build_status_panel_notification(-1001, "status")
    assert _visible_labels(discussion) == [
        ["🗳️ بدء التصويت على الجاسوس"],
        ["💡 تخمين الكلمة السرية (الجاسوس)"],
        ["📌 أظهر لوحة الأزرار بالأسفل"],
    ]

    session.voting_active = True
    voting = adapter._build_status_panel_notification(-1001, "status")
    assert _visible_labels(voting) == [
        ["👤 Alice"],
        ["👤 Bob"],
        ["📌 أظهر لوحة الأزرار بالأسفل"],
    ]