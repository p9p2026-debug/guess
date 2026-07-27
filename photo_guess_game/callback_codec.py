"""Compact, generation-bound Telegram callback data codec.

The payload deliberately contains no actor identity. Callers must derive the
actor from Telegram's ``callback_query.from.id`` and authorize it separately.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Literal, cast

CallbackAction = Literal["jn", "lv", "st", "cn", "rf", "vo", "vt", "sg", "rt", "dm"]
MalformedCallback = Literal["malformed_callback"]

VERSION: Final = "v1"
SEPARATOR: Final = "|"
MAX_CALLBACK_BYTES: Final = 64
MAX_NUMERIC_COMPONENT: Final = (1 << 63) - 1
MALFORMED_CALLBACK: Final[MalformedCallback] = "malformed_callback"
ALLOWED_ACTIONS: Final[frozenset[str]] = frozenset(
    {"jn", "lv", "st", "cn", "rf", "vo", "vt", "sg", "rt", "dm"}
)
MUTATING_ACTIONS: Final[frozenset[str]] = frozenset(
    {"jn", "lv", "st", "cn", "vo", "vt", "sg", "rt", "dm"}
)
_BASE36_RE: Final = re.compile(r"(?:0|[1-9a-z][0-9a-z]*)\Z")
_BASE36_ALPHABET: Final = "0123456789abcdefghijklmnopqrstuvwxyz"


@dataclass(frozen=True, slots=True)
class CallbackPayload:
    """Validated callback fields; actor identity is intentionally absent."""

    generation: int
    phase_or_ballot: int
    action: CallbackAction
    arg: int


def _encode_base36(value: int) -> str:
    if value == 0:
        return "0"
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append(_BASE36_ALPHABET[remainder])
    return "".join(reversed(digits))


def _validate_number(name: str, value: object, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    minimum = 1 if positive else 0
    if not minimum <= value <= MAX_NUMERIC_COMPONENT:
        raise ValueError(
            f"{name} must be between {minimum} and {MAX_NUMERIC_COMPONENT}"
        )
    return value


def encode_callback(
    generation: int,
    phase_or_ballot: int,
    action: CallbackAction | str,
    arg: int = 0,
) -> str:
    """Encode one canonical callback payload within Telegram's 64-byte limit."""
    generation = _validate_number("generation", generation, positive=True)
    phase_or_ballot = _validate_number("phase_or_ballot", phase_or_ballot)
    arg = _validate_number("arg", arg)
    if not isinstance(action, str) or action not in ALLOWED_ACTIONS:
        raise ValueError("action is not allowlisted")

    encoded = SEPARATOR.join(
        (
            VERSION,
            _encode_base36(generation),
            _encode_base36(phase_or_ballot),
            action,
            _encode_base36(arg),
        )
    )
    if len(encoded.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError("callback data exceeds Telegram's 64-byte limit")
    return encoded


def _decode_base36(component: str) -> int | None:
    if _BASE36_RE.fullmatch(component) is None:
        return None
    try:
        value = int(component, 36)
    except ValueError:
        return None
    if value > MAX_NUMERIC_COMPONENT:
        return None
    return value


def decode_callback(payload: object) -> CallbackPayload | MalformedCallback:
    """Decode bytes/text without raising for malformed callback data.

    Only canonical lowercase base36 is accepted, preventing multiple textual
    representations of the same generation-bound action.
    """
    if isinstance(payload, bytes):
        if len(payload) > MAX_CALLBACK_BYTES:
            return MALFORMED_CALLBACK
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            return MALFORMED_CALLBACK
    elif isinstance(payload, str):
        if len(payload) > MAX_CALLBACK_BYTES:
            return MALFORMED_CALLBACK
        text = payload
    else:
        return MALFORMED_CALLBACK

    try:
        if len(text.encode("utf-8")) > MAX_CALLBACK_BYTES:
            return MALFORMED_CALLBACK
    except UnicodeEncodeError:
        return MALFORMED_CALLBACK

    parts = text.split(SEPARATOR)
    if len(parts) != 5 or parts[0] != VERSION:
        return MALFORMED_CALLBACK

    generation = _decode_base36(parts[1])
    phase_or_ballot = _decode_base36(parts[2])
    arg = _decode_base36(parts[4])
    action = parts[3]
    if (
        generation is None
        or generation == 0
        or phase_or_ballot is None
        or arg is None
        or action not in ALLOWED_ACTIONS
    ):
        return MALFORMED_CALLBACK

    return CallbackPayload(
        generation=generation,
        phase_or_ballot=phase_or_ballot,
        action=cast(CallbackAction, action),
        arg=arg,
    )


# Short aliases for callers that prefer module-qualified ``encode``/``decode``.
encode = encode_callback
decode = decode_callback