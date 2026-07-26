"""Session_Manager: creates, transitions, and terminates Game_Sessions.

Implements the ``SessionManager`` component described in the design
document's "Components and Interfaces" section. All methods are
synchronous and side-effect-free beyond mutating the injected
``SessionStore``; I/O (actually sending the returned notifications) is
the Telegram Adapter's responsibility.
"""

from __future__ import annotations

import time

from .guess_tracker import GuessTracker
from .models import GameSession, GameState, Notification, OperationResult, Player
from .photo_distributor import PhotoDistributor
from .score_tracker import ScoreTracker
from .session_store import SessionStore
from .timer_service import TimerService

_ACTIVE_STATES = (GameState.LOBBY, GameState.GUESSING, GameState.REVEAL)


_LOBBY_BUTTONS: list[list[dict[str, str]]] = [
    [
        {"text": "➕ انضمام للعبة", "callback_data": "join_game"},
        {"text": "🚪 مغادرة اللعبة", "callback_data": "leave_game"},
    ],
    [
        {"text": "🚀 بدء اللعبة", "callback_data": "start_game"},
        {"text": "❌ إلغاء اللعبة", "callback_data": "cancel_game"},
    ],
]


class SessionManager:
    """Creates, joins, starts, cancels, and transitions Game_Sessions."""

    def __init__(
        self,
        store: SessionStore,
        score_tracker: ScoreTracker | None = None,
        photo_distributor: PhotoDistributor | None = None,
        guess_tracker: GuessTracker | None = None,
        timer_service: TimerService | None = None,
        on_notification_cb: Callable[[list[Notification]], None] | None = None,
    ) -> None:
        self._store = store
        self._score_tracker = (
            score_tracker if score_tracker is not None else ScoreTracker()
        )
        self._photo_distributor = (
            photo_distributor
            if photo_distributor is not None
            else PhotoDistributor(store)
        )
        self._guess_tracker = (
            guess_tracker if guess_tracker is not None else GuessTracker()
        )
        self._timer_service = timer_service
        self._on_notification_cb = on_notification_cb

    def create_session(
        self, group_chat_id: int, host_id: int, host_name: str
    ) -> OperationResult:
        existing = self._store.get(group_chat_id)
        if existing is not None and existing.state in _ACTIVE_STATES:
            return OperationResult(
                ok=False, reason="session_already_active", session=existing
            )

        host = Player(user_id=host_id, display_name=host_name)
        session = GameSession(
            group_chat_id=group_chat_id,
            host_id=host_id,
            state=GameState.LOBBY,
            players={host_id: host},
            created_at=time.time(),
        )
        self._store.put(session)

        announcement = Notification(
            channel="group",
            target_id=group_chat_id,
            text=(
                f"🎮 <b>{host_name}</b> بدأ لعبة تخمين صور جديدة! (Hedbanz / Heads Up)\n\n"
                "<blockquote expandable>\n"
                "<b>💡 فكرة اللعبة:</b>\n"
                "يرسل كل لاعب صورته الخاصة للبوت في السر، ثم يوزع البوت الصور بأسماء مستعارة (مثل <code>Photo A</code>). "
                "هدفكم هو تخمين صاحب كل صورة بالضغط على الأزرار وكسب النقاط!\n"
                "</blockquote>\n\n"
                "<b>📋 طريقة اللعب:</b>\n"
                "1️⃣ اضغط <b>➕ انضمام للعبة</b> أدناه.\n"
                "2️⃣ ارسل صورتك بالخاص للبوت <b>@guessJobot</b>.\n"
                "3️⃣ اضغط <b>🚀 بدء اللعبة</b> لتوزيع الصور والتخمين!\n\n"
                f"👥 <b>اللوبي مفتوح الآن:</b> (1/{session.max_players} لاعبين)"
            ),
            buttons=_LOBBY_BUTTONS,
        )
        return OperationResult(ok=True, notifications=[announcement], session=session)

    def join_session(
        self, group_chat_id: int, user_id: int, display_name: str
    ) -> OperationResult:
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.LOBBY:
            return OperationResult(ok=False, reason="not_joinable", session=session)

        if user_id in session.players:
            return OperationResult(
                ok=False, reason="already_member", session=session
            )

        if len(session.players) >= session.max_players:
            return OperationResult(ok=False, reason="lobby_full", session=session)

        session.players[user_id] = Player(user_id=user_id, display_name=display_name)
        self._store.put(session)

        count_notification = Notification(
            channel="group",
            target_id=group_chat_id,
            text=(
                f"👤 <b>{display_name}</b> انضم للعبة! "
                f"({len(session.players)}/{session.max_players} لاعبين)"
            ),
            buttons=_LOBBY_BUTTONS,
        )
        return OperationResult(
            ok=True, notifications=[count_notification], session=session
        )

    def leave_session(self, group_chat_id: int, user_id: int) -> OperationResult:
        """Remove or deactivate ``user_id`` in the Game_Session for ``group_chat_id``.

        - Lobby state: remove the Player and discard their Photo_Submission.
          If Host leaves and others remain, transfer Host (Req 10.2). If Host
          leaves alone, cancel the session (Req 10.3).
        - Guessing state: mark Player inactive (Req 10.4), discard their
          Guesses (Req 10.6), and cancel the session if active Players fall
          below Minimum_Players (Req 10.7).

        Requirements: 10.1, 10.2, 10.3, 10.4, 10.6, 10.7
        """
        session = self._store.get(group_chat_id)
        if session is None or user_id not in session.players:
            return OperationResult(ok=False, reason="not_member", session=session)

        if session.state == GameState.LOBBY:
            departing = session.players.pop(user_id)

            if not session.players:
                session.labels.clear()
                session.guesses.clear()
                session.state = GameState.CANCELLED
                self._store.put(session)
                notification = Notification(
                    channel="group",
                    target_id=group_chat_id,
                    text=(
                        f"🚪 <b>{departing.display_name}</b> غادر اللعبة ولم يبقَ أي لاعب — "
                        "تم إلغاء اللعبة."
                    ),
                )
                return OperationResult(
                    ok=True, notifications=[notification], session=session
                )

            if departing.user_id == session.host_id:
                new_host = next(iter(session.players.values()))
                session.host_id = new_host.user_id
                notification = Notification(
                    channel="group",
                    target_id=group_chat_id,
                    text=(
                        f"🚪 {departing.display_name} غادر اللعبة.\n"
                        f"أصبح <b>{new_host.display_name}</b> منشئ اللعبة (Host) الآن."
                    ),
                    buttons=_LOBBY_BUTTONS,
                )
            else:
                notification = Notification(
                    channel="group",
                    target_id=group_chat_id,
                    text=(
                        f"🚪 {departing.display_name} غادر اللعبة. "
                        f"({len(session.players)}/{session.max_players} لاعبين)"
                    ),
                    buttons=_LOBBY_BUTTONS,
                )

            self._store.put(session)
            return OperationResult(
                ok=True, notifications=[notification], session=session
            )

        if session.state == GameState.GUESSING:
            player = session.players[user_id]
            if not player.active:
                return OperationResult(
                    ok=False, reason="already_inactive", session=session
                )

            player.active = False
            self._guess_tracker.discard_player_guesses(session, user_id)

            active_players = [p for p in session.players.values() if p.active]
            if len(active_players) < session.min_players:
                if self._timer_service is not None:
                    self._timer_service.cancel(group_chat_id)
                for p in session.players.values():
                    p.photo_file_id = None
                session.labels.clear()
                session.guesses.clear()
                session.state = GameState.CANCELLED
                self._store.put(session)

                notification = Notification(
                    channel="group",
                    target_id=group_chat_id,
                    text=(
                        f"🚪 {player.display_name} غادر اللعبة. قل عدد اللاعبين النشطين عن "
                        f"الحد الأدنى ({session.min_players}) — تم إلغاء اللعبة."
                    ),
                )
                return OperationResult(
                    ok=True, notifications=[notification], session=session
                )

            self._store.put(session)
            notification = Notification(
                channel="group",
                target_id=group_chat_id,
                text=f"🚪 {player.display_name} غادر اللعبة.",
            )
            return OperationResult(
                ok=True, notifications=[notification], session=session
            )

        return OperationResult(
            ok=False, reason="leave_not_applicable", session=session
        )

    def start_session(
        self, group_chat_id: int, requester_id: int
    ) -> OperationResult:
        """Start the Guessing state for the Lobby-state Game_Session.

        Enforces Host-only (Req 4.4), Minimum_Players met (Req 4.2), and
        every Player has a Photo_Submission recorded (Req 4.3).

        On success:
        - Transitions session state to GameState.GUESSING (Req 4.1).
        - Calls PhotoDistributor.build_distribution (Req 5.1-5.4).
        - Starts TimerService countdown with configured or default duration (Req 7.1, 7.4-7.6).
        - If duration is zero, transitions immediately through Reveal to Completed (Req 7.7).

        Requirements: 4.1, 4.2, 4.3, 4.4, 7.1, 7.4, 7.5, 7.6, 7.7
        """
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.LOBBY:
            return OperationResult(ok=False, reason="not_in_lobby", session=session)

        if requester_id != session.host_id:
            return OperationResult(
                ok=False,
                reason="not_host",
                notifications=[
                    Notification(
                        channel="group",
                        target_id=group_chat_id,
                        text="عذراً، فقط منشئ اللعبة (Host) يمكنه بدء اللعبة.",
                    )
                ],
                session=session,
            )

        if len(session.players) < session.min_players:
            return OperationResult(
                ok=False,
                reason="below_minimum",
                notifications=[
                    Notification(
                        channel="group",
                        target_id=group_chat_id,
                        text=(
                            f"لا يمكن البدء: يجب توفر {session.min_players} لاعبين على الأقل "
                            f"(الحالي: {len(session.players)})."
                        ),
                    )
                ],
                session=session,
            )

        missing = [
            p for p in session.players.values() if p.photo_file_id is None
        ]
        if missing:
            missing_names = [p.display_name for p in missing]
            return OperationResult(
                ok=False,
                reason="missing_photos",
                notifications=[
                    Notification(
                        channel="group",
                        target_id=group_chat_id,
                        text=(
                            "لا يمكن البدء: اللاعبون التاليون لم يرسلوا صورهم بعد: "
                            + ", ".join(missing_names)
                        ),
                    )
                ],
                session=session,
            )

        # Transition to GUESSING
        session.state = GameState.GUESSING

        # Distribute photos / assign labels
        dist_res = self._photo_distributor.build_distribution(session)

        duration = session.guessing_timeout_seconds
        announcement = Notification(
            channel="group",
            target_id=group_chat_id,
            text=(
                f"⏱️ <b>بدأت مرحلة التخمين!</b> لديك {duration} ثانية لتخمين أصحاب الصور."
            ),
        )

        all_notifications = [announcement] + dist_res.notifications

        if self._timer_service is not None:
            self._timer_service.start(
                group_chat_id,
                duration,
                on_half_elapsed=lambda gid: self._on_half_elapsed(gid),
                on_expired=lambda gid: self._on_expired(gid),
            )

        if duration == 0:
            reveal_res = self.enter_reveal(group_chat_id)
            if reveal_res.ok:
                all_notifications.extend(reveal_res.notifications)
                session = reveal_res.session
            else:
                self._store.put(session)
        else:
            self._store.put(session)

        return OperationResult(
            ok=True, notifications=all_notifications, session=session
        )

    def _on_half_elapsed(self, group_chat_id: int) -> OperationResult:
        """Internal callback when half of Guessing_Timeout has elapsed."""
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.GUESSING:
            return OperationResult(ok=False, reason="not_in_guessing", session=session)

        notification = Notification(
            channel="group",
            target_id=group_chat_id,
            text="⏰ <b>تذكير نصف الوقت!</b> مضى نصف وقت التخمين.",
        )
        res = OperationResult(ok=True, notifications=[notification], session=session)
        if self._on_notification_cb is not None:
            self._on_notification_cb(res.notifications)
        return res

    def _on_expired(self, group_chat_id: int) -> OperationResult:
        """Internal callback when Guessing_Timeout has expired."""
        res = self.enter_reveal(group_chat_id)
        if res.ok and self._on_notification_cb is not None:
            self._on_notification_cb(res.notifications)
        return res


    def cancel_session(self, group_chat_id: int, user_id: int) -> OperationResult:
        """Cancel the non-terminal Game_Session for ``group_chat_id``.

        Only the Host may cancel. On success the session transitions to
        the Cancelled state, every Photo_Submission is discarded (each
        Player's ``photo_file_id`` cleared), every Label and every Guess
        is dropped from the session, and a Group_Chat notification is
        returned announcing the cancellation (Req 12.1, 12.3).

        Rejects the request without mutating any session state when:
        - no session exists for ``group_chat_id``
          (``reason="no_session"``);
        - the session is already in a terminal state
          (Completed/Cancelled) (``reason="already_terminal"``);
        - the requester is not the current Host
          (``reason="not_host"``, Req 12.2).

        Requirements: 12.1, 12.2, 12.3
        """
        session = self._store.get(group_chat_id)
        if session is None:
            return OperationResult(ok=False, reason="no_session", session=None)

        if session.state not in _ACTIVE_STATES:
            return OperationResult(
                ok=False, reason="already_terminal", session=session
            )

        if user_id != session.host_id:
            return OperationResult(ok=False, reason="not_host", session=session)

        if self._timer_service is not None:
            self._timer_service.cancel(group_chat_id)

        host_name = session.players[session.host_id].display_name

        # Discard every Photo_Submission, Label, and Guess (Req 12.3).
        for player in session.players.values():
            player.photo_file_id = None
        session.labels.clear()
        session.guesses.clear()

        session.state = GameState.CANCELLED
        self._store.put(session)

        notification = Notification(
            channel="group",
            target_id=group_chat_id,
            text=f"❌ قام <b>{host_name}</b> بإلغاء اللعبة.",
        )
        return OperationResult(
            ok=True, notifications=[notification], session=session
        )


    def set_guessing_timeout(
        self, group_chat_id: int, requester_id: int, seconds: int
    ) -> OperationResult:
        """Configure a custom Guessing_Timeout on a Lobby-state Game_Session.

        Records ``seconds`` as ``session.guessing_timeout_seconds`` so that
        the subsequent Start_Command uses this value instead of the
        5-minute default (Req 7.5). If no custom value is ever set, the
        default remains in effect (Req 7.6). Zero seconds is accepted so
        that the Host can request an immediate Reveal (Req 7.7); the
        immediate transition itself is applied by ``start_session``.

        Rejects the request without mutation when:
        - no session exists for ``group_chat_id`` or it is not in the
          Lobby state (``reason="not_in_lobby"``);
        - ``requester_id`` is not the current Host
          (``reason="not_host"``);
        - ``seconds`` is negative (``reason="invalid_timeout"``).

        Requirements: 7.5, 7.6
        """
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.LOBBY:
            return OperationResult(ok=False, reason="not_in_lobby", session=session)

        if requester_id != session.host_id:
            return OperationResult(ok=False, reason="not_host", session=session)

        if seconds < 0:
            return OperationResult(
                ok=False, reason="invalid_timeout", session=session
            )

        session.guessing_timeout_seconds = seconds
        self._store.put(session)

        notification = Notification(
            channel="group",
            target_id=group_chat_id,
            text=f"⚙️ تم ضبط وقت التخمين إلى <b>{seconds}</b> ثانية للجولة القادمة.",
        )
        return OperationResult(ok=True, notifications=[notification], session=session)

    def enter_reveal(self, group_chat_id: int) -> OperationResult:
        """Transition a Guessing-state session through Reveal to Completed.

        Invoked by the Timer_Service when the Guessing_Timeout elapses, or
        synchronously by ``start_session`` when a 0-length round is
        configured (Req 7.7). Computes final Scores via ``ScoreTracker``,
        produces the Group_Chat disclosure notification naming the true
        submitter of every Label (Req 9.1) and the descending ranking /
        winners notification (Req 9.2, 9.3), and unconditionally transitions
        the session to the Completed state regardless of whether the adapter
        subsequently succeeds in delivering the notifications (Req 9.4).

        The transient Reveal state (see the design's state machine) is
        entered and then immediately superseded by Completed within this
        single non-yielding call, so no external observer waits on Reveal.

        Rejects the request without mutating any session state when no
        session exists for ``group_chat_id`` or the session is not in the
        Guessing state (``reason="not_in_guessing"``). This is a defensive
        guard against a stale Timer_Service callback firing after the
        session has already cancelled or completed, per the design's timer
        race handling.

        Requirements: 9.1, 9.2, 9.3, 9.4
        """
        session = self._store.get(group_chat_id)
        if session is None or session.state != GameState.GUESSING:
            return OperationResult(
                ok=False, reason="not_in_guessing", session=session
            )

        # Enter the transient Reveal processing state, matching the design's
        # state machine. This state is not externally observable because we
        # transition to Completed unconditionally later in this same call.
        session.state = GameState.REVEAL

        scores = self._score_tracker.compute_scores(session)

        disclosure = self._build_disclosure_notification(session)
        ranking = self._build_ranking_notification(session, scores)

        # Req 9.4: transition to Completed unconditionally, independent of
        # whether the adapter later succeeds in delivering the notifications.
        # Delivery happens outside this function and its outcome cannot
        # affect the transition already applied here.
        session.state = GameState.COMPLETED
        self._store.put(session)

        return OperationResult(
            ok=True,
            notifications=[disclosure, ranking],
            session=session,
        )

    def _build_disclosure_notification(self, session: GameSession) -> Notification:
        """Build the Group_Chat notification disclosing each Label's submitter.

        Names, for every Label that exists in the session, the Player who
        actually submitted the corresponding Photo_Submission (Req 9.1).
        Labels are listed in sorted order for a deterministic message body.
        """
        lines = ["🎭 <b>انتهى الوقت! إليك أصحاب الصور الحقيقيين:</b>"]
        for label in sorted(session.labels):
            submitter_id = session.labels[label]
            submitter = session.players.get(submitter_id)
            name = (
                submitter.display_name if submitter is not None else f"user {submitter_id}"
            )
            label_text = label if label.startswith("Photo ") else f"Photo {label}"
            lines.append(f"  📸 {label_text}: <b>{name}</b>")
        return Notification(
            channel="group",
            target_id=session.group_chat_id,
            text="\n".join(lines),
        )

    def _build_ranking_notification(
        self, session: GameSession, scores: dict[int, int]
    ) -> Notification:
        """Build the Group_Chat notification listing Scores and winners.

        Lists every scored (active) Player in non-increasing Score order
        (Req 9.2). Ties are broken by join order so the message is
        deterministic without altering the ranking itself. Every Player
        whose Score equals the session's maximum Score is announced as a
        winner, including all of them when multiple Players tie at the top
        (Req 9.3).
        """
        join_order = {user_id: index for index, user_id in enumerate(session.players)}
        ranked = sorted(
            scores.items(),
            key=lambda entry: (-entry[1], join_order.get(entry[0], 0)),
        )

        lines = ["🏆 <b>الترتيب والنتائج النهائية:</b>"]
        for user_id, score in ranked:
            player = session.players.get(user_id)
            name = player.display_name if player is not None else f"user {user_id}"
            lines.append(f"  👤 {name}: <b>{score}</b> نقطة")

        if scores:
            max_score = max(scores.values())
            winner_ids = sorted(
                (uid for uid, s in scores.items() if s == max_score),
                key=lambda uid: join_order.get(uid, 0),
            )
            winner_names = [
                session.players[uid].display_name
                if uid in session.players
                else f"user {uid}"
                for uid in winner_ids
            ]
            if len(winner_names) == 1:
                lines.append("")
                lines.append(f"🎉 <b>الفائز باللعبة: {winner_names[0]}!</b>")
            else:
                lines.append("")
                lines.append(
                    f"🎉 <b>الفائزون بالمركز الأول (تعادل بـ {max_score} نقاط): "
                    + ", ".join(winner_names)
                    + "!</b>"
                )

        return Notification(
            channel="group",
            target_id=session.group_chat_id,
            text="\n".join(lines),
        )
