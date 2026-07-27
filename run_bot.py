#!/usr/bin/env python3
"""Entry point for the Telegram Spy Game bot (لعبة الجاسوس والكلمة السرية).

Runs long polling against the Bot API with no third-party dependencies, plus a
tiny HTTP health endpoint so the process can be hosted as a Render Web Service
(Render terminates any web service that never binds ``$PORT``).

Configuration (environment variables):
    BOT_TOKEN   required. Bot token from @BotFather.
    PORT        optional. Health-check port; Render injects this.
    LOG_LEVEL   optional. Python log level name (default INFO).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Coroutine

from photo_guess_game.session_manager import SessionManager
from photo_guess_game.session_store import SessionStore
from photo_guess_game.telegram_adapter import TelegramAdapter
from photo_guess_game.telegram_api import (
    DEFAULT_BASE_URL,
    LONG_POLL_SECONDS,
    TelegramAPI,
)

logger = logging.getLogger("spygame")

HELP_TEXT = (
    "🕵️ <b>لعبة الجاسوس والكلمة السرية</b>\n\n"
    "<b>الأوامر:</b>\n"
    "/newgame — فتح لوبي لعبة جديدة في المجموعة\n"
    "/vote — فتح التصويت على الجاسوس\n"
    "/panel — إظهار لوحة الأزرار من جديد بأسفل المحادثة\n"
    "/cancel — إلغاء اللعبة الحالية (المنشئ فقط)\n"
    "/help — عرض هذه الرسالة\n\n"
    "<b>ملاحظة:</b> لا يوجد وقت محدد للجولة. الجولة تنتهي بالتصويت، "
    "أو بضغط المنشئ <b>🏁 إنهاء الجولة وكشف الجاسوس</b>.\n\n"
    "<b>كيف تلعبون:</b>\n"
    "1️⃣ أضف البوت لمجموعة وأرسل /newgame\n"
    "2️⃣ كل لاعب يضغط <b>➕ انضمام للعبة</b>\n"
    "3️⃣ <b>مهم:</b> كل لاعب يجب أن يبدأ محادثة خاصة مع البوت أولاً، "
    "وإلا تعذّر إرسال الكلمة السرية له وستُلغى الجولة\n"
    "4️⃣ المنشئ يضغط <b>🚀 بدء اللعبة</b>"
)

PRIVATE_WELCOME = (
    "✅ <b>تم تفعيل المحادثة الخاصة!</b>\n\n"
    "أصبح بإمكاني إرسال كلمتك السرية هنا عند بدء أي جولة.\n"
    "الآن ارجع إلى المجموعة وانضم للعبة. 🕵️"
)


# ----------------------------------------------------------------------
# Health endpoint
# ----------------------------------------------------------------------
class _HealthHandler(BaseHTTPRequestHandler):
    """Answers Render's health probe and nothing else."""

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"spy game bot: polling\n")

    def log_message(self, *_args: Any) -> None:
        """Silence the default per-request stderr logging."""


def start_health_server(port: int) -> ThreadingHTTPServer:
    """Serve the health endpoint from a daemon thread."""
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    threading.Thread(
        target=server.serve_forever, name="health", daemon=True
    ).start()
    logger.info("health endpoint listening on port %d", port)
    return server


# ----------------------------------------------------------------------
# Update routing
# ----------------------------------------------------------------------
class BotRunner:
    """Wires the game components to the Bot API and pumps the update loop."""

    def __init__(self, api: TelegramAPI, *, bot_username: str) -> None:
        self._api = api
        self._store = SessionStore()
        self._stopping = asyncio.Event()
        # Strong references to fire-and-forget tasks; without this the event
        # loop is free to garbage-collect a task mid-flight.
        self._background: set[asyncio.Task[Any]] = set()

        self._manager = SessionManager(self._store)
        self._adapter = TelegramAdapter(
            self._store,
            session_manager=self._manager,
            send_message_fn=api.send_message,
            edit_message_fn=api.edit_message_text,
            edit_markup_fn=api.edit_message_reply_markup,
            bot_username=bot_username,
        )
        self._offset: int | None = None

    # -- task plumbing -------------------------------------------------
    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # -- routing -------------------------------------------------------
    @staticmethod
    def _display_name(user: dict[str, Any]) -> str:
        name = " ".join(
            part for part in (user.get("first_name"), user.get("last_name")) if part
        ).strip()
        return name or user.get("username") or f"user {user.get('id')}"

    async def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        user = message.get("from") or {}
        chat_id = chat.get("id")
        user_id = user.get("id")
        if chat_id is None or user_id is None:
            return

        text = (message.get("text") or "").strip()
        is_private = chat.get("type") == "private"

        if is_private:
            # Any private message proves the bot can DM this user, which is the
            # precondition the round-start delivery check depends on.
            self._adapter.mark_user_dm_ready(user_id)
            if text.startswith("/start") or text.startswith("/help"):
                await self._api.send_message(chat_id, PRIVATE_WELCOME)
            return

        if not text.startswith("/"):
            return

        command = text.split()[0].split("@")[0].lower()
        res = None

        if command in ("/newgame", "/start", "/game"):
            res = await self._adapter.handle_newgame(
                chat_id, user_id, self._display_name(user)
            )
        elif command in ("/cancel", "/cancelgame", "/endgame"):
            res = await self._adapter.handle_cancelgame(chat_id, user_id)
        elif command in ("/vote", "/voting"):
            res = await self._adapter.handle_start_voting(chat_id)
        elif command in ("/panel", "/buttons"):
            res = await self._adapter.handle_refresh_panel(chat_id)
        elif command == "/help":
            await self._api.send_message(chat_id, HELP_TEXT)
            return
        else:
            return

        # A rejected command carries its explanation in alert_text, which only
        # button presses can display. Without this the group saw total silence
        # and the bot looked frozen.
        if res is not None and not res.ok and not res.notifications and res.alert_text:
            await self._api.send_message(chat_id, res.alert_text)

    async def _handle_callback(self, query: dict[str, Any]) -> None:
        query_id = query.get("id")
        user = query.get("from") or {}
        user_id = user.get("id")
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        data = query.get("data") or ""

        if query_id is None or user_id is None or chat_id is None:
            return

        display_name = self._display_name(user)
        res = None

        try:
            if data == "join_game":
                res = await self._adapter.handle_join(chat_id, user_id, display_name)
            elif data == "leave_game":
                res = await self._adapter.handle_leave(chat_id, user_id)
            elif data == "start_game":
                res = await self._adapter.handle_startgame(chat_id, user_id)
            elif data == "cancel_game":
                res = await self._adapter.handle_cancelgame(chat_id, user_id)
            elif data in ("refresh_panel", "game_menu"):
                res = await self._adapter.handle_game_menu(chat_id)
            elif data == "main_menu":
                await self._api.send_message(chat_id, HELP_TEXT)
                res = None
            elif data in ("new_game", "new_game_button"):
                res = await self._adapter.handle_newgame(
                    chat_id, user_id, display_name
                )
            elif data == "close_ballot":
                res = await self._adapter.handle_close_ballot(chat_id, user_id)
            elif data == "end_round":
                res = await self._adapter.handle_end_round(chat_id, user_id)
            elif data == "start_voting":
                res = await self._adapter.handle_start_voting(chat_id)
            elif data == "spy_guess_menu":
                res = await self._adapter.handle_spy_guess_menu(chat_id, user_id)
            elif data.startswith("vote:"):
                target = self._parse_int(data.partition(":")[2])
                if target is not None:
                    res = await self._adapter.handle_spy_vote(chat_id, user_id, target)
            elif data.startswith("spy_guess:"):
                index = self._parse_int(data.partition(":")[2])
                if index is not None:
                    res = await self._adapter.handle_spy_guess_option(
                        chat_id, user_id, index
                    )
        except Exception:
            logger.exception("callback %r failed in chat %s", data, chat_id)

        # Telegram keeps the button spinner alive until the query is answered,
        # so this must happen on every path including failures.
        alert_text = res.alert_text if res is not None else None
        show_alert = bool(res.show_alert) if res is not None else False
        await self._api.answer_callback_query(
            query_id, alert_text, show_alert=show_alert
        )

    @staticmethod
    def _parse_int(raw: str) -> int | None:
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    async def _handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._handle_callback(update["callback_query"])
        elif "message" in update:
            await self._handle_message(update["message"])

    # -- main loop -----------------------------------------------------
    async def run(self) -> None:
        """Poll until stopped, dispatching each update concurrently."""
        while not self._stopping.is_set():
            response = await self._api.get_updates(
                self._offset, timeout=LONG_POLL_SECONDS
            )
            if not response.get("ok"):
                logger.error("getUpdates failed: %s", response.get("description"))
                await asyncio.sleep(3)
                continue

            for update in response.get("result", []):
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    # Advance the offset before handling so a handler crash can
                    # never turn one poisoned update into an infinite loop.
                    self._offset = update_id + 1
                self._spawn(self._guarded(update))

            self._store.cleanup_expired()

    async def _guarded(self, update: dict[str, Any]) -> None:
        try:
            await self._handle_update(update)
        except Exception:
            logger.exception("failed to handle update %s", update.get("update_id"))

    def request_stop(self) -> None:
        self._stopping.set()

    async def shutdown(self) -> None:
        """Let in-flight handlers finish."""
        if self._background:
            await asyncio.wait(set(self._background), timeout=10)


# ----------------------------------------------------------------------
# Bootstrap
# ----------------------------------------------------------------------
def _read_token() -> str:
    token = (os.environ.get("BOT_TOKEN") or "").strip()
    if not token:
        sys.stderr.write(
            "\nBOT_TOKEN is not set.\n\n"
            "On Render: Dashboard -> your service -> Environment -> Add\n"
            "  Key   = BOT_TOKEN\n"
            "  Value = the token from @BotFather\n\n"
            "Locally:  export BOT_TOKEN='123456789:AA...'\n\n"
            "The token is read from the environment on purpose: this repository\n"
            "is public, and a committed token can be used by anyone who reads it.\n\n"
        )
        raise SystemExit(2)
    return token


async def _amain() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )

    token = _read_token()

    # Overridable so the boot path can be exercised against a local stub in
    # tests; production never sets it.
    base_url = os.environ.get("TELEGRAM_API_BASE", DEFAULT_BASE_URL)
    api = TelegramAPI(token, base_url=base_url)
    me = await api.get_me()
    if not me.get("ok"):
        logger.error(
            "getMe rejected the token: %s. If it was ever committed or shared, "
            "revoke it with @BotFather (/revoke) and set the new one.",
            me.get("description"),
        )
        return 1

    username = (me.get("result") or {}).get("username", "unknown")
    logger.info("authenticated as @%s", username)

    # Long polling and a webhook are mutually exclusive; clear any stale one.
    await api.delete_webhook()

    runner = BotRunner(api, bot_username=username)

    port_value = os.environ.get("PORT")
    health_server = start_health_server(int(port_value)) if port_value else None

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, runner.request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda _sig, _frame: runner.request_stop())

    logger.info("polling started")
    try:
        await runner.run()
    finally:
        logger.info("shutting down")
        await runner.shutdown()
        if health_server is not None:
            health_server.shutdown()
    return 0


def main() -> int:
    try:
        return asyncio.run(_amain())
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
