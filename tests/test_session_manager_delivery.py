"""Staged role-delivery tests for SessionManager task 3.9.

Property 5: Staged Delivery — readiness iff all frozen DMs are confirmed.
**Validates: Requirements 2.6, 2.7, 2.12, 3.3, 3.9**
"""

from __future__ import annotations

import asyncio

from hypothesis import given, settings, strategies as st

from photo_guess_game.models import (
    DeliveryStatus,
    GameState,
    Notification,
    RoundPhase,
)
from photo_guess_game.session_manager import (
    DeliveryEvent,
    RetryDeliveryEvent,
    SessionManager,
    StartEvent,
)
from photo_guess_game.session_store import SessionStore

LOCATION = {"name": "المستشفى", "word": "مستشفى"}


def transact(store, group_id, operation):
    return asyncio.run(store.transact(group_id, operation))


def make_delivery(player_count=3, *, max_attempts=3, group_id=9001):
    store = SessionStore()
    manager = SessionManager(
        store,
        location_selector=lambda: LOCATION,
        spy_selector=lambda roster: roster[0],
        max_delivery_attempts=max_attempts,
    )
    manager.create_session(group_id, 1, "P1")
    for user_id in range(2, player_count + 1):
        manager.join_session(group_id, user_id, f"P{user_id}")
    key = store.get(group_id).session_key
    result = transact(
        store, group_id, lambda view: manager.prepare_start(view, StartEvent(key, 1))
    )
    return store, manager, key, result


def record(store, manager, key, user_id, *, attempt=1, delivered=True, retryable=False):
    return transact(
        store,
        key.group_chat_id,
        lambda view: manager.record_delivery(
            view,
            DeliveryEvent(
                key,
                user_id,
                attempt,
                delivered,
                retryable=retryable,
                error_code=None if delivered else "transport_error",
            ),
        ),
    )


def assignment_fingerprint(session):
    return tuple(
        (user_id, entry.assignment.role, entry.assignment.rendered_text)
        for user_id, entry in sorted(session.delivery.items())
    )


def test_all_success_becomes_ready_once_and_schedules_once():
    store, manager, key, prepared = make_delivery()
    assert prepared.ok
    assert len(prepared.effects) == 3
    session = store.get(key.group_chat_id)
    assert session.state == GameState.REVEAL
    assert session.phase == RoundPhase.DELIVERING
    assert sum(player.is_spy for player in session.players.values()) == 1

    for user_id in (1, 2, 3):
        assert record(store, manager, key, user_id).ok

    ready = transact(
        store, key.group_chat_id, lambda view: manager.commit_ready(view, key)
    )
    assert ready.ok
    assert [effect.kind for effect in ready.effects] == ["telegram", "schedule_timer"]
    assert store.get(key.group_chat_id).phase == RoundPhase.DISCUSSION
    assert store.get(key.group_chat_id).state == GameState.GUESSING

    duplicate = transact(
        store, key.group_chat_id, lambda view: manager.commit_ready(view, key)
    )
    assert not duplicate.ok
    assert duplicate.reason == "already_ready"
    assert duplicate.effects == ()


def test_partial_permanent_failure_stays_unplayable_and_names_only_recipient():
    store, manager, key, _ = make_delivery(player_count=2)
    record(store, manager, key, 1)
    failed = record(store, manager, key, 2, delivered=False, retryable=False)

    assert not failed.ok
    assert failed.reason == DeliveryStatus.FAILED_PERMANENT.value
    assert len(failed.effects) == 1
    notice = failed.effects[0].payload
    assert isinstance(notice, Notification)
    assert "P2" in notice.text
    assert LOCATION["name"] not in notice.text
    assert "الجاسوس الوحيد" not in notice.text

    ready = transact(
        store, key.group_chat_id, lambda view: manager.commit_ready(view, key)
    )
    assert not ready.ok
    assert ready.reason == "delivery_incomplete"
    assert store.get(key.group_chat_id).phase == RoundPhase.DELIVERING
    assert store.get(key.group_chat_id).state != GameState.GUESSING


def test_retry_targets_only_retryable_recipient_and_preserves_assignment():
    store, manager, key, _ = make_delivery(player_count=3, max_attempts=2)
    before = assignment_fingerprint(store.get(key.group_chat_id))
    record(store, manager, key, 1)
    record(store, manager, key, 2, delivered=False, retryable=True)

    retried = transact(
        store,
        key.group_chat_id,
        lambda view: manager.retry_delivery(view, RetryDeliveryEvent(key, 1)),
    )
    assert retried.ok
    assert [effect.payload.target_id for effect in retried.effects] == [2]
    session = store.get(key.group_chat_id)
    assert session.delivery[1].status == DeliveryStatus.DELIVERED
    assert session.delivery[2].status == DeliveryStatus.IN_FLIGHT
    assert session.delivery[3].status == DeliveryStatus.IN_FLIGHT
    assert assignment_fingerprint(session) == before

    while_in_flight = transact(
        store,
        key.group_chat_id,
        lambda view: manager.retry_delivery(view, RetryDeliveryEvent(key, 1)),
    )
    assert not while_in_flight.ok
    assert while_in_flight.reason == "no_retryable_deliveries"

    exhausted = record(
        store, manager, key, 2, attempt=2, delivered=False, retryable=True
    )
    assert exhausted.reason == DeliveryStatus.FAILED_PERMANENT.value
    bounded = transact(
        store,
        key.group_chat_id,
        lambda view: manager.retry_delivery(view, RetryDeliveryEvent(key, 1)),
    )
    assert not bounded.ok
    assert bounded.reason == "no_retryable_deliveries"


def test_retry_is_host_only_without_mutation():
    store, manager, key, _ = make_delivery(player_count=2)
    record(store, manager, key, 2, delivered=False, retryable=True)
    session = store.get(key.group_chat_id)
    before = (session.revision, assignment_fingerprint(session))

    rejected = transact(
        store,
        key.group_chat_id,
        lambda view: manager.retry_delivery(view, RetryDeliveryEvent(key, 2)),
    )
    assert not rejected.ok
    assert rejected.reason == "not_host"
    session = store.get(key.group_chat_id)
    assert (session.revision, assignment_fingerprint(session)) == before


def test_cancel_during_in_flight_makes_late_completion_stale():
    store, manager, key, _ = make_delivery(player_count=2)
    terminal = asyncio.run(store.commit_terminal(key, GameState.CANCELLED, reason="host_cancel"))
    assert terminal.ok
    assert store.get(key.group_chat_id) is None

    late = record(store, manager, key, 1)
    assert not late.ok
    assert late.reason == "stale_generation"
    assert late.effects == ()


def test_old_attempt_completion_cannot_overwrite_current_attempt():
    store, manager, key, _ = make_delivery(player_count=2)
    record(store, manager, key, 2, delivered=False, retryable=True)
    transact(
        store,
        key.group_chat_id,
        lambda view: manager.retry_delivery(view, RetryDeliveryEvent(key, 1)),
    )
    revision = store.get(key.group_chat_id).revision

    stale = record(store, manager, key, 2, attempt=1, delivered=True)
    assert not stale.ok
    assert stale.reason == "stale_delivery_completion"
    session = store.get(key.group_chat_id)
    assert session.revision == revision
    assert session.delivery[2].status == DeliveryStatus.IN_FLIGHT
    assert session.delivery[2].attempts == 2


def test_old_generation_completion_does_not_mutate_new_session():
    store, manager, old_key, _ = make_delivery(player_count=2, group_id=9002)
    asyncio.run(store.commit_terminal(old_key, GameState.CANCELLED))
    manager.create_session(9002, 10, "NewHost")
    current = store.get(9002)
    before = current.public_snapshot()

    stale = record(store, manager, old_key, 1)
    assert not stale.ok
    assert stale.reason == "stale_generation"
    assert store.get(9002).public_snapshot() == before


@settings(max_examples=20, deadline=None, database=None)
@given(outcomes=st.lists(st.booleans(), min_size=2, max_size=6))
def test_property_5_readiness_iff_every_delivery_succeeds(outcomes):
    """Property 5: staged delivery readiness exactly matches confirmations.

    **Validates: Requirements 2.6, 2.7, 2.12**
    """
    store, manager, key, _ = make_delivery(
        player_count=len(outcomes), group_id=9100 + len(outcomes)
    )
    for user_id, delivered in enumerate(outcomes, start=1):
        record(
            store,
            manager,
            key,
            user_id,
            delivered=delivered,
            retryable=False,
        )

    ready = transact(
        store, key.group_chat_id, lambda view: manager.commit_ready(view, key)
    )
    assert ready.ok is all(outcomes)
    session = store.get(key.group_chat_id)
    expected_phase = RoundPhase.DISCUSSION if all(outcomes) else RoundPhase.DELIVERING
    assert session.phase == expected_phase
    assert (session.state == GameState.GUESSING) is all(outcomes)
