"""Photo_Distributor: collects, stores, and delivers Photo_Submissions.

Implements the ``PhotoDistributor`` component described in the design
document's "Components and Interfaces" section. All methods are
synchronous and side-effect-free beyond mutating the injected
``SessionStore`` (and a short-lived internal pending-disambiguation
cache); I/O (actually sending the returned notifications) is the Telegram
Adapter's responsibility.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from time import monotonic

from .callback_codec import encode_callback
from .models import (
    GameSession,
    GameState,
    Notification,
    OperationResult,
    RoundPhase,
    SessionKey,
)
from .session_store import SessionStore, StoreView


def _label_for_index(index: int) -> str:
    """Return the Label assigned to the Player at ``index`` in join order."""
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
    """A pending choice whose candidates retain exact generation identity."""

    candidate_group_chat_ids: tuple[int, ...] = ()
    candidate_session_keys: tuple[SessionKey, ...] = ()


@dataclass(frozen=True, slots=True)
class PendingDMContext:
    """One bounded-lifetime private payload awaiting an explicit group choice."""

    context_id: int
    user_id: int
    file_id: str
    candidate_session_keys: frozenset[SessionKey]
    expires_at: float


@dataclass(frozen=True, slots=True)
class PendingDMResolution:
    """Pure resolution result; it cannot mutate a session until committed."""

    ok: bool
    reason: str | None
    context_id: int | None = None
    user_id: int | None = None
    file_id: str | None = None
    session_key: SessionKey | None = None
    expires_at: float | None = None


@dataclass(frozen=True, slots=True)
class PreparedPhotoSubmission:
    """Pure DM routing decision; session mutation still requires StoreView."""

    result: OperationResult
    session_key: SessionKey | None = None
    user_id: int | None = None
    file_id: str | None = None


class PhotoDistributor:
    """Stores photos and owns generation-safe pending DM contexts."""

    def __init__(
        self,
        store: SessionStore,
        *,
        clock: Callable[[], float] = monotonic,
        pending_ttl_seconds: float = 300.0,
        cleanup_batch_limit: int = 100,
    ) -> None:
        if pending_ttl_seconds <= 0:
            raise ValueError("pending_ttl_seconds must be positive")
        if cleanup_batch_limit <= 0:
            raise ValueError("cleanup_batch_limit must be positive")
        self._store = store
        self._clock = clock
        self._pending_ttl_seconds = pending_ttl_seconds
        self._cleanup_batch_limit = cleanup_batch_limit
        self._pending: dict[int, PendingDMContext] = {}
        self._next_context_id = 1

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def pending_context_for_user(self, user_id: int) -> PendingDMContext | None:
        """Return a lazily cleaned immutable context for diagnostics/adapters."""
        return self._refresh_context(user_id)

    def cleanup_expired(self, limit: int | None = None) -> int:
        """Sweep at most the configured number of contexts in one call."""
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative")
        requested = self._cleanup_batch_limit if limit is None else limit
        budget = min(requested, self._cleanup_batch_limit)
        removed = 0
        for user_id in tuple(self._pending)[:budget]:
            existed = user_id in self._pending
            self._refresh_context(user_id)
            removed += int(existed and user_id not in self._pending)
        return removed

    def prepare_submission(
        self, user_id: int, file_id: str
    ) -> PreparedPhotoSubmission:
        """Resolve DM attribution without mutating a game session."""
        self._refresh_context(user_id)
        candidate_keys = tuple(
            sorted(
                (
                    key
                    for key in self._store.active_session_keys_for_user(user_id)
                    if self._is_valid_candidate(user_id, key)
                ),
                key=lambda key: (key.group_chat_id, key.generation),
            )
        )
        if not candidate_keys:
            return PreparedPhotoSubmission(
                OperationResult(ok=False, reason="no_open_session")
            )

        if len(candidate_keys) > 1:
            context = PendingDMContext(
                context_id=self._next_context_id,
                user_id=user_id,
                file_id=file_id,
                candidate_session_keys=frozenset(candidate_keys),
                expires_at=self._clock() + self._pending_ttl_seconds,
            )
            self._next_context_id += 1
            self._pending[user_id] = context
            prompt_buttons = [
                [
                    {
                        "text": f"💬 المجموعة {key.group_chat_id}",
                        "callback_data": encode_callback(
                            key.generation, context.context_id, "dm", index
                        ),
                    }
                ]
                for index, key in enumerate(candidate_keys)
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
            result = DisambiguationRequired(
                ok=False,
                reason="disambiguation_required",
                notifications=[prompt],
                candidate_group_chat_ids=tuple(
                    key.group_chat_id for key in candidate_keys
                ),
                candidate_session_keys=candidate_keys,
            )
            return PreparedPhotoSubmission(result, user_id=user_id, file_id=file_id)

        return PreparedPhotoSubmission(
            OperationResult(ok=True), candidate_keys[0], user_id, file_id
        )

    def submit_photo(self, user_id: int, file_id: str) -> OperationResult:
        """Compatibility API; application handlers use prepare/commit instead."""
        prepared = self.prepare_submission(user_id, file_id)
        if prepared.session_key is None:
            return prepared.result
        return self._store_submission(user_id, prepared.session_key, file_id)

    def commit_submission(
        self,
        view: StoreView,
        user_id: int,
        key: SessionKey,
        file_id: str,
    ) -> OperationResult:
        """Commit an unambiguous photo under the exact generation lock."""
        if view.group_chat_id != key.group_chat_id:
            return OperationResult(False, "cross_group_commit")
        session = view.get_for_key(key)
        player = session.players.get(user_id) if session is not None else None
        valid = (
            session is not None
            and not session.terminal
            and session.state == GameState.LOBBY
            and session.phase == RoundPhase.LOBBY
            and player is not None
            and player.active
            and key in self._store.active_session_keys_for_user(user_id)
        )
        if not valid:
            return OperationResult(False, "stale_generation")
        player.photo_file_id = file_id
        session.revision += 1
        view.put(session)
        return self._submission_confirmation(user_id, session)

    def resolve_disambiguated_submission(
        self, user_id: int, chosen_session: int | SessionKey
    ) -> PendingDMResolution:
        """Resolve an explicit choice without mutating any game session."""
        context = self._refresh_context(user_id)
        if context is None:
            return PendingDMResolution(False, "no_pending_submission")

        if isinstance(chosen_session, SessionKey):
            chosen_key = chosen_session
        else:
            matching = tuple(
                key
                for key in context.candidate_session_keys
                if key.group_chat_id == chosen_session
            )
            if len(matching) != 1:
                return PendingDMResolution(False, "invalid_choice")
            chosen_key = matching[0]

        if (
            chosen_key not in context.candidate_session_keys
            or not self._is_valid_candidate(user_id, chosen_key)
        ):
            self._remove_candidate(user_id, chosen_key)
            return PendingDMResolution(False, "invalid_choice")

        return PendingDMResolution(
            True,
            None,
            context.context_id,
            user_id,
            context.file_id,
            chosen_key,
            context.expires_at,
        )

    def commit_disambiguated_submission(
        self, view: StoreView, resolution: PendingDMResolution
    ) -> OperationResult:
        """Commit a resolved payload after authoritative transaction checks."""
        if not resolution.ok or resolution.session_key is None:
            return OperationResult(ok=False, reason=resolution.reason or "invalid_choice")
        if resolution.user_id is None or resolution.file_id is None:
            return OperationResult(ok=False, reason="invalid_choice")
        key = resolution.session_key
        if view.group_chat_id != key.group_chat_id:
            return OperationResult(ok=False, reason="cross_group_commit")

        context = self._pending.get(resolution.user_id)
        if context is None or context.context_id != resolution.context_id:
            return OperationResult(ok=False, reason="no_pending_submission")
        if context.expires_at <= self._clock():
            del self._pending[resolution.user_id]
            return OperationResult(ok=False, reason="pending_expired")
        if key not in context.candidate_session_keys:
            return OperationResult(ok=False, reason="invalid_choice")

        session = view.get_for_key(key)
        player = session.players.get(resolution.user_id) if session else None
        valid = (
            session is not None
            and not session.terminal
            and session.state == GameState.LOBBY
            and session.phase == RoundPhase.LOBBY
            and player is not None
            and player.active
            and key
            in self._store.active_session_keys_for_user(resolution.user_id)
        )
        if not valid:
            self._remove_candidate(resolution.user_id, key)
            return OperationResult(ok=False, reason="invalid_choice")

        player.photo_file_id = resolution.file_id
        session.revision += 1
        view.put(session)
        del self._pending[resolution.user_id]
        return self._submission_confirmation(resolution.user_id, session)

    def _is_valid_candidate(self, user_id: int, key: SessionKey) -> bool:
        session = self._store.get_for_key(key)
        player = session.players.get(user_id) if session else None
        return bool(
            session is not None
            and not session.terminal
            and session.state == GameState.LOBBY
            and session.phase == RoundPhase.LOBBY
            and player is not None
            and player.active
            and key in self._store.active_session_keys_for_user(user_id)
        )

    def _refresh_context(self, user_id: int) -> PendingDMContext | None:
        context = self._pending.get(user_id)
        if context is None:
            return None
        if context.expires_at <= self._clock():
            del self._pending[user_id]
            return None
        valid_keys = frozenset(
            key
            for key in context.candidate_session_keys
            if self._is_valid_candidate(user_id, key)
        )
        if not valid_keys:
            del self._pending[user_id]
            return None
        if valid_keys != context.candidate_session_keys:
            context = replace(context, candidate_session_keys=valid_keys)
            self._pending[user_id] = context
        return context

    def _remove_candidate(self, user_id: int, key: SessionKey) -> None:
        context = self._pending.get(user_id)
        if context is None or key not in context.candidate_session_keys:
            return
        remaining = context.candidate_session_keys - {key}
        if remaining:
            self._pending[user_id] = replace(
                context, candidate_session_keys=frozenset(remaining)
            )
        else:
            del self._pending[user_id]

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
        self, user_id: int, key: SessionKey, file_id: str
    ) -> OperationResult:
        """Preserve the synchronous single-membership submission path."""
        session = self._store.get_for_key(key)
        if session is None or not self._is_valid_candidate(user_id, key):
            return OperationResult(ok=False, reason="no_open_session")
        session.players[user_id].photo_file_id = file_id
        session.revision += 1
        self._store.put(session)
        return self._submission_confirmation(user_id, session)

    @staticmethod
    def _submission_confirmation(
        user_id: int, session: GameSession
    ) -> OperationResult:
        confirmation = Notification(
            channel="dm",
            target_id=user_id,
            text="📸 تم استلام صورتك وحفظها للعبة بنجاح! بالتوفيق!",
        )
        return OperationResult(ok=True, notifications=[confirmation], session=session)
