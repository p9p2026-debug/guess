"""Single transaction/effect boundary for every state-bearing event.

The executor is deliberately transport-agnostic: a synchronous decision runs
under the exact group's store transaction, then effects run only after that
transaction has released its lock.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass

from .effects import EffectOutcome, EffectRunner
from .models import SessionKey, TransitionResult
from .session_store import SessionStore, StaleGenerationError, StoreView

Decision = Callable[[StoreView], TransitionResult]


@dataclass(frozen=True, slots=True)
class EventExecution:
    """Detached result of one atomic decision and its external effects."""

    transition: TransitionResult
    effect_outcomes: tuple[EffectOutcome, ...] = ()

    @property
    def effect_failed(self) -> bool:
        return any(not outcome.ok for outcome in self.effect_outcomes)


class EventExecutor:
    """Apply group, callback, DM, and timer events through one pipeline."""

    def __init__(
        self,
        store: SessionStore,
        effect_runner: EffectRunner,
        *,
        is_accepting: Callable[[], bool] | None = None,
    ) -> None:
        self._store = store
        self._effect_runner = effect_runner
        self._is_accepting = is_accepting or (lambda: True)

    @staticmethod
    def stale(key: SessionKey, reason: str = "stale_generation") -> TransitionResult:
        return TransitionResult(False, reason, key, None)
    async def decide(
        self,
        group_chat_id: int,
        decision: Decision,
        *,
        session_key: SessionKey | None = None,
        idempotency_key: Hashable | None = None,
        allow_during_shutdown: bool = False,
    ) -> TransitionResult:
        """Commit one synchronous decision with generation/idempotency guards."""
        if session_key is not None and session_key.group_chat_id != group_chat_id:
            raise ValueError("session key belongs to another group")
        if not self._is_accepting() and not allow_during_shutdown:
            return TransitionResult(
                False,
                "shutting_down",
                session_key or SessionKey(group_chat_id, 0),
                None,
            )

        def transact(view: StoreView) -> TransitionResult:
            current = view.get()
            guarded_key = session_key or (
                current.session_key if current is not None else None
            )

            def apply() -> TransitionResult:
                if session_key is not None and view.get_for_key(session_key) is None:
                    return self.stale(session_key)
                result = decision(view)
                if not isinstance(result, TransitionResult):
                    raise TypeError("event decision must return TransitionResult")
                if (
                    result.session_key is not None
                    and result.session_key.group_chat_id != group_chat_id
                ):
                    raise ValueError("event decision returned a cross-group session key")
                return result

            if idempotency_key is not None and guarded_key is not None:
                try:
                    return view.apply_update_once(
                        guarded_key, idempotency_key, apply
                    ).outcome
                except StaleGenerationError:
                    return self.stale(guarded_key)

            result = apply()
            if idempotency_key is not None and result.session_key is not None:
                try:
                    return view.commit_update(
                        result.session_key, idempotency_key, result
                    ).outcome
                except StaleGenerationError:
                    # A terminal decision may remove its generation before the
                    # bounded replay outcome is recorded by apply_update_once.
                    return result
            return result

        return await self._store.transact(group_chat_id, transact)

    async def dispatch(self, transition: TransitionResult) -> EventExecution:
        """Run already-committed effects; no store lock is acquired here."""
        outcomes = (
            await self._effect_runner.run(transition.effects)
            if transition.effects
            else ()
        )
        return EventExecution(transition, outcomes)

    async def execute(
        self,
        group_chat_id: int,
        decision: Decision,
        *,
        session_key: SessionKey | None = None,
        idempotency_key: Hashable | None = None,
    ) -> EventExecution:
        transition = await self.decide(
            group_chat_id,
            decision,
            session_key=session_key,
            idempotency_key=idempotency_key,
        )
        return await self.dispatch(transition)
