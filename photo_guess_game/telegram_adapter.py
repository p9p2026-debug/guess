"""Telegram I/O boundary layer separating decision locking from async HTTP effects."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine
from .models import Notification, OperationResult
from .session_manager import SessionManager
from .session_store import SessionStore


class TelegramAdapter:
    """I/O boundary adapter connecting Telegram events to game rules engine."""

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

    async def dispatch_notifications(
        self, notifications: list[Notification]
    ) -> list[dict[str, Any]]:
        """Dispatch notifications to Telegram, returning delivery status results."""
        results: list[dict[str, Any]] = []

        for notif in notifications:
            reply_markup = {"inline_keyboard": notif.buttons} if notif.buttons else None

            # Handle disabling old message markup
            if notif.disable_previous_message_id is not None and self._edit_markup_fn is not None:
                try:
                    await self._edit_markup_fn(
                        notif.target_id, notif.disable_previous_message_id, None
                    )
                except Exception:
                    pass

            # Handle Edit vs Send
            if (
                notif.channel == "group"
                and notif.edit_message_id is not None
                and self._edit_message_fn is not None
            ):
                try:
                    res = await self._edit_message_fn(
                        notif.target_id, notif.edit_message_id, notif.text, reply_markup
                    )
                    results.append({"target_id": notif.target_id, "ok": True, "res": res})
                    continue
                except Exception:
                    # Fallback to sending a new message if editing fails
                    pass

            if self._send_message_fn is not None:
                try:
                    res = await self._send_message_fn(
                        notif.target_id, notif.text, reply_markup
                    )
                    results.append({"target_id": notif.target_id, "ok": True, "res": res})
                except Exception as err:
                    results.append({"target_id": notif.target_id, "ok": False, "error": str(err)})

        return results

    async def handle_newgame(
        self, group_chat_id: int, user_id: int, display_name: str
    ) -> OperationResult:
        """Handle /newgame or Launcher start."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.create_session(
                group_chat_id=group_chat_id,
                host_id=user_id,
                host_name=display_name,
                bot_username=self.bot_username,
            )

        if res.notifications:
            dispatch_res = await self.dispatch_notifications(res.notifications)
            # Store control_message_id if available
            if dispatch_res and dispatch_res[0].get("ok"):
                raw_res = dispatch_res[0].get("res")
                if isinstance(raw_res, dict) and "message_id" in raw_res:
                    async with self._store.lock_for(group_chat_id):
                        session = self._store.get(group_chat_id)
                        if session:
                            session.control_message_id = raw_res["message_id"]
                            self._store.put(session)
        return res

    async def handle_join(
        self, group_chat_id: int, user_id: int, display_name: str
    ) -> OperationResult:
        """Handle player join."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.join_session(
                group_chat_id=group_chat_id,
                user_id=user_id,
                display_name=display_name,
                bot_username=self.bot_username,
            )

        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_leave(self, group_chat_id: int, user_id: int) -> OperationResult:
        """Handle player leave."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.leave_session(
                group_chat_id=group_chat_id, user_id=user_id, bot_username=self.bot_username
            )

        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_startgame(self, group_chat_id: int, user_id: int) -> OperationResult:
        """Handle game start with two-phase role distribution."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.start_session(
                group_chat_id=group_chat_id, requester_id=user_id, bot_username=self.bot_username
            )

        if not res.ok or not res.notifications:
            if res.notifications:
                await self.dispatch_notifications(res.notifications)
            return res

        # Execute DM role distribution OUTSIDE the lock
        dm_results = await self.dispatch_notifications(res.notifications)
        failed_uids = [
            r["target_id"] for r in dm_results if not r.get("ok")
        ]

        async with self._store.lock_for(group_chat_id):
            if failed_uids:
                final_res = self.session_manager.rollback_failed_dealing(
                    group_chat_id=group_chat_id,
                    failed_user_ids=failed_uids,
                    bot_username=self.bot_username,
                )
            else:
                final_res = self.session_manager.complete_role_dealing(group_chat_id)

        if final_res.notifications:
            await self.dispatch_notifications(final_res.notifications)
        return final_res

    async def handle_cancelgame(
        self, group_chat_id: int, user_id: int, game_id: str
    ) -> OperationResult:
        """Handle session cancellation."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.cancel_session(
                group_chat_id=group_chat_id, user_id=user_id, game_id=game_id
            )

        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_start_voting(
        self, group_chat_id: int, requester_id: int
    ) -> OperationResult:
        """Handle opening voting panel."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.start_voting_panel(
                group_chat_id=group_chat_id, requester_id=requester_id
            )

        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_spy_vote(
        self, group_chat_id: int, voter_id: int, target_id: int, game_id: str, vote_round: int
    ) -> OperationResult:
        """Handle vote submission."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.record_spy_vote(
                group_chat_id=group_chat_id,
                voter_id=voter_id,
                target_id=target_id,
                game_id=game_id,
                vote_round=vote_round,
            )

        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_spy_guess_menu(
        self, group_chat_id: int, user_id: int, game_id: str
    ) -> OperationResult:
        """Handle Spy Guess Menu trigger."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.handle_spy_guess_menu(
                group_chat_id=group_chat_id, user_id=user_id, game_id=game_id
            )

        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_spy_guess(
        self, group_chat_id: int, spy_id: int, option_index: int, game_id: str
    ) -> OperationResult:
        """Handle Spy location option selection."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.submit_spy_location_guess(
                group_chat_id=group_chat_id,
                spy_id=spy_id,
                option_index=option_index,
                game_id=game_id,
            )

        if res.notifications:
            await self.dispatch_notifications(res.notifications)
        return res

    async def handle_refresh_panel(self, group_chat_id: int) -> OperationResult:
        """Resend current control panel down at bottom of chat."""
        async with self._store.lock_for(group_chat_id):
            session = self._store.get(group_chat_id)
            if session is None:
                return OperationResult(
                    ok=False,
                    reason="no_session",
                    alert_text="⚠️ لا توجد لعبة نشطة في هذه المجموعة.",
                    show_alert=True,
                )

            old_msg_id = session.control_message_id
            notif = self.session_manager._build_status_panel_notification(
                group_chat_id, "📌 <b>لوحة التحكم الحالية للعبة:</b>"
            )
            notif.disable_previous_message_id = old_msg_id
            notif.edit_message_id = None  # Force fresh send

        dispatch_res = await self.dispatch_notifications([notif])
        if dispatch_res and dispatch_res[0].get("ok"):
            raw_res = dispatch_res[0].get("res")
            if isinstance(raw_res, dict) and "message_id" in raw_res:
                async with self._store.lock_for(group_chat_id):
                    session = self._store.get(group_chat_id)
                    if session:
                        session.control_message_id = raw_res["message_id"]
                        self._store.put(session)

        return OperationResult(ok=True, alert_text="📌 تم إظهار اللوحة بالأسفل!", show_alert=False)

    def mark_user_dm_ready(self, user_id: int) -> None:
        """Mark user as DM ready."""
        self.session_manager.mark_dm_ready(user_id)
