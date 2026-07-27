"""Telegram Adapter: I/O boundary layer wiring Telegram updates to game components.

Translates Telegram group commands, DM photo uploads, and DM disambiguation replies
into calls on SessionManager, PhotoDistributor, GuessTracker, ScoreTracker, and
TimerService, rendering returned Notifications as outbound Telegram messages.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine

from .guess_tracker import GuessTracker
from .models import GameState, Notification, OperationResult
from .photo_distributor import PhotoDistributor
from .score_tracker import ScoreTracker
from .session_manager import SessionManager
from .session_store import SessionStore
from .timer_service import TimerService


class TelegramAdapter:
    """I/O boundary adapter connecting Telegram events to pure game components."""

    def __init__(
        self,
        store: SessionStore,
        session_manager: SessionManager | None = None,
        photo_distributor: PhotoDistributor | None = None,
        guess_tracker: GuessTracker | None = None,
        score_tracker: ScoreTracker | None = None,
        timer_service: TimerService | None = None,
        send_message_fn: Callable[[int, str], Coroutine[Any, Any, None]] | None = None,
        send_photo_fn: Callable[[int, str, str], Coroutine[Any, Any, None]] | None = None,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._store = store
        self._loop = loop

        def _async_scheduler(delay: float, callback: Callable[[], None]):
            current_loop = self._loop or asyncio.get_event_loop()
            return current_loop.call_later(delay, callback)

        self.timer_service = timer_service or TimerService(scheduler=_async_scheduler)
        self.score_tracker = score_tracker or ScoreTracker()
        self.photo_distributor = photo_distributor or PhotoDistributor(store)
        self.guess_tracker = guess_tracker or GuessTracker()
        self.session_manager = session_manager or SessionManager(
            store,
            score_tracker=self.score_tracker,
            photo_distributor=self.photo_distributor,
            guess_tracker=self.guess_tracker,
            timer_service=self.timer_service,
            on_notification_cb=self._on_timer_notifications,
        )

        self._send_message_fn = send_message_fn
        self._send_photo_fn = send_photo_fn
        self.sent_notifications: list[Notification] = []

    def _on_timer_notifications(self, notifications: list[Notification]) -> None:
        for notification in notifications:
            self.sent_notifications.append(notification)
            if notification.photo_file_id is not None:
                if self._send_photo_fn is not None:
                    try:
                        loop = self._loop or asyncio.get_running_loop()
                        loop.create_task(
                            self._send_photo_fn(
                                notification.target_id,
                                notification.photo_file_id,
                                notification.text,
                            )
                        )
                    except RuntimeError:
                        pass
            else:
                if self._send_message_fn is not None:
                    try:
                        loop = self._loop or asyncio.get_running_loop()
                        loop.create_task(
                            self._send_message_fn(notification.target_id, notification.text)
                        )
                    except RuntimeError:
                        pass


    async def dispatch_notifications(self, notifications: list[Notification]) -> None:
        """Render and send every returned Notification via Telegram calls."""
        for notification in notifications:
            self.sent_notifications.append(notification)
            reply_markup = None
            if notification.buttons:
                reply_markup = {"inline_keyboard": notification.buttons}

            if notification.photo_file_id is not None:
                if self._send_photo_fn is not None:
                    await self._send_photo_fn(
                        notification.target_id,
                        notification.photo_file_id,
                        notification.text,
                        reply_markup,
                    )
            else:
                if self._send_message_fn is not None:
                    await self._send_message_fn(
                        notification.target_id,
                        notification.text,
                        reply_markup,
                    )


    def _build_status_panel_notification(
        self, group_chat_id: int, prefix_text: str
    ) -> Notification:
        session = self._store.get(group_chat_id)
        if session is None or session.state not in (GameState.LOBBY, GameState.GUESSING):
            return Notification(
                channel="group",
                target_id=group_chat_id,
                text=f"{prefix_text}\n\n💡 أرسل <code>/newgame</code> لإنشاء لعبة جديدة!",
            )

        if session.state == GameState.LOBBY:
            players_list = ", ".join([f"<b>{p.display_name}</b>" for p in session.players.values()])
            text = (
                f"{prefix_text}\n\n"
                f"📋 <b>لوحة تحكم اللوبي الحالية:</b>\n"
                f"👥 <b>اللاعبون المنضمون ({len(session.players)}/{session.max_players}):</b> {players_list}\n"
                f"⏱️ <b>الوقت:</b> {session.guessing_timeout_seconds // 60} دقيقة"
            )
            buttons = [
                [
                    {"text": "➕ انضمام للعبة", "callback_data": "join_game"},
                    {"text": "🚪 مغادرة اللعبة", "callback_data": "leave_game"},
                ],
                [
                    {"text": "🚀 بدء اللعبة", "callback_data": "start_game"},
                    {"text": "❌ إلغاء اللعبة", "callback_data": "cancel_game"},
                ],
            ]
            return Notification(channel="group", target_id=group_chat_id, text=text, buttons=buttons)

        # GameState.GUESSING
        if session.voting_active:
            vote_buttons = [
                [{"text": f"👤 {p.display_name}", "callback_data": f"vote:{pid}"}]
                for pid, p in session.players.items()
            ]
            voted_count = len(session.votes)
            total_players = len(session.players)
            text = (
                f"{prefix_text}\n\n"
                f"🗳️ <b>لوحة التصويت الحالية:</b>\n"
                f"اختر الشخص المشتبه به أنه الجاسوس!\n"
                f"📊 <b>الأصوات المسجلة:</b> ({voted_count}/{total_players})"
            )
            return Notification(channel="group", target_id=group_chat_id, text=text, buttons=vote_buttons)
        else:
            spy_buttons = [
                [{"text": "🗳️ بدء التصويت على الجاسوس", "callback_data": "start_voting"}],
                [{"text": "💡 تخمين الكلمة السرية (الجاسوس)", "callback_data": "spy_guess_menu"}],
            ]
            text = (
                f"{prefix_text}\n\n"
                f"🕵️ <b>لوحة التحكم للجولة النشطة:</b>\n"
                f"الأسئلة مستمرة في المجموعة! عند الجاهزية اضغط زر التصويت أدناه."
            )
            return Notification(channel="group", target_id=group_chat_id, text=text, buttons=spy_buttons)

    async def handle_status(self, group_chat_id: int) -> OperationResult:
        """Handle /status or /settings command to resend current control panel at bottom."""
        async with self._store.lock_for(group_chat_id):
            notif = self._build_status_panel_notification(
                group_chat_id, "⚙️ <b>لوحة التحكم الحالية للعبة:</b>"
            )
            await self.dispatch_notifications([notif])
            return OperationResult(ok=True, notifications=[notif])

    async def handle_newgame(
        self, group_chat_id: int, user_id: int, display_name: str
    ) -> OperationResult:
        """Handle /newgame command."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.create_session(
                group_chat_id=group_chat_id, host_id=user_id, host_name=display_name
            )
            if not res.ok and not res.notifications:
                res.notifications = [
                    self._build_status_panel_notification(
                        group_chat_id, "⚠️ <b>تنبيه:</b> هناك لعبة نشطة حالياً في هذه المجموعة!"
                    )
                ]
            await self.dispatch_notifications(res.notifications)
            return res

    async def handle_join(
        self, group_chat_id: int, user_id: int, display_name: str
    ) -> OperationResult:
        """Handle /join command."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.join_session(
                group_chat_id=group_chat_id, user_id=user_id, display_name=display_name
            )
            if not res.ok and not res.notifications:
                err_text = {
                    "not_joinable": "⚠️ لا توجد لعبة مفتوحة في اللوبي للانضمام إليها حالياً.",
                    "already_member": "⚠️ أنت منضم لهذه اللعبة بالفعل!",
                    "lobby_full": "⚠️ اللوبي ممتلئ بحده الأقصى من اللاعبين.",
                }.get(res.reason or "", "⚠️ تعذر الانضمام للعبة.")
                res.notifications = [
                    self._build_status_panel_notification(group_chat_id, err_text)
                ]
            await self.dispatch_notifications(res.notifications)
            return res

    async def handle_leave(self, group_chat_id: int, user_id: int) -> OperationResult:
        """Handle /leave command."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.leave_session(
                group_chat_id=group_chat_id, user_id=user_id
            )
            if not res.ok and not res.notifications:
                err_text = {
                    "not_member": "⚠️ أنت لست مشاركاً في هذه اللعبة.",
                    "already_inactive": "⚠️ لقد غادرت اللعبة مسبقاً.",
                }.get(res.reason or "", "⚠️ لا يمكن المغادرة في الوقت الحالي.")
                res.notifications = [
                    self._build_status_panel_notification(group_chat_id, err_text)
                ]
            await self.dispatch_notifications(res.notifications)
            return res

    async def handle_startgame(
        self, group_chat_id: int, user_id: int
    ) -> OperationResult:
        """Handle /startgame command."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.start_session(
                group_chat_id=group_chat_id, requester_id=user_id
            )
            if not res.ok and not res.notifications:
                err_text = {
                    "not_in_lobby": "⚠️ اللعبة ليست في مرحلة اللوبي لبدئها.",
                    "not_host": "⚠️ عذراً، فقط منشئ اللعبة (Host) يمكنه بدء اللعبة.",
                    "below_minimum": "⚠️ عدد اللاعبين أقل من الحد الأدنى المطلوب (لاعبَين اثنين).",
                    "missing_photos": "⚠️ لم يرسل جميع اللاعبين صورهم بالخاص بعد.",
                }.get(res.reason or "", "⚠️ تعذر بدء اللعبة.")
                res.notifications = [
                    self._build_status_panel_notification(group_chat_id, err_text)
                ]
            await self.dispatch_notifications(res.notifications)
            return res

    async def handle_cancelgame(
        self, group_chat_id: int, user_id: int
    ) -> OperationResult:
        """Handle /cancelgame command."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.cancel_session(
                group_chat_id=group_chat_id, user_id=user_id
            )
            if not res.ok and not res.notifications:
                err_text = {
                    "no_session": "⚠️ لا توجد لعبة نشطة لإلغائها.",
                    "already_terminal": "⚠️ اللعبة منتهية بالفعل.",
                    "not_host": "⚠️ عذراً، فقط منشئ اللعبة (Host) يمكنه إلغاء اللعبة.",
                }.get(res.reason or "", "⚠️ تعذر إلغاء اللعبة.")
                res.notifications = [
                    self._build_status_panel_notification(group_chat_id, err_text)
                ]
            await self.dispatch_notifications(res.notifications)
            return res


    async def handle_settimeout(
        self, group_chat_id: int, user_id: int, minutes_str: str
    ) -> OperationResult:
        """Handle /settimeout <minutes> command."""
        try:
            minutes = float(minutes_str)
            seconds = int(minutes * 60)
            if seconds < 0:
                raise ValueError
        except ValueError:
            res = OperationResult(
                ok=False,
                reason="invalid_timeout_argument",
                notifications=[
                    Notification(
                        channel="group",
                        target_id=group_chat_id,
                        text="طريقة الاستخدام: /settimeout <عدد_الدقائق> (يجب أن يكون رقماً موجباً).",
                    )
                ],
            )
            await self.dispatch_notifications(res.notifications)
            return res

        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.set_guessing_timeout(
                group_chat_id=group_chat_id, requester_id=user_id, seconds=seconds
            )
            if not res.ok and not res.notifications:
                err_text = {
                    "not_in_lobby": "⚠️ يمكنك تغيير الوقت أثناء تواجد اللعبة في اللوبي فقط.",
                    "not_host": "⚠️ عذراً، فقط منشئ اللعبة (Host) يمكنه تغيير الوقت.",
                    "invalid_timeout": "⚠️ الوقت المدخل غير صحيح.",
                }.get(res.reason or "", "⚠️ تعذر ضبط الوقت.")
                res.notifications = [
                    Notification(channel="group", target_id=group_chat_id, text=err_text)
                ]
            await self.dispatch_notifications(res.notifications)
            return res

    async def handle_start_voting(self, group_chat_id: int) -> OperationResult:
        """Handle start_voting button click."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.start_voting_panel(group_chat_id=group_chat_id)
            await self.dispatch_notifications(res.notifications)
            return res

    async def handle_spy_vote(
        self, group_chat_id: int, voter_id: int, target_id: int
    ) -> OperationResult:
        """Handle player vote button click."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.record_spy_vote(
                group_chat_id=group_chat_id, voter_id=voter_id, target_id=target_id
            )
            await self.dispatch_notifications(res.notifications)
            return res

    async def handle_spy_guess_menu(
        self, group_chat_id: int, user_id: int
    ) -> OperationResult:
        """Handle spy_guess_menu button click."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.handle_spy_guess_menu(
                group_chat_id=group_chat_id, user_id=user_id
            )
            await self.dispatch_notifications(res.notifications)
            return res

    async def handle_spy_guess(
        self, group_chat_id: int, spy_id: int, word_guess: str
    ) -> OperationResult:
        """Handle spy_guess location button click."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.submit_spy_location_guess(
                group_chat_id=group_chat_id, spy_id=spy_id, word_guess=word_guess
            )
            await self.dispatch_notifications(res.notifications)
            return res

    async def handle_answer(
        self, group_chat_id: int, responder_id: int, answer_type: str
    ) -> OperationResult:

        """Handle Yes/No answer button click from opponent."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.record_answer(
                group_chat_id=group_chat_id,
                responder_id=responder_id,
                answer_type=answer_type,
            )
            await self.dispatch_notifications(res.notifications)
            return res

    async def handle_guess_intent(
        self, group_chat_id: int, user_id: int
    ) -> OperationResult:
        """Handle guess intent button click from active player."""
        async with self._store.lock_for(group_chat_id):
            res = self.session_manager.record_guess_intent(
                group_chat_id=group_chat_id, user_id=user_id
            )
            await self.dispatch_notifications(res.notifications)
            return res

    async def handle_guess(
        self, group_chat_id: int, guesser_id: int, text_args: str
    ) -> OperationResult:
        """Handle /guess command or direct word guess."""
        parts = text_args.strip().split(maxsplit=1)
        if len(parts) == 1 and text_args.strip():
            # Direct word guess for own photo card!
            async with self._store.lock_for(group_chat_id):
                res = self.session_manager.submit_direct_guess(
                    group_chat_id=group_chat_id,
                    guesser_id=guesser_id,
                    guess_text=text_args.strip(),
                )
                await self.dispatch_notifications(res.notifications)
                return res

        if len(parts) < 2:
            res = OperationResult(
                ok=False,
                reason="malformed_guess",
                notifications=[
                    Notification(
                        channel="dm",
                        target_id=guesser_id,
                        text="طريقة الاستخدام: /guess <كلمة_التخمين> أو /guess <رمز_الصورة> <اسم_اللاعب>",
                    )
                ],
            )
            await self.dispatch_notifications(res.notifications)
            return res


        label, target_str = parts[0], parts[1].strip()

        async with self._store.lock_for(group_chat_id):
            session = self._store.get(group_chat_id)
            if session is None or session.state != GameState.GUESSING:
                res = OperationResult(
                    ok=False,
                    reason="guessing_closed",
                    notifications=[
                        Notification(
                            channel="dm",
                            target_id=guesser_id,
                            text="مرحلة التخمين مغلقة حالياً لهذه اللعبة.",
                        )
                    ],
                    session=session,
                )
                await self.dispatch_notifications(res.notifications)
                return res

            target_id = None
            clean_target = target_str.lstrip("@").lower()
            for pid, p in session.players.items():
                if str(pid) == target_str or p.display_name.lower() == clean_target:
                    target_id = pid
                    break

            if target_id is None:
                try:
                    tid = int(target_str)
                    if tid in session.players:
                        target_id = tid
                except ValueError:
                    pass

            if target_id is None:
                res = OperationResult(
                    ok=False,
                    reason="invalid_target",
                    notifications=[
                        Notification(
                            channel="dm",
                            target_id=guesser_id,
                            text=f"اللاعب '{target_str}' غير موجود في اللعبة.",
                        )
                    ],
                    session=session,
                )
                await self.dispatch_notifications(res.notifications)
                return res

            formatted_label = label if label.startswith("Photo ") else f"Photo {label}"
            if formatted_label not in session.labels and label in session.labels:
                formatted_label = label

            res = self.guess_tracker.record_guess(
                session, guesser_id=guesser_id, label=formatted_label, target_id=target_id
            )
            if res.ok:
                self._store.put(session)
                res.notifications = [
                    Notification(
                        channel="dm",
                        target_id=guesser_id,
                        text=f"تم تسجيل تخمينك لـ {formatted_label} بنجاح! 🎯",
                    )
                ]
            else:
                reason_msg = {
                    "invalid_label": f"رمز الصورة '{label}' غير موجود.",
                    "invalid_target": "اللاعب المحدد غير صحيح.",
                    "self_guess": "لا يمكنك تخمين صورتك الخاصة!",
                    "guessing_closed": "مرحلة التخمين مغلقة.",
                }.get(res.reason or "", "تخمين غير صحيح.")
                res.notifications = [
                    Notification(channel="dm", target_id=guesser_id, text=reason_msg)
                ]

            await self.dispatch_notifications(res.notifications)
            return res

    async def handle_dm_photo(self, user_id: int, file_id: str) -> OperationResult:
        """Handle incoming DM photo from a user."""
        res = self.photo_distributor.submit_photo(user_id=user_id, file_id=file_id)
        if not res.ok and not res.notifications:
            err_text = {
                "no_open_session": "⚠️ لم يتم العثور على لعبة مفتوحة باسمك. انضم إلى لعبة في إحدى المجموعات أولاً!",
            }.get(res.reason or "", "⚠️ تعذر حفظ الصورة.")
            res.notifications = [
                Notification(channel="dm", target_id=user_id, text=err_text)
            ]
        await self.dispatch_notifications(res.notifications)
        return res

    async def handle_dm_text_reply(self, user_id: int, text: str) -> OperationResult:
        """Handle incoming DM text reply (e.g. for disambiguation choice)."""
        try:
            chosen_gid = int(text.strip())
        except ValueError:
            res = OperationResult(
                ok=False,
                reason="invalid_choice",
                notifications=[
                    Notification(
                        channel="dm",
                        target_id=user_id,
                        text="الرجاء اختيار مجموعة صحيحة من الأزرار المتاحة.",
                    )
                ],
            )
            await self.dispatch_notifications(res.notifications)
            return res

        async with self._store.lock_for(chosen_gid):
            res = self.photo_distributor.resolve_disambiguated_submission(
                user_id=user_id, chosen_group_chat_id=chosen_gid
            )
            if not res.ok and not res.notifications:
                err_text = {
                    "no_pending_submission": "⚠️ لا توجد صورة معلقة بانتظار تباين المجموعة.",
                    "invalid_choice": "⚠️ اختيار غير صحيح أو أن اللعبة قد انتهت.",
                }.get(res.reason or "", "⚠️ اختيار غير صحيح.")
                res.notifications = [
                    Notification(channel="dm", target_id=user_id, text=err_text)
                ]
            await self.dispatch_notifications(res.notifications)
            return res


