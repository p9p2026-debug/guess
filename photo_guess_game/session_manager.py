"""Pure game logic engine for Telegram Spy Game."""

from __future__ import annotations

import html
import random
import time
import uuid
from .locations import LocationEntry, get_location_options, get_random_location
from .models import GameSession, GameState, Notification, OperationResult, Player
from .session_store import SessionStore, _NON_TERMINAL_STATES


class SessionManager:
    """Core state machine and rules engine for Spy Game."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    @staticmethod
    def generate_game_id() -> str:
        """Generate a short 4-character hex game_id."""
        return uuid.uuid4().hex[:4]

    def create_session(
        self, group_chat_id: int, host_id: int, host_name: str, bot_username: str = "guessJobot"
    ) -> OperationResult:
        """Initialize a new GameSession in LOBBY state."""
        existing = self._store.get(group_chat_id)
        current_time = time.time()
        if (
            existing is not None
            and existing.state in _NON_TERMINAL_STATES
            and (current_time - existing.last_activity_at < 3600)
        ):
            return OperationResult(
                ok=False,
                reason="session_already_active",
                alert_text="⚠️ هناك لعبة نشطة حالياً في هذه المجموعة!",
                show_alert=True,
                session=existing,
            )

        clean_host_name = html.escape(host_name)
        host = Player(user_id=host_id, display_name=clean_host_name, joined_at=current_time)
        game_id = self.generate_game_id()

        session = GameSession(
            game_id=game_id,
            group_chat_id=group_chat_id,
            host_id=host_id,
            state=GameState.LOBBY,
            players={host_id: host},
            created_at=current_time,
            last_activity_at=current_time,
            min_players=3,
            max_players=15,
        )
        self._store.put(session)

        buttons = self._build_lobby_buttons(session, bot_username)
        announcement = Notification(
            channel="group",
            target_id=group_chat_id,
            text=(
                f"🕵️ <b>{clean_host_name}</b> بدأ لعبة <b>الجاسوس والكلمة السرية</b>!\n\n"
                "<blockquote expandable>\n"
                "<b>💡 فكرة اللعبة:</b>\n"
                "البوت يرسل كلمة سرية واحدة بالخاص لجميع المواطنين، ولكنه يختار شخصاً واحداً ليكون <b>الجاسوس 🕵️</b> (لا يعرف الكلمة!).\n"
                "اطرحوا أسئلة على بعضكم في المحادثة لاكتشاف الجاسوس دون إفشاء الكلمة السرية!\n"
                "</blockquote>\n\n"
                "<b>📋 طريقة اللعب:</b>\n"
                "1️⃣ اضغط <b>💬 تفعيل الخاص مع البوت</b> بالأسفل أولاً.\n"
                "2️⃣ اضغط <b>➕ انضمام للعبة</b>.\n"
                "3️⃣ اضغط <b>🚀 بدء اللعبة</b> لتلقي الدور بالخاص!\n\n"
                f"👥 <b>اللوبي مفتوح:</b> (1/{session.max_players} لاعبين - الحد الأدنى: {session.min_players})"
            ),
            buttons=buttons,
        )
        return OperationResult(ok=True, notifications=[announcement], session=session)

    def mark_dm_ready(self, user_id: int) -> None:
        """Mark a user as having DM enabled across active sessions."""
        for session in list(self._store._sessions.values()):
            if user_id in session.players:
                session.players[user_id].dm_ready = True
                self._store.put(session)

    def join_session(
        self, group_chat_id: int, user_id: int, display_name: str, bot_username: str = "guessJobot"
    ) -> OperationResult:
        """Add a player to LOBBY state session."""
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.LOBBY:
            return OperationResult(
                ok=False,
                reason="not_joinable",
                alert_text="⚠️ لا توجد لعبة مفتوحة في اللوبي حالياً.",
                show_alert=True,
                session=session,
            )

        if user_id in session.players:
            return OperationResult(
                ok=False,
                reason="already_member",
                alert_text="⚠️ أنت منضم لهذه اللعبة بالفعل! ✅",
                show_alert=True,
                session=session,
            )

        if len(session.players) >= session.max_players:
            return OperationResult(
                ok=False,
                reason="lobby_full",
                alert_text="⚠️ اللوبي ممتلئ بحده الأقصى من اللاعبين.",
                show_alert=True,
                session=session,
            )

        clean_name = html.escape(display_name)
        session.players[user_id] = Player(
            user_id=user_id, display_name=clean_name, joined_at=time.time()
        )
        self._store.put(session)

        buttons = self._build_lobby_buttons(session, bot_username)
        players_list = ", ".join([p.display_name for p in session.players.values()])
        text = (
            f"➕ <b>{clean_name}</b> انضم للعبة!\n\n"
            f"👥 <b>اللاعبون ({len(session.players)}/{session.max_players}):</b> {players_list}"
        )

        notif = Notification(
            channel="group",
            target_id=group_chat_id,
            text=text,
            buttons=buttons,
            edit_message_id=session.control_message_id,
        )
        return OperationResult(ok=True, notifications=[notif], session=session)

    def leave_session(
        self, group_chat_id: int, user_id: int, bot_username: str = "guessJobot"
    ) -> OperationResult:
        """Remove a player from LOBBY state session."""
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.LOBBY:
            return OperationResult(
                ok=False,
                reason="cannot_leave",
                alert_text="⚠️ يمكنك المغادرة أثناء تواجد اللعبة في اللوبي فقط.",
                show_alert=True,
                session=session,
            )

        if user_id not in session.players:
            return OperationResult(
                ok=False,
                reason="not_member",
                alert_text="⚠️ أنت لست مشاركاً في هذه اللعبة.",
                show_alert=True,
                session=session,
            )

        departing = session.players.pop(user_id)
        if not session.players:
            session.state = GameState.CANCELLED
            self._store.put(session)
            notif = Notification(
                channel="group",
                target_id=group_chat_id,
                text=f"🚪 غادر <b>{departing.display_name}</b> ولم يبقَ أي لاعب — تم إلغاء اللعبة.",
                edit_message_id=session.control_message_id,
            )
            return OperationResult(ok=True, notifications=[notif], session=session)

        if departing.user_id == session.host_id:
            oldest_player = min(session.players.values(), key=lambda p: p.joined_at)
            session.host_id = oldest_player.user_id

        self._store.put(session)
        buttons = self._build_lobby_buttons(session, bot_username)
        players_list = ", ".join([p.display_name for p in session.players.values()])
        host_p = session.players[session.host_id]

        text = (
            f"🚪 غادر <b>{departing.display_name}</b> اللعبة.\n"
            f"👑 <b>منشئ اللعبة الحالي:</b> {host_p.display_name}\n"
            f"👥 <b>اللاعبون ({len(session.players)}/{session.max_players}):</b> {players_list}"
        )
        notif = Notification(
            channel="group",
            target_id=group_chat_id,
            text=text,
            buttons=buttons,
            edit_message_id=session.control_message_id,
        )
        return OperationResult(ok=True, notifications=[notif], session=session)

    def start_session(
        self, group_chat_id: int, requester_id: int, bot_username: str = "guessJobot"
    ) -> OperationResult:
        """Validate requirements and transition LOBBY session to DEALING state."""
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.LOBBY:
            return OperationResult(
                ok=False,
                reason="not_in_lobby",
                alert_text="⚠️ اللعبة ليست في مرحلة اللوبي لبدئها.",
                show_alert=True,
                session=session,
            )

        if requester_id != session.host_id:
            return OperationResult(
                ok=False,
                reason="not_host",
                alert_text="⚠️ عذراً، فقط منشئ اللعبة (Host) يمكنه بدء اللعبة.",
                show_alert=True,
                session=session,
            )

        active_players = [p for p in session.players.values() if p.active]
        if len(active_players) < session.min_players:
            return OperationResult(
                ok=False,
                reason="below_minimum",
                alert_text=f"⚠️ يلزم وجود {session.min_players} لاعبين على الأقل لبدء اللعبة!",
                show_alert=True,
                session=session,
            )

        # Check DM readiness
        unready_players = [p for p in active_players if not p.dm_ready]
        if unready_players:
            unready_names = ", ".join([p.display_name for p in unready_players])
            buttons = [
                [{"text": "💬 اضغط هنا لتفعيل الخاص مع البوت", "url": f"https://t.me/{bot_username}"}],
                [{"text": "📌 أظهر لوحة الأزرار بالأسفل", "callback_data": f"sg:{session.game_id}:r{session.round_number}:ref"}],
            ]
            notif = Notification(
                channel="group",
                target_id=group_chat_id,
                text=(
                    f"⚠️ <b>لا يمكن بدء اللعبة قبل تفعيل الخاص!</b>\n\n"
                    f"اللاعبون التالية أسماؤهم لم يفعلوا الخاص مع البوت بعد:\n"
                    f"👉 <b>{unready_names}</b>\n\n"
                    f"يرجى الضغط على الزر بالأسفل والضغط على <b>Start</b> بالخاص ثم محاولة البدء مجدداً!"
                ),
                buttons=buttons,
                edit_message_id=session.control_message_id,
            )
            return OperationResult(
                ok=False,
                reason="dm_unready",
                alert_text="⚠️ هناك لاعبون لم يفعلوا الخاص مع البوت بعد!",
                show_alert=True,
                notifications=[notif],
                session=session,
            )

        session.state = GameState.DEALING
        loc = get_random_location()
        session.secret_location_name = loc["name"]
        session.secret_location_word = loc["word"]
        session.secret_category = loc["category"]

        active_uids = [p.user_id for p in active_players]
        spy_uid = random.choice(active_uids)
        session.spy_user_id = spy_uid
        session.votes.clear()
        session.eligible_vote_targets = active_uids.copy()

        dm_notifications: list[Notification] = []
        for p in active_players:
            if p.user_id == spy_uid:
                p.is_spy = True
                p.secret_word = None
                dm_notifications.append(
                    Notification(
                        channel="dm",
                        target_id=p.user_id,
                        text=(
                            "🚨 <b>أنت الجاسوس الوحيد في هذه الجولة! 🕵️‍♂️</b>\n\n"
                            "❌ أنت لا تعرف الكلمة السرية للموقع!\n"
                            "💡 تظاهر بأنك تعرف الكلمة واستمع لأسئلة المنافسين في المحادثة بذكاء حتى تكتشف المكان!"
                        ),
                    )
                )
            else:
                p.is_spy = False
                p.secret_word = loc["word"]
                dm_notifications.append(
                    Notification(
                        channel="dm",
                        target_id=p.user_id,
                        text=(
                            f"👥 <b>أنت مواطن شريف! (لست الجاسوس) ✅</b>\n\n"
                            f"🤫 <b>الكلمة السرية للموقع هي: {loc['name']}</b>\n\n"
                            "احذر أن يكتشفك الجاسوس! اسأل أسئلة ذكية في المحادثة لاكتشاف الجاسوس دون كشف الكلمة السرية."
                        ),
                    )
                )

        self._store.put(session)
        return OperationResult(ok=True, notifications=dm_notifications, session=session)

    def complete_role_dealing(self, group_chat_id: int) -> OperationResult:
        """Transition session state from DEALING to DISCUSSION after DM distribution."""
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.DEALING:
            return OperationResult(ok=False, reason="not_in_dealing", session=session)

        session.state = GameState.DISCUSSION
        buttons = self._build_discussion_buttons(session)

        announcement = Notification(
            channel="group",
            target_id=group_chat_id,
            text=(
                "🕵️‍♂️ <b>تم توزيع الكلمات السرية بالخاص لجميع اللاعبين بنجاح!</b>\n\n"
                "• هناك جاسوس واحد بينكم لا يعرف الكلمة السرية!\n"
                "• ابدأوا النقاش والأسئلة فوراً في المحادثة.\n"
                "• عند الجاهزية، اضغطوا 🗳️ <b>بدء التصويت على الجاسوس</b> أدناه."
            ),
            buttons=buttons,
            edit_message_id=session.control_message_id,
        )
        self._store.put(session)
        return OperationResult(ok=True, notifications=[announcement], session=session)

    def rollback_failed_dealing(
        self, group_chat_id: int, failed_user_ids: list[int], bot_username: str = "guessJobot"
    ) -> OperationResult:
        """Roll back DEALING session to LOBBY if DM role distribution fails."""
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.DEALING:
            return OperationResult(ok=False, reason="not_in_dealing", session=session)

        for uid in failed_user_ids:
            if uid in session.players:
                session.players[uid].dm_ready = False

        session.state = GameState.LOBBY
        session.spy_user_id = None
        session.secret_location_name = ""
        session.secret_location_word = ""

        failed_names = ", ".join(
            [session.players[uid].display_name for uid in failed_user_ids if uid in session.players]
        )
        buttons = self._build_lobby_buttons(session, bot_username)
        notif = Notification(
            channel="group",
            target_id=group_chat_id,
            text=(
                f"❌ <b>فشل إرسال الكلمات بالخاص!</b>\n\n"
                f"تعذر الإرسال للاعبين التالية أسماؤهم بسبب حظر البوت أو عدم تفعيل الخاص:\n"
                f"👉 <b>{failed_names}</b>\n\n"
                f"تمت إعادة اللعبة للوبي. يرجى تفعيل الخاص مع البوت ومحاولة البدء مجدداً."
            ),
            buttons=buttons,
            edit_message_id=session.control_message_id,
        )
        self._store.put(session)
        return OperationResult(ok=True, notifications=[notif], session=session)

    def start_voting_panel(self, group_chat_id: int, requester_id: int) -> OperationResult:
        """Transition DISCUSSION session to VOTING state."""
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.DISCUSSION:
            return OperationResult(
                ok=False,
                reason="not_in_discussion",
                alert_text="⚠️ لا يمكنك فتح التصويت في الوقت الحالي.",
                show_alert=True,
                session=session,
            )

        if requester_id not in session.players or not session.players[requester_id].active:
            return OperationResult(
                ok=False,
                reason="not_active_player",
                alert_text="⚠️ عذراً، فقط اللاعبون المشاركون يستطيعون فتح التصويت!",
                show_alert=True,
                session=session,
            )

        session.state = GameState.VOTING
        session.vote_round = 1
        session.votes.clear()
        session.eligible_vote_targets = [p.user_id for p in session.players.values() if p.active]

        buttons = self._build_voting_buttons(session)
        notif = Notification(
            channel="group",
            target_id=group_chat_id,
            text=(
                "🗳️ <b>تم فتح باب التصويت لاكتشاف الجاسوس!</b>\n\n"
                "اختر الشخص المشتبه به بالضغط على اسمه أدناه (لا يمكنك التصويت لنفسك):"
            ),
            buttons=buttons,
            edit_message_id=session.control_message_id,
        )
        self._store.put(session)
        return OperationResult(ok=True, notifications=[notif], session=session)

    def record_spy_vote(
        self, group_chat_id: int, voter_id: int, target_id: int, game_id: str, vote_round: int
    ) -> OperationResult:
        """Record a vote, handling validation, double-voting prevention, and tie-breaking."""
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.VOTING:
            return OperationResult(
                ok=False,
                reason="not_in_voting",
                alert_text="⚠️ التصويت غير مفتوح حالياً.",
                show_alert=True,
                session=session,
            )

        if session.game_id != game_id or session.vote_round != vote_round:
            return OperationResult(
                ok=False,
                reason="stale_vote_button",
                alert_text="⚠️ انتهت صلاحية هذه اللوحة. استخدم لوحة التصويت الحالية.",
                show_alert=True,
                session=session,
            )

        if voter_id not in session.players or not session.players[voter_id].active:
            return OperationResult(
                ok=False,
                reason="not_active_voter",
                alert_text="⚠️ أنت لست مشاركاً نشطاً في هذه الجولة.",
                show_alert=True,
                session=session,
            )

        if voter_id == target_id:
            return OperationResult(
                ok=False,
                reason="self_voting_prohibited",
                alert_text="⚠️ لا يمكنك التصويت لنفسك!",
                show_alert=True,
                session=session,
            )

        if target_id not in session.eligible_vote_targets:
            return OperationResult(
                ok=False,
                reason="ineligible_target",
                alert_text="⚠️ هذا الهدف غير متاح في جولة التصويت الحالية.",
                show_alert=True,
                session=session,
            )

        if voter_id in session.votes:
            return OperationResult(
                ok=False,
                reason="already_voted",
                alert_text="⚠️ لقد قمت بالتصويت مسبقاً في هذه الجولة! ✅",
                show_alert=True,
                session=session,
            )

        session.votes[voter_id] = target_id
        active_players = [p for p in session.players.values() if p.active]
        total_active = len(active_players)
        voted_count = len(session.votes)

        if voted_count < total_active:
            self._store.put(session)
            buttons = self._build_voting_buttons(session)
            notif = Notification(
                channel="group",
                target_id=group_chat_id,
                text=(
                    f"🗳️ <b>التصويت جارٍ لاكتشاف الجاسوس!</b> (الجولة {session.vote_round})\n\n"
                    f"📊 <b>الأصوات المسجلة حتى الآن:</b> ({voted_count}/{total_active})"
                ),
                buttons=buttons,
                edit_message_id=session.control_message_id,
            )
            return OperationResult(
                ok=True,
                alert_text="✅ تم تسجيل صوتك بنجاح!",
                show_alert=True,
                notifications=[notif],
                session=session,
            )

        # Tally all votes
        tally: dict[int, int] = {}
        for tid in session.votes.values():
            tally[tid] = tally.get(tid, 0) + 1

        max_votes = max(tally.values())
        top_targets = [tid for tid, count in tally.items() if count == max_votes]

        # Check for Tie
        if len(top_targets) > 1:
            session.vote_round += 1
            session.votes.clear()
            session.eligible_vote_targets = top_targets.copy()
            self._store.put(session)

            tied_names = ", ".join([session.players[tid].display_name for tid in top_targets])
            buttons = self._build_voting_buttons(session)
            notif = Notification(
                channel="group",
                target_id=group_chat_id,
                text=(
                    f"⚖️ <b>حدث تعادل في التصويت بين: ({tied_names})!</b>\n\n"
                    f"🔄 تبدأ الآن جولة تصويت جديدة (جولة {session.vote_round}) بين المتعادلين فقط.\n"
                    f"اصوتوا لاختيار المتهم الرئيسي:"
                ),
                buttons=buttons,
                edit_message_id=session.control_message_id,
            )
            return OperationResult(
                ok=True,
                alert_text="⚖️ تعادل في الأصوات! بدأت جولة إعادة تصويت.",
                show_alert=True,
                notifications=[notif],
                session=session,
            )

        # Single accused target
        accused_id = top_targets[0]
        accused_p = session.players.get(accused_id)
        accused_name = accused_p.display_name if accused_p else "لاعب"

        if accused_id == session.spy_user_id:
            session.state = GameState.SPY_LAST_GUESS
            loc_entry: LocationEntry = {
                "name": session.secret_location_name,
                "word": session.secret_location_word,
                "category": session.secret_category,
            }
            options = get_location_options(loc_entry, count=4)
            session.spy_guess_options = [opt["word"] for opt in options]

            buttons = self._build_spy_last_guess_buttons(session, options)
            self._store.put(session)
            notif = Notification(
                channel="group",
                target_id=group_chat_id,
                text=(
                    f"🎉 <b>أحسنتم! تم كشف الجاسوس الحقيقي وهو: {accused_name}! 🕵️‍♂️</b>\n\n"
                    f"💡 <b>الفرصة الأخيرة للجاسوس {accused_name}:</b>\n"
                    f"لديك محاولة واحدة فقط لتخمين المكان السري من الأزرار أدناه للفوز والهروب!"
                ),
                buttons=buttons,
                edit_message_id=session.control_message_id,
            )
            return OperationResult(
                ok=True,
                alert_text="🎉 تم كشف الجاسوس! بدأت فرصة التخمين الأخيرة.",
                show_alert=True,
                notifications=[notif],
                session=session,
            )
        else:
            session.state = GameState.COMPLETED
            spy_p = session.players.get(session.spy_user_id)
            spy_name = spy_p.display_name if spy_p else "الجاسوس"
            self._store.put(session)

            buttons = [[{"text": "🎮 لعبة جديدة", "callback_data": f"sg:{session.game_id}:r{session.round_number}:newgame"}]]
            notif = Notification(
                channel="group",
                target_id=group_chat_id,
                text=(
                    f"❌ <b>للأسف! تم طرد مواطن بريء وهو: {accused_name}! 👥</b>\n\n"
                    f"🕵️‍♂️ <b>الجاسوس الحقيقي كان: {spy_name}!</b>\n"
                    f"🤫 المكان السري كان: <b>{session.secret_location_name}</b>\n\n"
                    f"🏆 <b>فاز الجاسوس {spy_name} بالتمويه والخدع! 🎉</b>"
                ),
                buttons=buttons,
                edit_message_id=session.control_message_id,
            )
            return OperationResult(
                ok=True,
                alert_text="❌ تم طرد مواطن بريء! فاز الجاسوس.",
                show_alert=True,
                notifications=[notif],
                session=session,
            )

    def handle_spy_guess_menu(self, group_chat_id: int, user_id: int, game_id: str) -> OperationResult:
        """Handle tap on Spy Guess Menu button during DISCUSSION state."""
        session = self._store.get(group_chat_id)
        if session is None or session.state not in (GameState.DISCUSSION, GameState.SPY_LAST_GUESS):
            return OperationResult(
                ok=False,
                reason="invalid_state",
                alert_text="⚠️ زر التخمين غير متاح حالياً.",
                show_alert=True,
                session=session,
            )

        if session.game_id != game_id:
            return OperationResult(
                ok=False,
                reason="stale_game_id",
                alert_text="⚠️ انتهت صلاحية هذه اللوحة.",
                show_alert=True,
                session=session,
            )

        if user_id != session.spy_user_id:
            return OperationResult(
                ok=False,
                reason="not_spy",
                alert_text="⚠️ عذراً، هذا الزر مخصص للجاسوس الحقيقي فقط! 🕵️‍♂️",
                show_alert=True,
                session=session,
            )

        session.state = GameState.SPY_LAST_GUESS
        loc_entry: LocationEntry = {
            "name": session.secret_location_name,
            "word": session.secret_location_word,
            "category": session.secret_category,
        }
        options = get_location_options(loc_entry, count=4)
        session.spy_guess_options = [opt["word"] for opt in options]

        buttons = self._build_spy_last_guess_buttons(session, options)
        self._store.put(session)
        notif = Notification(
            channel="group",
            target_id=group_chat_id,
            text=(
                f"💡 <b>الجاسوس يرفع التحدي بالتخمين المباشر! 🕵️‍♂️</b>\n\n"
                f"اختر المكان الصحيح من الأزرار أدناه (محاولة واحدة فقط):"
            ),
            buttons=buttons,
            edit_message_id=session.control_message_id,
        )
        return OperationResult(
            ok=True,
            alert_text="💡 اختر المكان الصحيح من الأزرار!",
            show_alert=True,
            notifications=[notif],
            session=session,
        )

    def submit_spy_location_guess(
        self, group_chat_id: int, spy_id: int, option_index: int, game_id: str
    ) -> OperationResult:
        """Submit the single allowed Spy location guess by index."""
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.SPY_LAST_GUESS:
            return OperationResult(
                ok=False,
                reason="not_in_spy_guess",
                alert_text="⚠️ التخمين غير متاح حالياً.",
                show_alert=True,
                session=session,
            )

        if session.game_id != game_id:
            return OperationResult(
                ok=False,
                reason="stale_game_id",
                alert_text="⚠️ انتهت صلاحية هذه اللوحة.",
                show_alert=True,
                session=session,
            )

        if spy_id != session.spy_user_id:
            return OperationResult(
                ok=False,
                reason="not_spy",
                alert_text="⚠️ عذراً، التخمين مخصص للجاسوس فقط!",
                show_alert=True,
                session=session,
            )

        if session.spy_guess_attempted:
            return OperationResult(
                ok=False,
                reason="already_attempted",
                alert_text="⚠️ تمت محاولة التخمين مسبقاً!",
                show_alert=True,
                session=session,
            )

        session.spy_guess_attempted = True
        session.state = GameState.COMPLETED

        if option_index < 0 or option_index >= len(session.spy_guess_options):
            word_guess = ""
        else:
            word_guess = session.spy_guess_options[option_index]

        spy_p = session.players.get(spy_id)
        spy_name = spy_p.display_name if spy_p else "الجاسوس"
        buttons = [[{"text": "🎮 لعبة جديدة", "callback_data": f"sg:{session.game_id}:r{session.round_number}:newgame"}]]

        if word_guess and word_guess.strip().lower() == session.secret_location_word.lower():
            notif = Notification(
                channel="group",
                target_id=group_chat_id,
                text=(
                    f"🎉 <b>تخمين عبقري من الجاسوس! 🕵️‍♂️🏆</b>\n\n"
                    f"عرف الجاسوس <b>{spy_name}</b> المكان السري الصحيح وهو: <b>{session.secret_location_name}</b> وقام بالهروب وفاز بالجولة!"
                ),
                buttons=buttons,
                edit_message_id=session.control_message_id,
            )
        else:
            notif = Notification(
                channel="group",
                target_id=group_chat_id,
                text=(
                    f"❌ <b>تخمين خاطئ من الجاسوس! 🕵️‍♂️</b>\n\n"
                    f"حاول الجاسوس {spy_name} التخمين لكن الإجابة كانت خاطئة!\n"
                    f"🤫 المكان السري الحقيقي كان: <b>{session.secret_location_name}</b>\n\n"
                    f"🏆 <b>فاز المواطنون الشرفاء بالجولة! 👥🎉</b>"
                ),
                buttons=buttons,
                edit_message_id=session.control_message_id,
            )

        self._store.put(session)
        return OperationResult(ok=True, notifications=[notif], session=session)

    def cancel_session(
        self, group_chat_id: int, user_id: int, game_id: str
    ) -> OperationResult:
        """Cancel an active session (Host-only)."""
        session = self._store.get(group_chat_id)
        if session is None or session.state not in _NON_TERMINAL_STATES:
            return OperationResult(
                ok=False,
                reason="no_active_session",
                alert_text="⚠️ لا توجد لعبة نشطة لإلغائها.",
                show_alert=True,
                session=session,
            )

        if session.game_id != game_id:
            return OperationResult(
                ok=False,
                reason="stale_game_id",
                alert_text="⚠️ انتهت صلاحية هذه اللوحة.",
                show_alert=True,
                session=session,
            )

        if user_id != session.host_id:
            return OperationResult(
                ok=False,
                reason="not_host",
                alert_text="⚠️ عذراً، فقط منشئ اللعبة (Host) يمكنه إلغاء اللعبة.",
                show_alert=True,
                session=session,
            )

        host_p = session.players.get(user_id)
        host_name = host_p.display_name if host_p else "منشئ اللعبة"
        session.state = GameState.CANCELLED

        buttons = [[{"text": "🎮 لعبة جديدة", "callback_data": f"sg:{session.game_id}:r{session.round_number}:newgame"}]]
        notif = Notification(
            channel="group",
            target_id=group_chat_id,
            text=f"❌ قام <b>{host_name}</b> بإلغاء اللعبة.",
            buttons=buttons,
            edit_message_id=session.control_message_id,
        )
        self._store.put(session)
        return OperationResult(ok=True, notifications=[notif], session=session)

    # Button Builders
    def _build_lobby_buttons(self, session: GameSession, bot_username: str) -> list[list[dict[str, str]]]:
        gid = session.game_id
        r = f"r{session.round_number}"
        return [
            [{"text": "💬 1. تفعيل الخاص مع البوت (اضغط هنا أولاً)", "url": f"https://t.me/{bot_username}"}],
            [
                {"text": "➕ 2. انضمام للعبة", "callback_data": f"sg:{gid}:{r}:join"},
                {"text": "🚪 مغادرة", "callback_data": f"sg:{gid}:{r}:leave"},
            ],
            [
                {"text": "🚀 3. بدء اللعبة", "callback_data": f"sg:{gid}:{r}:start"},
                {"text": "❌ إلغاء اللعبة", "callback_data": f"sg:{gid}:{r}:cancel"},
            ],
            [{"text": "📌 أظهر لوحة الأزرار بالأسفل", "callback_data": f"sg:{gid}:{r}:ref"}],
        ]

    def _build_discussion_buttons(self, session: GameSession) -> list[list[dict[str, str]]]:
        gid = session.game_id
        r = f"r{session.round_number}"
        return [
            [{"text": "🗳️ بدء التصويت على الجاسوس", "callback_data": f"sg:{gid}:{r}:openvote"}],
            [{"text": "💡 تخمين الكلمة السرية (للجاسوس)", "callback_data": f"sg:{gid}:{r}:spymenu"}],
            [{"text": "📌 أظهر لوحة الأزرار بالأسفل", "callback_data": f"sg:{gid}:{r}:ref"}],
        ]

    def _build_voting_buttons(self, session: GameSession) -> list[list[dict[str, str]]]:
        gid = session.game_id
        r = f"r{session.round_number}"
        vr = f"v{session.vote_round}"

        buttons: list[list[dict[str, str]]] = []
        eligible_players = [
            p for p in session.players.values()
            if p.active and p.user_id in session.eligible_vote_targets
        ]

        row: list[dict[str, str]] = []
        for p in eligible_players:
            row.append({"text": f"👤 {p.display_name}", "callback_data": f"sg:{gid}:{r}:{vr}:vote:{p.user_id}"})
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        buttons.append([{"text": "📌 أظهر لوحة الأزرار بالأسفل", "callback_data": f"sg:{gid}:{r}:ref"}])
        return buttons

    def _build_spy_last_guess_buttons(
        self, session: GameSession, options: list[LocationEntry]
    ) -> list[list[dict[str, str]]]:
        gid = session.game_id
        r = f"r{session.round_number}"

        buttons: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for idx, opt in enumerate(options):
            row.append({"text": opt["name"], "callback_data": f"sg:{gid}:{r}:spyopt:{idx}"})
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        return buttons
