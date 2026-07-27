"""Telegram I/O boundary layer separating decision locking from async HTTP effects."""

from __future__ import annotations

from typing import Any, Callable, Coroutine
from .models import Notification, OperationResult
from .session_manager import SessionManager
from .session_store import SessionStore


class TelegramAdapter:
    """I/O boundary adapter connecting Telegram events to the game rules engine.

    Decisions run synchronously under the per-group lock; every Telegram send
    happens afterwards, outside the lock, so no network await ever occurs while
    a group's state lock is held.
    """

    def __init__(
        self,
        store: SessionStore,
        session_manager: SessionManager | None = None,
        send_message_fn: Callable[..., Coroutine[Any, Any, object]] | None = None,
        edit_message_fn: Callable[..., Coroutine[Any, Any, object]] | None = None,
        edit_markup_fn: Callable[..., Coroutine[Any, Any, object]] | None = None,
        bot_username: str = "guessJobot",
    ) -> None:
        self._store = store
        self.session_manager = session_manager or SessionManager(store)
        self._send_message_fn = send_message_fn
        self._edit_message_fn = edit_message_fn
        self._edit_markup_fn = edit_markup_fn
        self.bot_username = bot_username

    @staticmethod
    def _is_ok(res: object) -> bool:
        """A send is confirmed unless the transport explicitly reports ok=False."""
        return not (isinstance(res, dict) and res.get("ok") is False)

    @staticmethod
    def _message_id_of(res: object) -> int | None:
        """Extract ``result.message_id`` from a Bot API envelope, if present."""
        if not isinstance(res, dict):
            return None
        result = res.get("result")
        if not isinstance(result, dict):
            return None
        message_id = result.get("message_id")
        return message_id if isinstance(message_id, int) else None

    def _remember_control_message(self, notif: Notification, res: object) -> None:
        """Track the newest keyboard-bearing group message as the control panel.

        Nothing previously assigned ``control_message_id``, so every
        ``edit_message_id`` was ``None`` and the panel could neither be edited
        in place nor have its stale buttons stripped.  Recording it here -- the
        one place that actually sees Telegram's response -- keeps the manager
        free of I/O concerns.
        """
        if notif.channel != "group" or not notif.buttons:
            return
        message_id = self._message_id_of(res)
        if message_id is None:
            return
        session = self._store.get(notif.target_id)
        if session is not None:
            session.control_message_id = message_id

    async def dispatch_notifications(
        self, notifications: list[Notification]
    ) -> list[dict[str, Any]]:
        """Dispatch notifications to Telegram, returning per-notification delivery status."""
        results: list[dict[str, Any]] = []

        for notif in notifications:
            reply_markup = {"inline_keyboard": notif.buttons} if notif.buttons else None

            if notif.disable_previous_message_id is not None and self._edit_markup_fn is not None:
                try:
                    await self._edit_markup_fn(
                        notif.target_id, notif.disable_previous_message_id, None
                    )
                except Exception:
                    pass

            if (
                notif.channel == "group"
                and notif.edit_message_id is not None
                and self._edit_message_fn is not None
            ):
                try:
                    res = await self._edit_message_fn(
                        notif.target_id, notif.edit_message_id, notif.text, reply_markup
                    )
                    if self._is_ok(res):
                        results.append(
                            {"target_id": notif.target_id, "channel": notif.channel, "ok": True, "res": res}
                        )
                        continue
                    # An edit can legitimately fail (message too old, deleted,
                    # or identical content).  Fall through to a fresh send so
                    # the update still reaches the group.
                except Exception:
                    pass

            try:
                if self._send_message_fn is not None:
                    res = await self._send_message_fn(notif.target_id, notif.text, reply_markup)
                else:
                    continue
                ok = self._is_ok(res)
                if ok:
                    self._remember_control_message(notif, res)
                results.append(
                    {"target_id": notif.target_id, "channel": notif.channel, "ok": ok, "res": res}
                )
            except Exception as err:
                results.append(
                    {"target_id": notif.target_id, "channel": notif.channel, "ok": False, "error": str(err)}
                )

        return results

    async def handle_newgame(
        self, group_chat_id: int, user_id: int, display_name: str
    ) -> OperationResult:
        """Handle /newgame or launcher start."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.create_session(
                group_chat_id, user_id, display_name, bot_username=self.bot_username
            )
        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_join(
        self, group_chat_id: int, user_id: int, display_name: str
    ) -> OperationResult:
        """Handle player join."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.join_session(
                group_chat_id, user_id, display_name, bot_username=self.bot_username
            )
        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_leave(self, group_chat_id: int, user_id: int) -> OperationResult:
        """Handle player leave."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.leave_session(
                group_chat_id, user_id, bot_username=self.bot_username
            )
        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_startgame(self, group_chat_id: int, user_id: int) -> OperationResult:
        """Handle round start with staged role delivery.

        Role DMs are delivered first (outside the lock).  Only when every active
        recipient is confirmed is the group readiness announcement sent.  A
        partial delivery failure rolls the round back to the lobby and never
        announces success, keeping the round unplayable.
        """
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.start_session(
                group_chat_id, user_id, bot_username=self.bot_username
            )

        if not res.ok:
            if res.notifications:
                await self.dispatch_notifications(res.notifications)
            return res

        # Staged delivery: send the private role DMs first (outside the lock).
        # Only when every active recipient is confirmed do we announce readiness
        # to the group.  A partial role-delivery failure rolls the round back to
        # the lobby and never announces success, so the round stays unplayable.
        group_notifs = [n for n in res.notifications if n.channel == "group"]
        dm_notifs = [n for n in res.notifications if n.channel == "dm"]

        dm_results = await self.dispatch_notifications(dm_notifs)
        failed = [r["target_id"] for r in dm_results if not r["ok"]]

        if failed:
            async with self._store.lock_for(group_chat_id):
                rollback = self.session_manager.rollback_failed_start(group_chat_id, failed)
            if rollback.notifications:
                await self.dispatch_notifications(rollback.notifications)
            return rollback

        if group_notifs:
            await self.dispatch_notifications(group_notifs)

        return res

    async def handle_cancelgame(self, group_chat_id: int, user_id: int) -> OperationResult:
        """Handle session cancellation."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.cancel_session(group_chat_id, user_id)
        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_start_voting(self, group_chat_id: int) -> OperationResult:
        """Handle opening the voting panel."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.start_voting_panel(group_chat_id)
        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_spy_vote(
        self, group_chat_id: int, voter_id: int, target_id: int
    ) -> OperationResult:
        """Handle vote submission."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.record_spy_vote(group_chat_id, voter_id, target_id)
        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_spy_guess(
        self, group_chat_id: int, actor_id: int, word: str
    ) -> OperationResult:
        """Handle spy location guess submission."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.submit_spy_location_guess(group_chat_id, actor_id, word)
        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_spy_guess_menu(self, group_chat_id: int, actor_id: int) -> OperationResult:
        """Show the exposed spy their one multiple-choice location ballot."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.build_spy_guess_menu(group_chat_id, actor_id)
        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_spy_guess_option(
        self, group_chat_id: int, actor_id: int, option_index: int
    ) -> OperationResult:
        """Resolve a ballot button index to its word and submit it as the guess.

        Index resolution and submission happen under the same lock, so a second
        press cannot resolve against options that the first press already
        consumed.
        """
        async with self._store.lock_for(group_chat_id):
            word = self.session_manager.resolve_spy_guess_option(group_chat_id, option_index)
            if word is None:
                return OperationResult(
                    ok=False,
                    reason="invalid_option",
                    alert_text="⚠️ هذا الخيار لم يعد متاحاً.",
                    show_alert=True,
                )
            res = self.session_manager.submit_spy_location_guess(group_chat_id, actor_id, word)
        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    def _build_status_panel_notification(self, group_chat_id: int, header: str) -> Notification:
        """Build the current control panel notification (delegates to the manager)."""
        return self.session_manager.build_status_panel_notification(group_chat_id, header)

    async def handle_refresh_panel(self, group_chat_id: int) -> OperationResult:
        """Resend the control panel at the bottom of the chat.

        Always succeeds and always sends a keyboard, including when no session
        exists. Returning a bare rejection here would defeat the point of the
        persistent menu: the button that exists to recover a lost panel must
        never itself be a dead end.
        """
        async with self._store.lock_for(group_chat_id):
            session = self._store.get(group_chat_id)
            notif = self._build_status_panel_notification(
                group_chat_id, "📌 <b>لوحة التحكم الحالية للعبة:</b>"
            )
            notif.disable_previous_message_id = (
                session.control_message_id if session is not None else None
            )
            notif.edit_message_id = None
        await self.dispatch_notifications([notif])
        return OperationResult(ok=True, show_alert=False)

    async def handle_close_ballot(self, group_chat_id: int, actor_id: int) -> OperationResult:
        """Tally an open ballot early at the host's request."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.close_ballot(group_chat_id, actor_id)
        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_end_round(self, group_chat_id: int, actor_id: int) -> OperationResult:
        """End the round with no verdict and disclose the spy (host only)."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.enter_reveal(group_chat_id, actor_id)
        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_game_menu(self, group_chat_id: int) -> OperationResult:
        """Re-send the panel for the current state; always available."""
        return await self.handle_refresh_panel(group_chat_id)

    def mark_user_dm_ready(self, user_id: int) -> None:
        """Mark user as DM ready."""
        self.session_manager.mark_dm_ready(user_id)
