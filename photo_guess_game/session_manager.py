"""Pure game logic engine for Telegram Spy Game."""

from __future__ import annotations

import html
import random
import time
import uuid
from typing import Callable

from .locations import LocationEntry, get_location_options, get_random_location
from .models import (
    GameSession,
    GameState,
    Notification,
    OperationResult,
    Player,
    RoundPhase,
)
from .session_store import SessionStore, _NON_TERMINAL_STATES

MIN_PLAYERS_TO_START = 2
MIN_PLAYERS_TO_VOTE = 3
MAX_PLAYERS = 15
DEFAULT_TIMEOUT_SECONDS = 300
SPY_GUESS_OPTION_COUNT = 4

LOBBY_BUTTONS = [
    [
        {"text": "➕ انضمام للعبة", "callback_data": "join_game"},
        {"text": "🚪 مغادرة اللعبة", "callback_data": "leave_game"},
    ],
    [
        {"text": "🚀 بدء اللعبة", "callback_data": "start_game"},
        {"text": "❌ إلغاء اللعبة", "callback_data": "cancel_game"},
    ],
    [{"text": "📌 أظهر لوحة الأزرار بالأسفل", "callback_data": "refresh_panel"}],
]

# The spy's guess button deliberately does NOT live here.  ``build_spy_guess_menu``
# requires ``spy_guessing_active``, which only becomes true after a ballot
# exposes the spy, so offering it during discussion produced a button that
# always answered "التخمين غير متاح حالياً".  It is attached to the
# spy-exposed message instead, which is the only moment it can work.
ACTIVE_PANEL_BUTTONS = [
    [{"text": "🗳️ بدء التصويت على الجاسوس", "callback_data": "start_voting"}],
    [{"text": "📌 أظهر لوحة الأزرار بالأسفل", "callback_data": "refresh_panel"}],
]

SPY_GUESS_BUTTONS = [
    [{"text": "💡 تخمين الكلمة السرية (الجاسوس)", "callback_data": "spy_guess_menu"}],
]


class SessionManager:
    """Core state machine and rules engine for Spy Game."""

    def __init__(
        self,
        store: SessionStore,
        timer_service=None,
        on_notification_cb: Callable[[list[Notification]], None] | None = None,
        timeout_dispatcher: Callable[[str, int], None] | None = None,
        round_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._store = store
        self._timer_service = timer_service
        self._on_notification_cb = on_notification_cb
        self._timeout_dispatcher = timeout_dispatcher
        self._round_seconds = round_seconds

    @staticmethod
    def generate_game_id() -> str:
        """Generate a short 4-character hex game_id."""
        return uuid.uuid4().hex[:4]

    # ------------------------------------------------------------------
    # Lobby lifecycle
    # ------------------------------------------------------------------
    def create_session(
        self, group_chat_id: int, host_id: int, host_name: str, bot_username: str = "guessJobot"
    ) -> OperationResult:
        """Initialize a new GameSession in LOBBY state.

        Rejects the request without mutation when a non-terminal session
        already exists for the group, regardless of age, so an active lobby
        or round is never silently replaced.
        """
        existing = self._store.get(group_chat_id)
        if existing is not None and existing.state in _NON_TERMINAL_STATES:
            return OperationResult(
                ok=False,
                reason="session_already_active",
                alert_text="⚠️ هناك لعبة نشطة حالياً في هذه المجموعة!",
                show_alert=True,
                session=existing,
            )

        current_time = time.time()
        clean_host_name = html.escape(host_name)
        host = Player(user_id=host_id, display_name=clean_host_name, joined_at=current_time)
        session = GameSession(
            game_id=self.generate_game_id(),
            group_chat_id=group_chat_id,
            host_id=host_id,
            state=GameState.LOBBY,
            players={host_id: host},
            created_at=current_time,
            last_activity_at=current_time,
            min_players=MIN_PLAYERS_TO_START,
            max_players=MAX_PLAYERS,
            generation=self._store.next_generation(group_chat_id),
            guessing_timeout_seconds=self._round_seconds,
        )
        self._store.put(session)

        announcement = Notification(
            channel="group",
            target_id=group_chat_id,
            text=(
                f"🕵️ <b>{clean_host_name}</b> بدأ لعبة <b>الجاسوس والكلمة السرية</b>!\n\n"
                "<blockquote expandable>\n"
                "<b>💡 فكرة اللعبة:</b>\n"
                "البوت يرسل كلمة سرية واحدة بالخاص لجميع اللاعبين، ولكنه يختار شخصاً واحداً "
                "ليكون <b>الجاسوس 🕵️</b> (لا يعرف الكلمة!).\n"
                "اطرحوا أسئلة على بعضكم في المحادثة لاكتشاف الجاسوس دون إفشاء الكلمة السرية!\n"
                "</blockquote>\n\n"
                "<b>📋 طريقة اللعب:</b>\n"
                "1️⃣ اضغط <b>➕ انضمام للعبة</b> أدناه.\n"
                "2️⃣ اضغط <b>🚀 بدء اللعبة</b> للتصويت وتلقي الكلمات السرية بالخاص!\n\n"
                f"👥 <b>اللوبي مفتوح الآن:</b> ({len(session.players)}/{session.max_players} لاعبين)"
            ),
            buttons=[list(row) for row in LOBBY_BUTTONS],
        )
        return OperationResult(ok=True, notifications=[announcement], session=session)

    def join_session(
        self, group_chat_id: int, user_id: int, display_name: str, bot_username: str = "guessJobot"
    ) -> OperationResult:
        """Add a player to a LOBBY state session."""
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

        notif = Notification(
            channel="group",
            target_id=group_chat_id,
            text=(
                f"👤 <b>{clean_name}</b> انضم للعبة! "
                f"({len(session.players)}/{session.max_players} لاعبين)"
            ),
            buttons=[list(row) for row in LOBBY_BUTTONS],
            edit_message_id=session.control_message_id,
        )
        return OperationResult(ok=True, notifications=[notif], session=session)

    def leave_session(
        self, group_chat_id: int, user_id: int, bot_username: str = "guessJobot"
    ) -> OperationResult:
        """Remove a player from a LOBBY state session."""
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
                text=f"🚪 {departing.display_name} غادر اللعبة ولم يبقَ أي لاعب — تم إلغاء اللعبة.",
                buttons=[list(row) for row in LOBBY_BUTTONS],
            )
            return OperationResult(ok=True, notifications=[notif], session=session)

        host_transferred = False
        if departing.user_id == session.host_id:
            oldest_player = min(session.players.values(), key=lambda p: p.joined_at)
            session.host_id = oldest_player.user_id
            host_transferred = True

        self._store.put(session)

        if host_transferred:
            new_host = session.players[session.host_id]
            text = (
                f"🚪 {departing.display_name} غادر اللعبة.\n"
                f"أصبح <b>{new_host.display_name}</b> منشئ اللعبة (Host) الآن."
            )
        else:
            text = (
                f"🚪 {departing.display_name} غادر اللعبة. "
                f"({len(session.players)}/{session.max_players} لاعبين)"
            )

        notif = Notification(
            channel="group",
            target_id=group_chat_id,
            text=text,
            buttons=[list(row) for row in LOBBY_BUTTONS],
            edit_message_id=session.control_message_id,
        )
        return OperationResult(ok=True, notifications=[notif], session=session)

    def mark_dm_ready(self, user_id: int) -> None:
        """Mark a user as having DM enabled across active sessions."""
        for session in list(self._store._sessions.values()):
            if user_id in session.players:
                session.players[user_id].dm_ready = True

    # ------------------------------------------------------------------
    # Round start
    # ------------------------------------------------------------------
    def start_session(
        self, group_chat_id: int, requester_id: int, bot_username: str = "guessJobot"
    ) -> OperationResult:
        """Validate requirements and transition a LOBBY session to GUESSING.

        Assigns roles and the secret word once, produces the group readiness
        announcement followed by one private role message per active player,
        and schedules the one-shot round timers.
        """
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
        if len(active_players) < MIN_PLAYERS_TO_START:
            return OperationResult(
                ok=False,
                reason="below_minimum",
                alert_text=f"⚠️ يلزم وجود {MIN_PLAYERS_TO_START} لاعبين على الأقل لبدء اللعبة!",
                show_alert=True,
                session=session,
            )

        loc = get_random_location()
        session.secret_location_name = loc["name"]
        session.secret_location_word = loc["word"]
        session.secret_category = loc.get("category", "")

        active_uids = [p.user_id for p in active_players]
        spy_uid = random.choice(active_uids)
        session.spy_user_id = spy_uid
        session.votes.clear()
        session.eligible_vote_targets = active_uids.copy()
        session.voting_active = False
        session.spy_guessing_active = False
        session.spy_guess_attempted = False
        session.state = GameState.GUESSING

        group_announcement = Notification(
            channel="group",
            target_id=group_chat_id,
            text=(
                "🕵️ <b>تم توزيع الكلمات السرية بالخاص لجميع اللاعبين!</b>\n\n"
                "• هناك <b>جاسوس واحد</b> بينكم لا يعرف الكلمة السرية!\n"
                "• ابدأوا النقاش والأسئلة فوراً في المحادثة.\n"
                "• عند الجاهزية، اضغطوا <b>🗳️ بدء التصويت على الجاسوس</b> أدناه."
            ),
            # This message told players to press a button "أدناه" while carrying
            # no keyboard at all, so a started round had no reachable panel.
            buttons=[list(row) for row in ACTIVE_PANEL_BUTTONS],
            # Sent as a new message (not an edit) so the panel sits at the
            # bottom of the chat, and the now-obsolete lobby keyboard above it
            # is stripped rather than left clickable.
            disable_previous_message_id=session.control_message_id,
        )
        notifications: list[Notification] = [group_announcement]

        for player in session.players.values():
            if not player.active:
                continue
            if player.user_id == spy_uid:
                player.is_spy = True
                player.secret_word = None
                notifications.append(
                    Notification(
                        channel="dm",
                        target_id=player.user_id,
                        text=(
                            "🚨 <b>أنت الجاسوس الوحيد في هذه الجولة! 🕵️♂️</b>\n\n"
                            "❌ أنت لا تعرف الكلمة السرية للموقع!\n"
                            "💡 تظاهر بأنك تعرف الكلمة واستمع لأسئلة المنافسين في المحادثة بذكاء حتى تكتشف المكان!"
                        ),
                    )
                )
            else:
                player.is_spy = False
                player.secret_word = loc["word"]
                notifications.append(
                    Notification(
                        channel="dm",
                        target_id=player.user_id,
                        text=(
                            "👥 <b>أنت مواطن شريف! (لست الجاسوس) ✅</b>\n\n"
                            f"🤫 <b>الكلمة السرية للموقع هي: {loc['name']}</b>\n\n"
                            "احذر أن يكتشفك الجاسوس! اسأل أسئلة ذكية في المحادثة لاكتشاف الجاسوس دون كشف الكلمة السرية."
                        ),
                    )
                )

        self._store.put(session)
        self._schedule_round_timers(session)

        return OperationResult(ok=True, notifications=notifications, session=session)

    def _schedule_round_timers(self, session: GameSession) -> None:
        """Arm the half-time reminder and the round deadline for this generation.

        Uses the generation-bound ``schedule`` API rather than the legacy
        ``start(group_chat_id, ...)`` helper.  The legacy helper hardcoded
        generation ``0``, which no real session ever has, so any deployment that
        wired ``session_lookup`` would have had every timer silently rejected.

        ``expected_revision`` is deliberately left unset: the store bumps the
        revision on every commit, so pinning it would let an ordinary vote
        cancel the round deadline.
        """
        if self._timer_service is None:
            return

        key = session.session_key
        timeout = session.guessing_timeout_seconds
        if timeout <= 0:
            self._fire_timeout("expired", session.group_chat_id)
            return

        self._timer_service.schedule(
            key,
            "half_elapsed",
            f"{session.game_id}-r{session.round_number}-half",
            timeout / 2,
            lambda event: self._fire_timeout(
                "half_elapsed", event.session_key.group_chat_id
            ),
            # Skipped automatically once a ballot or the spy's guess is open,
            # where a "hurry up and vote" nudge would be noise.
            expected_phase=RoundPhase.DISCUSSION,
        )
        self._timer_service.schedule(
            key,
            "expired",
            f"{session.game_id}-r{session.round_number}-expired",
            timeout,
            lambda event: self._fire_timeout(
                "expired", event.session_key.group_chat_id
            ),
        )

    def _fire_timeout(self, kind: str, group_chat_id: int) -> None:
        """Hand a fired timer to the injected dispatcher, or resolve inline.

        Timer callbacks run synchronously in the event loop, so mutating a
        session directly from one can interleave with a handler that is holding
        the group lock across an ``await``.  When a dispatcher is injected the
        work is re-entered asynchronously under that lock instead.  The inline
        branch keeps the component usable in synchronous tests.
        """
        if self._timeout_dispatcher is not None:
            self._timeout_dispatcher(kind, group_chat_id)
            return
        if kind == "half_elapsed":
            self._on_half_elapsed(group_chat_id)
        else:
            self._on_expired(group_chat_id)

    def rollback_failed_start(
        self, group_chat_id: int, failed_user_ids: list[int], bot_username: str = "guessJobot"
    ) -> OperationResult:
        """Roll a GUESSING round back to LOBBY when role delivery fails.

        Keeps the round unplayable: clears roles/secret, cancels timers, and
        returns a group notification naming the failed recipients without any
        success announcement.
        """
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.GUESSING:
            return OperationResult(ok=False, reason="not_in_guessing", session=session)

        if self._timer_service is not None:
            self._timer_service.cancel(group_chat_id)

        for uid in failed_user_ids:
            if uid in session.players:
                session.players[uid].dm_ready = False

        for player in session.players.values():
            player.is_spy = False
            player.secret_word = None

        session.state = GameState.LOBBY
        session.spy_user_id = None
        session.secret_location_name = ""
        session.secret_location_word = ""
        session.secret_category = ""
        session.votes.clear()
        session.voting_active = False
        session.spy_guessing_active = False

        failed_names = ", ".join(
            session.players[uid].display_name
            for uid in failed_user_ids
            if uid in session.players
        )
        notif = Notification(
            channel="group",
            target_id=group_chat_id,
            text=(
                "❌ <b>فشل إرسال الكلمات السرية بالخاص!</b>\n\n"
                "تعذر الإرسال للاعبين التالية أسماؤهم بسبب حظر البوت أو عدم تفعيل الخاص:\n"
                f"👉 <b>{failed_names}</b>\n\n"
                "تمت إعادة اللعبة للوبي. يرجى تفعيل الخاص مع البوت ومحاولة البدء مجدداً."
            ),
            buttons=[list(row) for row in LOBBY_BUTTONS],
            edit_message_id=session.control_message_id,
        )
        self._store.put(session)
        return OperationResult(ok=False, reason="role_delivery_failed", notifications=[notif], session=session)

    # ------------------------------------------------------------------
    # Voting
    # ------------------------------------------------------------------
    def start_voting_panel(self, group_chat_id: int) -> OperationResult:
        """Open the spy ballot for an active GUESSING round."""
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.GUESSING:
            return OperationResult(
                ok=False,
                reason="not_playing",
                alert_text="⚠️ لا يمكنك فتح التصويت في الوقت الحالي.",
                show_alert=True,
                session=session,
            )

        if session.voting_active:
            return OperationResult(
                ok=False,
                reason="voting_already_open",
                alert_text="⚠️ التصويت مفتوح بالفعل.",
                show_alert=True,
                session=session,
            )

        active_players = [p for p in session.players.values() if p.active]
        if len(active_players) < MIN_PLAYERS_TO_VOTE:
            return OperationResult(
                ok=False,
                reason="not_enough_players",
                alert_text=f"⚠️ يلزم {MIN_PLAYERS_TO_VOTE} لاعبين على الأقل لفتح التصويت.",
                show_alert=True,
                session=session,
            )

        session.voting_active = True
        session.vote_round = 1
        session.votes.clear()
        session.eligible_vote_targets = [p.user_id for p in active_players]

        self._store.put(session)
        notif = self._panel_update(
            session, self._vote_progress_text(session, active_players)
        )
        return OperationResult(ok=True, notifications=[notif], session=session)

    def record_spy_vote(
        self, group_chat_id: int, voter_id: int, target_id: int
    ) -> OperationResult:
        """Record a single vote, resolving the round when every active voter has voted."""
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.GUESSING or not session.voting_active:
            return OperationResult(
                ok=False,
                reason="voting_not_open",
                alert_text="⚠️ التصويت غير مفتوح حالياً.",
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

        if target_id not in session.eligible_vote_targets or target_id not in session.players:
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
            # Refresh the ballot in place, keyboard intact, so the remaining
            # voters can still act.  This used to be an edit with no buttons,
            # which deleted the ballot after the very first vote.
            return OperationResult(
                ok=True,
                alert_text="✅ تم تسجيل صوتك بنجاح!",
                show_alert=True,
                notifications=[
                    self._panel_update(
                        session, self._vote_progress_text(session, active_players)
                    )
                ],
                session=session,
            )

        # Everyone active has voted -> tally.
        tally: dict[int, int] = {}
        for tid in session.votes.values():
            tally[tid] = tally.get(tid, 0) + 1
        max_votes = max(tally.values())
        top_targets = [tid for tid, count in tally.items() if count == max_votes]

        if len(top_targets) > 1:
            # Tie: close the ballot without eliminating anyone.
            session.voting_active = False
            session.votes.clear()
            session.eligible_vote_targets = [p.user_id for p in active_players]
            self._store.put(session)
            # The ballot is closed but the round continues, so the panel must
            # come back with the "reopen voting" keyboard rather than none.
            tie_notif = self._panel_update(
                session,
                "⚖️ <b>تعادل في التصويت!</b>\n\n"
                "لم يتم إقصاء أي لاعب. تابعوا النقاش وأعيدوا فتح التصويت عند الجاهزية.",
            )
            return OperationResult(
                ok=True,
                alert_text="⚖️ تعادل! لم يُقصَ أحد.",
                show_alert=True,
                notifications=[tie_notif],
                session=session,
            )

        accused_id = top_targets[0]
        accused_name = session.players[accused_id].display_name
        session.voting_active = False

        if accused_id == session.spy_user_id:
            # Spy exposed: grant the single final guess opportunity.
            session.spy_guessing_active = True
            session.spy_guess_attempted = False
            self._store.put(session)
            # ``_panel_buttons`` now resolves to the spy-guess keyboard, which
            # is the only state where that button is actionable.
            caught_notif = self._panel_update(
                session,
                f"🎯 <b>تم كشف الجاسوس: {accused_name}! 🕵️♂️</b>\n\n"
                f"🗳️ صوّت ضده <b>{max_votes}</b> من {total_active}.\n"
                "💡 لديه فرصة أخيرة لتخمين الكلمة السرية للفوز.",
            )
            return OperationResult(
                ok=True,
                alert_text="🎯 تم كشف الجاسوس!",
                show_alert=True,
                notifications=[caught_notif],
                session=session,
            )

        # Innocent accused -> spy wins.
        spy_player = session.players.get(session.spy_user_id)
        spy_name = spy_player.display_name if spy_player else "الجاسوس"
        secret_name = session.secret_location_name
        session.state = GameState.COMPLETED
        if self._timer_service is not None:
            self._timer_service.cancel(group_chat_id)
        self._store.put(session)
        # Terminal: _panel_buttons returns None here, which is the one case
        # where dropping the keyboard is correct.
        win_notif = self._panel_update(
            session,
            "🎉 <b>فاز الجاسوس! 🕵️🏆</b>\n\n"
            f"قام الجميع بطرد خاطئ لـ <b>{accused_name}</b>!\n"
            f"بينما الجاسوس الحقيقي <b>{spy_name}</b> نجح بالتمويه والمكر وخدع الجميع!\n"
            f"📍 المكان السري كان: <b>{secret_name}</b>",
        )
        return OperationResult(
            ok=True,
            alert_text="🎉 فاز الجاسوس!",
            show_alert=True,
            notifications=[win_notif],
            session=session,
        )

    # ------------------------------------------------------------------
    # Spy final guess
    # ------------------------------------------------------------------
    def submit_spy_location_guess(
        self, group_chat_id: int, actor_id: int, word: str
    ) -> OperationResult:
        """Submit the spy's single allowed location guess by word."""
        session = self._store.get(group_chat_id)
        if session is None or not session.spy_guessing_active:
            return OperationResult(
                ok=False,
                reason="not_in_spy_guess",
                alert_text="⚠️ التخمين غير متاح حالياً.",
                show_alert=True,
                session=session,
            )

        if actor_id != session.spy_user_id:
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
        session.spy_guessing_active = False
        session.state = GameState.COMPLETED
        if self._timer_service is not None:
            self._timer_service.cancel(group_chat_id)

        spy_player = session.players.get(session.spy_user_id)
        spy_name = spy_player.display_name if spy_player else "الجاسوس"
        secret_word = session.secret_location_word or ""
        secret_name = session.secret_location_name
        correct = bool(word) and word.strip().lower() == secret_word.strip().lower()

        if correct:
            text = (
                "🎉 <b>تخمين عبقري من الجاسوس! 🕵️♂️🏆</b>\n\n"
                f"عرف الجاسوس <b>{spy_name}</b> المكان السري الصحيح وهو: <b>{secret_name}</b> وفاز بالجولة!"
            )
        else:
            text = (
                "❌ <b>تخمين خاطئ من الجاسوس! 🕵️♂️</b>\n\n"
                f"حاول الجاسوس {spy_name} التخمين لكن الإجابة كانت خاطئة!\n"
                f"🤫 المكان السري الحقيقي كان: <b>{secret_name}</b>\n\n"
                "🏆 <b>فاز المواطنون الشرفاء بالجولة! 👥🎉</b>"
            )

        self._store.put(session)
        notif = Notification(
            channel="group",
            target_id=group_chat_id,
            text=text,
            edit_message_id=session.control_message_id,
        )
        return OperationResult(ok=True, notifications=[notif], session=session)

    # ------------------------------------------------------------------
    # Cancellation
    # ------------------------------------------------------------------
    def cancel_session(self, group_chat_id: int, user_id: int) -> OperationResult:
        """Cancel an active session (Host-only) and scrub its resources."""
        session = self._store.get(group_chat_id)
        if session is None or session.state not in _NON_TERMINAL_STATES:
            return OperationResult(
                ok=False,
                reason="no_active_session",
                alert_text="⚠️ لا توجد لعبة نشطة لإلغائها.",
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

        if self._timer_service is not None:
            self._timer_service.cancel(group_chat_id)

        host_name = session.players[session.host_id].display_name
        self._scrub_terminal(session, GameState.CANCELLED)
        self._store.put(session)

        notif = Notification(
            channel="group",
            target_id=group_chat_id,
            text=f"❌ قام <b>{host_name}</b> بإلغاء اللعبة.",
        )
        return OperationResult(ok=True, notifications=[notif], session=session)

    # ------------------------------------------------------------------
    # Timeout / reveal
    # ------------------------------------------------------------------
    def half_time_reminder(self, group_chat_id: int) -> OperationResult:
        """Build the half-time nudge without mutating the session."""
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.GUESSING:
            return OperationResult(ok=False, reason="not_in_guessing", session=session)
        notif = Notification(
            channel="group",
            target_id=group_chat_id,
            text="⏳ <b>تذكير: نصف الوقت مضى!</b> سارعوا بالنقاش والتصويت.",
        )
        return OperationResult(ok=True, notifications=[notif], session=session)

    def _on_half_elapsed(self, group_chat_id: int) -> OperationResult:
        res = self.half_time_reminder(group_chat_id)
        if res.ok and self._on_notification_cb is not None:
            self._on_notification_cb(res.notifications)
        return res

    def _on_expired(self, group_chat_id: int) -> OperationResult:
        res = self.enter_reveal(group_chat_id)
        if res.ok and self._on_notification_cb is not None:
            self._on_notification_cb(res.notifications)
        return res

    def enter_reveal(self, group_chat_id: int) -> OperationResult:
        """Resolve a GUESSING round whose discussion timer expired.

        Nobody exposed the spy before the deadline, so the spy takes the round.
        The spy's identity and the secret location are disclosed before the
        session is scrubbed, because scrubbing clears both.
        """
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.GUESSING:
            return OperationResult(ok=False, reason="not_in_guessing", session=session)

        session.state = GameState.REVEAL

        spy_player = (
            session.players.get(session.spy_user_id)
            if session.spy_user_id is not None
            else None
        )
        spy_name = spy_player.display_name if spy_player is not None else "الجاسوس"
        secret_name = session.secret_location_name or "غير معروف"

        if self._timer_service is not None:
            self._timer_service.cancel(group_chat_id)

        notif = Notification(
            channel="group",
            target_id=group_chat_id,
            text=(
                "⏰ <b>انتهى الوقت ولم يُكشف الجاسوس!</b>\n\n"
                f"🕵️ الجاسوس كان: <b>{spy_name}</b>\n"
                f"📍 المكان السري كان: <b>{secret_name}</b>\n\n"
                "🏆 <b>فاز الجاسوس بالجولة! 🕵️🎉</b>"
            ),
            edit_message_id=session.control_message_id,
        )

        self._scrub_terminal(session, GameState.COMPLETED)
        self._store.put(session)
        return OperationResult(ok=True, notifications=[notif], session=session)

    # ------------------------------------------------------------------
    # Spy final-guess menu
    # ------------------------------------------------------------------
    def build_spy_guess_menu(self, group_chat_id: int, actor_id: int) -> OperationResult:
        """Offer the exposed spy a fixed multiple-choice ballot of locations.

        The option list is generated once per round and then reused, so the spy
        cannot reroll for an easier set of distractors by reopening the menu.
        """
        session = self._store.get(group_chat_id)
        if session is None or not session.spy_guessing_active:
            return OperationResult(
                ok=False,
                reason="not_in_spy_guess",
                alert_text="⚠️ التخمين غير متاح حالياً.",
                show_alert=True,
                session=session,
            )

        if actor_id != session.spy_user_id:
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

        if not session.spy_guess_options:
            secret_entry: LocationEntry = {
                "name": session.secret_location_name or "",
                "word": session.secret_location_word or "",
                "category": session.secret_category,
            }
            options = get_location_options(secret_entry, SPY_GUESS_OPTION_COUNT)
            session.spy_guess_options = [option["word"] for option in options]
            session.spy_guess_labels = [option["name"] for option in options]
            self._store.put(session)

        buttons: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for index, label in enumerate(session.spy_guess_labels):
            row.append({"text": label, "callback_data": f"spy_guess:{index}"})
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        notif = Notification(
            channel="group",
            target_id=group_chat_id,
            text=(
                "💡 <b>فرصة الجاسوس الأخيرة!</b>\n\n"
                "على الجاسوس اختيار المكان السري الصحيح من الخيارات أدناه.\n"
                "<b>محاولة واحدة فقط — لا تراجع.</b>"
            ),
            buttons=buttons,
        )
        return OperationResult(ok=True, notifications=[notif], session=session)

    def resolve_spy_guess_option(self, group_chat_id: int, option_index: int) -> str | None:
        """Map a menu button index back to its secret word, or ``None``."""
        session = self._store.get(group_chat_id)
        if session is None:
            return None
        if not 0 <= option_index < len(session.spy_guess_options):
            return None
        return session.spy_guess_options[option_index]

    # ------------------------------------------------------------------
    # Panel / helpers
    # ------------------------------------------------------------------
    def build_status_panel_notification(self, group_chat_id: int, header: str) -> Notification:
        """Build a fresh control panel reflecting the session's current state.

        The keyboard is derived from the live state rather than hardcoded to the
        discussion panel, otherwise re-showing the panel mid-ballot replaced the
        open vote buttons with a "start voting" button and stranded the round.
        """
        session = self._store.get(group_chat_id)
        if session is None:
            return Notification(
                channel="group",
                target_id=group_chat_id,
                text=f"{header}\n\n⚠️ لا توجد لعبة نشطة في هذه المجموعة.",
            )

        if session.state is GameState.LOBBY:
            body = (
                f"👥 <b>اللوبي مفتوح:</b> "
                f"({len(session.players)}/{session.max_players} لاعبين)\n"
                "اضغط <b>➕ انضمام للعبة</b>، ثم يبدأ المنشئ الجولة."
            )
        elif session.spy_guessing_active:
            body = "🎯 تم كشف الجاسوس! أمامه فرصة تخمين واحدة أدناه."
        elif session.voting_active:
            active_players = [p for p in session.players.values() if p.active]
            body = self._vote_progress_text(session, active_players)
        else:
            body = (
                "🕵️ <b>لوحة التحكم للجولة النشطة:</b>\n"
                "الأسئلة مستمرة في المجموعة! عند الجاهزية اضغط زر التصويت أدناه."
            )

        return Notification(
            channel="group",
            target_id=group_chat_id,
            text=f"{header}\n\n{body}",
            buttons=self._panel_buttons(session),
        )

    def _panel_buttons(self, session: GameSession) -> list[list[dict[str, str]]] | None:
        """Return the keyboard that matches the session's current state.

        Every notification that edits the control panel must pass its result
        through here.  Editing a message without ``reply_markup`` strips its
        keyboard permanently, which previously left the group with no way to
        act and the round unfinishable.
        """
        if session.terminal:
            return None
        if session.state is GameState.LOBBY:
            return [list(row) for row in LOBBY_BUTTONS]
        if session.state is GameState.GUESSING:
            if session.spy_guessing_active:
                return [list(row) for row in SPY_GUESS_BUTTONS]
            if session.voting_active:
                active_players = [p for p in session.players.values() if p.active]
                return self._build_vote_buttons(active_players)
            return [list(row) for row in ACTIVE_PANEL_BUTTONS]
        return [list(row) for row in ACTIVE_PANEL_BUTTONS]

    def _panel_update(self, session: GameSession, text: str) -> Notification:
        """Build an in-place panel edit that always carries a live keyboard."""
        return Notification(
            channel="group",
            target_id=session.group_chat_id,
            text=text,
            buttons=self._panel_buttons(session),
            edit_message_id=session.control_message_id,
        )

    @staticmethod
    def _vote_progress_text(session: GameSession, active_players: list[Player]) -> str:
        """Render the ballot with who has already voted and who is pending."""
        voted = [
            session.players[uid].display_name
            for uid in session.votes
            if uid in session.players
        ]
        pending = [p.display_name for p in active_players if p.user_id not in session.votes]
        lines = [
            "🗳️ <b>التصويت على الجاسوس جارٍ!</b>",
            "",
            f"📊 الأصوات: <b>{len(session.votes)}/{len(active_players)}</b>",
        ]
        if voted:
            lines.append("✅ صوّتوا: " + "، ".join(voted))
        if pending:
            lines.append("⏳ بانتظار: " + "، ".join(pending))
        lines.append("")
        lines.append("اضغط على اسم اللاعب الذي تشك أنه الجاسوس أدناه:")
        return "\n".join(lines)

    def _build_vote_buttons(self, active_players: list[Player]) -> list[list[dict[str, str]]]:
        buttons: list[list[dict[str, str]]] = []
        row: list[dict[str, str]] = []
        for player in active_players:
            row.append({"text": f"👤 {player.display_name}", "callback_data": f"vote:{player.user_id}"})
            if len(row) == 2:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)
        return buttons

    def _scrub_terminal(self, session: GameSession, terminal_state: GameState) -> None:
        """Mark a session terminal and erase every round secret it still holds."""
        session.state = terminal_state
        session.voting_active = False
        session.spy_guessing_active = False
        session.votes.clear()
        session.eligible_vote_targets = []
        session.spy_guess_options = []
        session.spy_guess_labels = []
        session.secret_location_word = None
        session.secret_location_name = None
        session.secret_category = ""
        for player in session.players.values():
            player.is_spy = False
            player.secret_word = None
