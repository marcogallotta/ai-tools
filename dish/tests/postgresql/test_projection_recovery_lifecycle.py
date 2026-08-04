from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.projection_worker import ExternalAttempt, ExternalObservation, ProjectionWorker
from tests.support.postgresql.projection_attempts import (
    MutableClock,
    external_evidence,
    projection,
    request_identity,
    seed_events,
)
from tests.support.postgresql.workflow import NOW, workflow_db


class _CrashAfterDispatch(RuntimeError):
    pass


class _CrashDuringRecovery(RuntimeError):
    pass


class _CrashRecoveryAdapter:
    def __init__(self, factory, *, recovery_applied: bool | None = True) -> None:
        self.factory = factory
        self.recovery_applied = recovery_applied
        self.dispatches = 0
        self.recovery_reads = 0
        self.active_attempt_seen = False

    def prepare(self, claim) -> ExternalAttempt:
        return ExternalAttempt(
            request_identity=f"asana-update:{claim.event_id}",
            request_payload={"notes": claim.payload["content_version_id"]},
            intended_external_id="123456789",
        )

    def attempt_and_observe(self, claim, attempt: ExternalAttempt) -> ExternalObservation:
        self.dispatches += 1
        with session_scope(self.factory) as session:
            active = session.scalar(
                select(tx.ProjectionAttempt).where(
                    tx.ProjectionAttempt.projection_event_id == claim.event_id,
                    tx.ProjectionAttempt.dispatch_identity == attempt.request_identity,
                    tx.ProjectionAttempt.state == "dispatched",
                )
            )
            self.active_attempt_seen = active is not None
        raise _CrashAfterDispatch("external call completed before process crash")

    def observe_recovery(self, claim, attempt: ExternalAttempt) -> ExternalObservation:
        self.recovery_reads += 1
        if self.recovery_applied is None:
            return ExternalObservation(
                observed_applied=None,
                observed_identity=None,
                reread_complete=False,
                evidence=external_evidence(available=False),
                decision_reason="external reread unavailable",
            )
        identity = request_identity(attempt.request_payload) if self.recovery_applied else None
        return ExternalObservation(
            observed_applied=self.recovery_applied,
            observed_identity=identity,
            reread_complete=True,
            evidence=external_evidence(
                observed_identity=identity,
                absent=self.recovery_applied is False,
            ),
            decision_reason="independent recovery reread",
        )


class _CrashFirstRecoveryAdapter(_CrashRecoveryAdapter):
    def observe_recovery(self, claim, attempt: ExternalAttempt) -> ExternalObservation:
        self.recovery_reads += 1
        if self.recovery_reads == 1:
            raise _CrashDuringRecovery(
                "recovery reread completed externally before process crash"
            )
        identity = request_identity(attempt.request_payload)
        return ExternalObservation(
            observed_applied=True,
            observed_identity=identity,
            reread_complete=True,
            evidence=external_evidence(observed_identity=identity),
            decision_reason="independent reread after recovery restart",
        )


def test_crash_after_external_call_recovers_without_redispatch_and_preserves_order(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    first_event, second_event = seed_events(
        factory, ids, context, task_id, count=2
    )
    clock = MutableClock(NOW)
    adapter = _CrashRecoveryAdapter(factory)
    first_worker = ProjectionWorker(
        session_maker=factory,
        adapter=adapter,
        worker_id="worker-a",
        claim_ttl=timedelta(minutes=1),
        clock=clock,
    )

    with pytest.raises(_CrashAfterDispatch):
        first_worker.run_once()
    assert adapter.dispatches == 1
    assert adapter.active_attempt_seen is True

    with session_scope(factory) as session:
        first = session.get(tx.ProjectionOutboxEvent, first_event)
        second = session.get(tx.ProjectionOutboxEvent, second_event)
        assert first.state == "claimed"
        assert second.state == "pending"
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionAttempt).where(
                tx.ProjectionAttempt.projection_event_id == first_event,
                tx.ProjectionAttempt.state == "dispatched",
            )
        ) == 1

    clock.value = NOW + timedelta(minutes=2)
    second_worker = ProjectionWorker(
        session_maker=factory,
        adapter=adapter,
        worker_id="worker-b",
        claim_ttl=timedelta(minutes=1),
        clock=clock,
    )
    assert second_worker.run_once() is True
    assert adapter.dispatches == 1
    assert adapter.recovery_reads == 1

    with session_scope(factory) as session:
        first = session.get(tx.ProjectionOutboxEvent, first_event)
        attempts = session.scalars(
            select(tx.ProjectionAttempt)
            .where(tx.ProjectionAttempt.projection_event_id == first_event)
            .order_by(tx.ProjectionAttempt.attempt_number)
        ).all()
        assert first.state == "applied"
        assert [(row.attempt_kind, row.state) for row in attempts] == [
            ("dispatch", "uncertain"),
            ("recovery", "confirmed"),
        ]
        assert attempts[1].predecessor_attempt_id == attempts[0].attempt_id
        next_claim = projection(session).claim_next(
            worker_id="worker-c",
            now=clock.value,
            ttl=timedelta(minutes=1),
        )
        assert next_claim is not None and next_claim.event_id == second_event


def test_recovery_crash_restarts_observation_without_reopening_dispatch(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    (event_id,) = seed_events(factory, ids, context, task_id)
    clock = MutableClock(NOW)
    adapter = _CrashFirstRecoveryAdapter(factory)

    worker_a = ProjectionWorker(
        session_maker=factory,
        adapter=adapter,
        worker_id="worker-a",
        claim_ttl=timedelta(minutes=1),
        clock=clock,
    )
    with pytest.raises(_CrashAfterDispatch):
        worker_a.run_once()

    clock.value = NOW + timedelta(minutes=2)
    worker_b = ProjectionWorker(
        session_maker=factory,
        adapter=adapter,
        worker_id="worker-b",
        claim_ttl=timedelta(minutes=1),
        clock=clock,
    )
    with pytest.raises(_CrashDuringRecovery):
        worker_b.run_once()

    clock.value = NOW + timedelta(minutes=4)
    worker_c = ProjectionWorker(
        session_maker=factory,
        adapter=adapter,
        worker_id="worker-c",
        claim_ttl=timedelta(minutes=1),
        clock=clock,
    )
    assert worker_c.run_once() is True
    assert adapter.dispatches == 1
    assert adapter.recovery_reads == 2

    with session_scope(factory) as session:
        event = session.get(tx.ProjectionOutboxEvent, event_id)
        attempts = session.scalars(
            select(tx.ProjectionAttempt)
            .where(tx.ProjectionAttempt.projection_event_id == event_id)
            .order_by(tx.ProjectionAttempt.attempt_number)
        ).all()
        assert event.state == "applied"
        assert [(row.attempt_kind, row.state) for row in attempts] == [
            ("dispatch", "uncertain"),
            ("recovery", "uncertain"),
            ("recovery", "confirmed"),
        ]
        assert attempts[1].predecessor_attempt_id == attempts[0].attempt_id
        assert attempts[2].predecessor_attempt_id == attempts[0].attempt_id
        assert attempts[1].dispatch_identity != attempts[2].dispatch_identity


def test_unavailable_recovery_becomes_uncertain_without_redispatch_or_rollout_advance(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    first_event, second_event = seed_events(
        factory, ids, context, task_id, count=2
    )
    clock = MutableClock(NOW)
    adapter = _CrashRecoveryAdapter(factory, recovery_applied=None)
    worker_a = ProjectionWorker(
        session_maker=factory,
        adapter=adapter,
        worker_id="worker-a",
        claim_ttl=timedelta(minutes=1),
        clock=clock,
    )
    with pytest.raises(_CrashAfterDispatch):
        worker_a.run_once()

    clock.value = NOW + timedelta(minutes=2)
    worker_b = ProjectionWorker(
        session_maker=factory,
        adapter=adapter,
        worker_id="worker-b",
        claim_ttl=timedelta(minutes=1),
        clock=clock,
    )
    assert worker_b.run_once() is True
    assert adapter.dispatches == 1
    assert adapter.recovery_reads == 1
    with session_scope(factory) as session:
        assert session.get(tx.ProjectionOutboxEvent, first_event).state == "uncertain"
        assert session.get(tx.ProjectionOutboxEvent, second_event).state == "pending"
        assert projection(session).claim_next(
            worker_id="worker-c",
            now=clock.value,
            ttl=timedelta(minutes=1),
        ) is None
