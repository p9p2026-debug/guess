"""Minimal async Telegram Bot API client built only on the standard library.

Blocking ``urllib`` calls are pushed onto the default thread pool with
``asyncio.to_thread``, which keeps the public surface fully awaitable without
pulling in an HTTP dependency.  Avoiding third-party packages means the
deployment has nothing to install and nothing to break on a version bump.

Every method returns the decoded Telegram envelope (``{"ok": ..., ...}``)
rather than raising on API-level failures, because the game's delivery logic
distinguishes "the user blocked the bot" from "the network broke" and must be
able to inspect ``ok`` itself.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.telegram.org"

#: Long-poll window for getUpdates.  The socket timeout is deliberately larger
#: so the server, not the client, decides when an empty poll ends.
LONG_POLL_SECONDS = 25
SOCKET_TIMEOUT_SECONDS = LONG_POLL_SECONDS + 15

_MAX_ATTEMPTS = 4
_BACKOFF_BASE_SECONDS = 1.5

#: Telegram hard-caps a single message at 4096 UTF-16 code units.
MAX_MESSAGE_LENGTH = 4096


class TelegramAPI:
    """Awaitable wrapper over the subset of Bot API methods the game needs."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        socket_timeout: float = SOCKET_TIMEOUT_SECONDS,
    ) -> None:
        if not token or ":" not in token:
            raise ValueError(
                "a Telegram bot token looks like '123456789:AA...'; "
                "got something that cannot be one"
            )
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._socket_timeout = socket_timeout

    # ------------------------------------------------------------------
    # Transport
    # ------------------------------------------------------------------
    def _endpoint(self, method: str) -> str:
        return f"{self._base_url}/bot{self._token}/{method}"

    def _post_blocking(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            self._endpoint(method),
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._socket_timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as err:
            # 4xx/5xx still carry a JSON Telegram envelope; surface it verbatim
            # so callers can read description/parameters instead of guessing.
            raw = err.read()
            try:
                return json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                return {
                    "ok": False,
                    "error_code": err.code,
                    "description": f"HTTP {err.code} with unparsable body",
                }
        return json.loads(raw.decode("utf-8"))

    async def call(self, method: str, **payload: Any) -> dict[str, Any]:
        """Invoke a Bot API method, retrying transient transport failures.

        Retries cover network errors, 5xx responses, and 429 rate limits (using
        Telegram's ``retry_after`` when present).  Application-level rejections
        such as "bot was blocked by the user" are returned immediately, because
        retrying them would only delay the caller's fallback path.
        """
        payload = {key: value for key, value in payload.items() if value is not None}
        last_error: dict[str, Any] | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                result = await asyncio.to_thread(self._post_blocking, method, payload)
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as err:
                last_error = {"ok": False, "description": f"transport error: {err}"}
            else:
                if result.get("ok"):
                    return result

                error_code = result.get("error_code")
                if error_code == 429:
                    retry_after = 1.0
                    parameters = result.get("parameters")
                    if isinstance(parameters, dict):
                        retry_after = float(parameters.get("retry_after", 1.0))
                    logger.warning(
                        "%s rate limited, sleeping %.1fs", method, retry_after
                    )
                    await asyncio.sleep(retry_after + 0.25)
                    last_error = result
                    continue
                if isinstance(error_code, int) and 500 <= error_code < 600:
                    last_error = result
                else:
                    # Deterministic rejection: no amount of retrying helps.
                    return result

            if attempt < _MAX_ATTEMPTS:
                delay = _BACKOFF_BASE_SECONDS ** attempt + random.uniform(0, 0.4)
                logger.warning(
                    "%s failed (attempt %d/%d), retrying in %.1fs: %s",
                    method,
                    attempt,
                    _MAX_ATTEMPTS,
                    delay,
                    (last_error or {}).get("description"),
                )
                await asyncio.sleep(delay)

        return last_error or {"ok": False, "description": "exhausted retries"}

    # ------------------------------------------------------------------
    # Methods used by the game
    # ------------------------------------------------------------------
    @staticmethod
    def _clamp(text: str) -> str:
        if len(text) <= MAX_MESSAGE_LENGTH:
            return text
        return text[: MAX_MESSAGE_LENGTH - 1] + "…"

    async def get_me(self) -> dict[str, Any]:
        """Fetch the bot account, used at boot to validate the token."""
        return await self.call("getMe")

    async def delete_webhook(self, *, drop_pending_updates: bool = False) -> dict[str, Any]:
        """Remove any webhook so long polling is allowed to start."""
        return await self.call(
            "deleteWebhook", drop_pending_updates=drop_pending_updates
        )

    async def get_updates(
        self, offset: int | None = None, *, timeout: int = LONG_POLL_SECONDS
    ) -> dict[str, Any]:
        """Long-poll for updates, limited to the types this bot reacts to."""
        return await self.call(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query", "my_chat_member"],
        )

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send an HTML message; signature matches the adapter's send hook."""
        return await self.call(
            "sendMessage",
            chat_id=chat_id,
            text=self._clamp(text),
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )

    async def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Edit an existing message in place."""
        return await self.call(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=self._clamp(text),
            parse_mode="HTML",
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )

    async def edit_message_reply_markup(
        self,
        chat_id: int,
        message_id: int,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Replace (or strip) the inline keyboard of an existing message."""
        return await self.call(
            "editMessageReplyMarkup",
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=reply_markup,
        )

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        *,
        show_alert: bool = False,
    ) -> dict[str, Any]:
        """Acknowledge a button press, optionally with a toast or alert."""
        return await self.call(
            "answerCallbackQuery",
            callback_query_id=callback_query_id,
            text=text,
            show_alert=show_alert,
        )
