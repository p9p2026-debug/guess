"""Photo_Distributor: collects, stores, and delivers Photo_Submissions.

Implements the ``PhotoDistributor`` component described in the design
document's "Components and Interfaces" section. All methods are
synchronous and side-effect-free beyond mutating the injected
``SessionStore`` (and a short-lived internal pending-disambiguation
cache); I/O (actually sending the returned notifications) is the Telegram
Adapter's responsibility.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from .models import GameSession, GameState, OperationResult, Notification
from .session_store import SessionStore


def _label_for_index(index: int) -> str:
    """Return the Label assigned to the Player at ``index`` in join order.

    Uses a fixed, join-order-based scheme (``"Photo A"``, ``"Photo B"``,
    ...) so the Label a given Photo_Submission receives depends only on
    the Player's position in the session's join order, not on the
    recipient or any other identity-bearing data (Req 5.4). Extends
    naturally past 26 Players (``"Photo AA"``, ``"Photo AB"``, ...) even
    though ``Maximum_Players`` defaults to 15.
    """
    letters = ""
    n = index
    while True:
        letters = chr(ord("A") + (n % 26)) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return f"Photo {letters}"


@dataclass
class DisambiguationRequired(OperationResult):
    """An ``OperationResult`` subtype returned when a photo needs a choice.

    Carries the candidate ``group_chat_id``s (sorted) alongside the usual
    ``notifications`` prompt so the Telegram Adapter can render selectable
    options for the Player rather than parse them out of the prompt text
    (Req 11.3). ``resolve_disambiguated_submission`` validates the Player's
    reply against exactly these candidates.
    """

    candidate_group_chat_ids: tuple[int, ...] = ()


@dataclass
class _PendingSubmission:
    """A photo held while the Player resolves which session it belongs to.

    Mirrors the design's "Pending photo (disambiguation)" data model:
    ``{user_id: (file_id, candidate_group_chat_ids, received_at)}``. Held
    only while a Req 11.3 disambiguation is outstanding.
    """

    file_id: str
    candidate_group_chat_ids: frozenset[int]
    received_at: float


class PhotoDistributor:
    """Stores Photo_Submissions and builds per-Player distribution payloads."""

    def __init__(self, store: SessionStore) -> None:
        self._store = store
        # Short-lived cache of photos awaiting a disambiguation reply,
        # keyed by user_id (Req 11.3). Entries are cleared once resolved.
        self._pending: dict[int, _PendingSubmission] = {}

    def submit_photo(self, user_id: int, file_id: str) -> OperationResult:
        """Store ``file_id`` as ``user_id``'s Photo_Submission.

        Looks up every Lobby-state Game_Session in which ``user_id`` is
        currently a Player, via the store's cross-group membership index:

        - Zero matches: reject with ``reason="no_open_session"``
          (Req 3.2), without mutating any session.
        - Exactly one match: store/replace the Photo_Submission on that
          session's Player and return a Direct_Message confirmation
          (Req 3.1, 3.3, 3.4). Only currently-Lobby sessions are
          considered, so a Game_Session the Player was previously part
          of that has since ended is ignored (Req 11.4).
        - More than one match: cache the pending ``file_id`` together with
          the candidate ``group_chat_id``s and ask the Player to pick one
          (Req 11.3). The photo is held (not stored, not discarded) until
          :meth:`resolve_disambiguated_submission` completes the store.

        Requirements: 3.1, 3.2, 3.3, 3.4, 11.3, 11.4
        """
        candidate_group_chat_ids = [
            group_chat_id
            for group_chat_id in self._store.group_chat_ids_for_user(user_id)
            if (session := self._store.get(group_chat_id)) is not None
            and session.state == GameState.LOBBY
        ]

        if not candidate_group_chat_ids:
            return OperationResult(ok=False, reason="no_open_session")

        if len(candidate_group_chat_ids) > 1:
            # Multiple current Lobby memberships: the Player must pick one
            # (Req 11.3). Cache the pending photo so it is neither stored
            # against an arbitrary session nor discarded, and prompt the
            # Player to choose via Direct_Message. The candidate list is
            # also returned in a structured form so the adapter can render
            # selectable options rather than parse the prompt text.
            sorted_candidates = tuple(sorted(candidate_group_chat_ids))
            self._pending[user_id] = _PendingSubmission(
                file_id=file_id,
                candidate_group_chat_ids=frozenset(candidate_group_chat_ids),
                received_at=time.monotonic(),
            )
            prompt_buttons: list[list[dict[str, str]]] = [
                [
                    {
                        "text": f"💬 المجموعة {gid}",
                        "callback_data": f"disambiguate:{gid}",
                    }
                ]
                for gid in sorted_candidates
            ]
            prompt = Notification(
                channel="dm",
                target_id=user_id,
                text=(
                    "أنت مشارك في أكثر من لعبة مفتوحة بنفس الوقت! "
                    "لأي مجموعة تتبع هذه الصورة؟ اضغط على اسم المجموعة أدناه:"
                ),
                buttons=prompt_buttons,
            )
            return DisambiguationRequired(
                ok=False,
                reason="disambiguation_required",
                notifications=[prompt],
                candidate_group_chat_ids=sorted_candidates,
            )

        # Exactly one Lobby-state membership: store/replace directly.
        group_chat_id = candidate_group_chat_ids[0]
        return self._store_submission(user_id, group_chat_id, file_id)

    def resolve_disambiguated_submission(
        self, user_id: int, chosen_group_chat_id: int
    ) -> OperationResult:
        """Complete a disambiguated Photo_Submission once the Player chooses."""
        pending = self._pending.get(user_id)
        if pending is None:
            return OperationResult(ok=False, reason="no_pending_submission")

        session = self._store.get(chosen_group_chat_id)
        is_current_lobby_member = (
            session is not None
            and session.state == GameState.LOBBY
            and user_id in session.players
        )
        if (
            chosen_group_chat_id not in pending.candidate_group_chat_ids
            or not is_current_lobby_member
        ):
            return OperationResult(ok=False, reason="invalid_choice")

        result = self._store_submission(user_id, chosen_group_chat_id, pending.file_id)
        del self._pending[user_id]
        return result

    def build_distribution(self, session: GameSession) -> OperationResult:
        """Assign Labels and build the per-recipient distribution DMs."""
        # (1) Assign Labels by join order and write session.labels.
        label_by_user: dict[int, str] = {}
        labels: dict[str, int] = {}
        for index, user_id in enumerate(session.players):
            label = _label_for_index(index)
            labels[label] = user_id
            label_by_user[user_id] = label
        session.labels = labels

        # (2) One DM per (recipient, other-player) pair, excluding self.
        notifications: list[Notification] = []
        for recipient_id in session.players:
            for other_id, other in session.players.items():
                if other_id == recipient_id:
                    continue
                label = label_by_user[other_id]

                # Create guess buttons for each candidate player
                guess_buttons: list[list[dict[str, str]]] = []
                row: list[dict[str, str]] = []
                for pid, p in session.players.items():
                    row.append(
                        {
                            "text": f"👤 {p.display_name}",
                            "callback_data": f"guess:{label}:{pid}",
                        }
                    )
                    if len(row) == 2:
                        guess_buttons.append(row)
                        row = []
                if row:
                    guess_buttons.append(row)

                notifications.append(
                    Notification(
                        channel="dm",
                        target_id=recipient_id,
                        text=(
                            f"📸 <b>الصورة {label}</b> — "
                            "من تعتقد أنه صاحب هذه الصورة؟ اضغط على اسم اللاعب أدناه لتسجيل تخمينك:"
                        ),
                        photo_file_id=other.photo_file_id,
                        buttons=guess_buttons,
                    )
                )

        self._store.put(session)
        return OperationResult(
            ok=True, notifications=notifications, session=session
        )


    def _store_submission(
        self, user_id: int, group_chat_id: int, file_id: str
    ) -> OperationResult:
        """Store/replace ``user_id``'s photo on ``group_chat_id`` and confirm."""
        session = self._store.get(group_chat_id)
        player = session.players[user_id]
        player.photo_file_id = file_id
        self._store.put(session)

        confirmation = Notification(
            channel="dm",
            target_id=user_id,
            text="📸 تم استلام صورتك وحفظها للعبة بنجاح! بالتوفيق!",
        )
        return OperationResult(ok=True, notifications=[confirmation], session=session)
