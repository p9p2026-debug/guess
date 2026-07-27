"""Post-commit effect execution and owned asyncio task lifecycle.

The runner is intentionally transport-agnostic: Telegram delivery, timers, and
other effect kinds are supplied through an injected executor.  State decisions
must finish before :meth:`EffectRunner.run` is awaited.

Requirements: 2.2, 2.3, 2.7, 2.15, 2.16, 3.9
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Protocol, TypeVar

from .models import Effect, SessionKey

_T = TypeVar("_T")
EffectExecutor = Callable[[Effect], Awaitable[object] | object]
SuccessClassifier = Callable[[object], bool]
TaskExceptionHandler = Callable[[SessionKey, asyncio.Task[object], BaseException], None]


class EffectLedger(Protocol):
    """Minimal persistent confirmed-outcome ledger used by ``EffectRunner``."""

    def processed_effect_outcome(self, effect_id: str) -> object | None: ...

    def commit_effect(self, effect_id: str, outcome: object) -> object: ...


@dataclass(frozen=True, slots=True)
class EffectOutcome:
    """Sanitized result of one effect attempt.

    ``value`` is excluded from repr because a transport result may carry a
    private role or message payload.  Exceptions are represented only by type
    to avoid retaining or logging secret-bearing exception messages.
    """

    effect_id: str
    session_key: SessionKey
    ok: bool
    value: object | None = field(default=None, repr=False)
    error_type: str | None = None
    cancelled: bool = False
    replayed: bool = False


@dataclass(frozen=True, slots=True)
class TaskFailure:
    """Non-secret diagnostic retained after a tracked task fails."""

    session_key: SessionKey
    task_name: str
    exception_type: str


class TaskRegistry:
    """Own every background task by exact session generation.

    Registry mutations contain no ``await`` and therefore remain atomic within
    one event loop.  Once an owner is drained it cannot acquire new tasks;
    attempts to register late completion work are cancelled immediately.
    """

    def __init__(
        self,
        *,
        on_exception: TaskExceptionHandler | None = None,
    ) -> None:
        self._tasks: dict[SessionKey, set[asyncio.Task[object]]] = defaultdict(set)
        self._closed_owners: set[SessionKey] = set()
        self._accepting = True
        self._on_exception = on_exception
        self._failures: list[TaskFailure] = []

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def pending_count(self) -> int:
        return sum(len(tasks) for tasks in self._tasks.values())

    def pending_for(self, key: SessionKey) -> int:
        return len(self._tasks.get(key, ()))

    @property
    def failures(self) -> tuple[TaskFailure, ...]:
        return tuple(self._failures)

    def create_task(
        self,
        key: SessionKey,
        awaitable: Awaitable[_T],
        *,
        name: str | None = None,
    ) -> asyncio.Task[_T]:
        """Create and synchronously register a task before yielding control."""
        task = asyncio.create_task(awaitable, name=name)
        self.track(key, task)
        return task

    def track(
        self, key: SessionKey, task: asyncio.Task[_T]
    ) -> asyncio.Task[_T]:
        """Register an existing task and install exception consumption."""
        untyped_task: asyncio.Task[object] = task  # type: ignore[assignment]
        if untyped_task in self._tasks.get(key, ()):
            return task
        self._tasks[key].add(untyped_task)
        untyped_task.add_done_callback(
            lambda completed, owner=key: self._task_done(owner, completed)
        )
        if not self._accepting or key in self._closed_owners:
            untyped_task.cancel()
        return task

    def _task_done(
        self, key: SessionKey, task: asyncio.Task[object]
    ) -> None:
        owned = self._tasks.get(key)
        if owned is not None:
            owned.discard(task)
            if not owned:
                self._tasks.pop(key, None)

        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return
        if exception is None:
            return
        self._failures.append(
            TaskFailure(key, task.get_name(), type(exception).__name__)
        )
        if self._on_exception is not None:
            try:
                self._on_exception(key, task, exception)
            except Exception:
                # A diagnostic callback must never create another orphan error.
                pass

    async def cancel_and_drain(self, key: SessionKey) -> None:
        """Cancel and await all tasks for ``key``; safe to call repeatedly."""
        self._closed_owners.add(key)
        while True:
            tasks = tuple(self._tasks.get(key, ()))
            if not tasks:
                return
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Run done callbacks before checking for tasks spawned during cleanup.
            await asyncio.sleep(0)

    async def shutdown(self) -> None:
        """Stop accepting work, then cancel and consume every tracked task."""
        self._accepting = False
        self._closed_owners.update(self._tasks)
        while self._tasks:
            tasks = tuple(
                task for owned in self._tasks.values() for task in owned
            )
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)


class EffectRunner:
    """Execute effects outside state locks with confirmed-send idempotency."""

    def __init__(
        self,
        executor: EffectExecutor | Mapping[str, EffectExecutor],
        *,
        registry: TaskRegistry | None = None,
        ledger: EffectLedger | None = None,
        is_success: SuccessClassifier | None = None,
    ) -> None:
        self._executor = executor
        self.registry = registry or TaskRegistry()
        self._ledger = ledger
        self._is_success = is_success or self._default_is_success
        self._outcomes: dict[str, EffectOutcome] = {}
        self._effects: dict[str, Effect] = {}
        self._inflight: dict[str, asyncio.Task[EffectOutcome]] = {}
        self._closed = False
        self._shutdown_task: asyncio.Task[None] | None = None

    @staticmethod
    def _default_is_success(value: object) -> bool:
        """Classify injected transport outcomes without depending on task 3.7."""
        if isinstance(value, bool):
            return value
        for attribute in ("delivered", "ok", "success"):
            candidate = getattr(value, attribute, None)
            if isinstance(candidate, bool):
                return candidate
        # Side-effect callables conventionally return None on successful await.
        return True

    @property
    def pending_count(self) -> int:
        return self.registry.pending_count

    def outcome_for(self, effect_id: str) -> EffectOutcome | None:
        """Return the latest local or persisted outcome for an effect id."""
        local = self._outcomes.get(effect_id)
        if local is not None:
            return local
        if self._ledger is None:
            return None
        persisted = self._ledger.processed_effect_outcome(effect_id)
        return persisted if isinstance(persisted, EffectOutcome) else None

    def _executor_for(self, effect: Effect) -> EffectExecutor:
        if callable(self._executor):
            return self._executor
        try:
            return self._executor[effect.kind]
        except KeyError as error:
            raise LookupError(f"no executor for effect kind {effect.kind!r}") from error

    def _validate_identity(self, effect: Effect) -> None:
        existing = self._effects.get(effect.effect_id)
        if existing is not None and existing != effect:
            raise ValueError("effect_id reused for a different effect")
        self._effects.setdefault(effect.effect_id, effect)

    def _confirmed_outcome(self, effect_id: str) -> EffectOutcome | None:
        outcome = self.outcome_for(effect_id)
        return outcome if outcome is not None and outcome.ok else None

    async def _execute(self, effect: Effect) -> EffectOutcome:
        try:
            result = self._executor_for(effect)(effect)
            if isinstance(result, Awaitable):
                result = await result
            outcome = EffectOutcome(
                effect.effect_id,
                effect.session_key,
                self._is_success(result),
                value=result,
            )
        except asyncio.CancelledError:
            outcome = EffectOutcome(
                effect.effect_id,
                effect.session_key,
                False,
                error_type="CancelledError",
                cancelled=True,
            )
        except Exception as error:
            outcome = EffectOutcome(
                effect.effect_id,
                effect.session_key,
                False,
                error_type=type(error).__name__,
            )

        self._outcomes[effect.effect_id] = outcome
        if outcome.ok and self._ledger is not None:
            committed = self._ledger.commit_effect(effect.effect_id, outcome)
            stored = getattr(committed, "outcome", outcome)
            duplicate = bool(getattr(committed, "duplicate", False))
            if isinstance(stored, EffectOutcome):
                outcome = replace(stored, replayed=duplicate)
                self._outcomes[effect.effect_id] = outcome
        return outcome

    def _remove_inflight(
        self, effect_id: str, task: asyncio.Task[EffectOutcome]
    ) -> None:
        if self._inflight.get(effect_id) is task:
            self._inflight.pop(effect_id, None)

    async def run(self, effects: Iterable[Effect]) -> tuple[EffectOutcome, ...]:
        """Run an immutable post-commit effect batch.

        Confirmed outcomes are replayed from the ledger. Failed outcomes remain
        observable but are attempted again on redelivery. Concurrent duplicate
        ids coalesce onto one owned task.
        """
        if self._closed or not self.registry.accepting:
            raise RuntimeError("effect runner is shut down")

        resolved: list[EffectOutcome | None] = []
        pending: list[tuple[int, asyncio.Task[EffectOutcome], bool]] = []
        seen_in_batch: set[str] = set()

        for effect in tuple(effects):
            self._validate_identity(effect)
            confirmed = self._confirmed_outcome(effect.effect_id)
            if confirmed is not None:
                resolved.append(replace(confirmed, replayed=True))
                continue

            task = self._inflight.get(effect.effect_id)
            coalesced = task is not None or effect.effect_id in seen_in_batch
            if task is None:
                task = self.registry.create_task(
                    effect.session_key,
                    self._execute(effect),
                    name=f"effect:{effect.effect_id}",
                )
                self._inflight[effect.effect_id] = task
                task.add_done_callback(
                    lambda done, effect_id=effect.effect_id: self._remove_inflight(
                        effect_id, done
                    )
                )
            index = len(resolved)
            resolved.append(None)
            pending.append((index, task, coalesced))
            seen_in_batch.add(effect.effect_id)

        if pending:
            outcomes = await asyncio.gather(*(item[1] for item in pending))
            for (index, _task, coalesced), outcome in zip(pending, outcomes):
                resolved[index] = replace(outcome, replayed=True) if coalesced else outcome

        return tuple(outcome for outcome in resolved if outcome is not None)

    async def cancel_and_drain(self, key: SessionKey) -> None:
        """Cancel one generation's effects outside all state transactions."""
        await self.registry.cancel_and_drain(key)

    async def shutdown(self) -> None:
        """Reject new effects, then cancel and consume every owned task.

        Concurrent callers await the same drain operation.  Cancellation of a
        caller does not abandon that drain; the cancellation is consumed only
        at this lifecycle boundary so no shutdown task becomes orphaned.
        """
        if self._shutdown_task is None:
            self._closed = True
            self._shutdown_task = asyncio.create_task(
                self.registry.shutdown(), name="effect-runner-shutdown"
            )
        try:
            await asyncio.shield(self._shutdown_task)
        except asyncio.CancelledError:
            await self._shutdown_task
