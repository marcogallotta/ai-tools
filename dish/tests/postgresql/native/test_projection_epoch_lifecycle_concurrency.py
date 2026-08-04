"""Native PostgreSQL projection-epoch lifecycle concurrency regressions."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from sqlalchemy import func, select

from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService, TransitionAuthorityError
from tests.support.postgresql.concurrency import (
    TransactionGate,
    assert_transaction_aborted,
    assert_transaction_blocked,
    execute_transaction,
    independent_connections,
    managed_session,
)
from tests.support.postgresql.core import core_db
from tests.support.postgresql.projection_attempts import native_workflow_db
from tests.support.postgresql.workflow import NOW, _claimed_execution, _next

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


@pytest.fixture(autouse=True)
def _require_native_postgresql(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--postgresql"):
        pytest.skip("native PostgreSQL concurrency certification requires --postgresql")


def _external_evidence(identity: str | None = None) -> dict:
    fact = {
        "source": "external_reread",
        "operation": "update_task_document",
        "observed_external_id": "123456789",
    }
    if identity is not None:
        fact["observed_document_identity"] = identity
    return {"external_observation": fact}


def _seed_projection(factory, ids, context, task_id) -> uuid.UUID:
    with session_scope(factory) as session:
        projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
        projection.activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="native PostgreSQL projection concurrency",
            created_at=NOW,
            external_effects_enabled=True,
        )
        execution_id = _claimed_execution(session, ids, context, task_id)
        return projection.record(
            generation_id=context["generation_id"],
            execution_id=execution_id,
            task_id=task_id,
            event_type="update_task_document",
            payload={"content_version_id": "v2"},
            created_at=NOW,
        )


class _CandidateSelectionGateService(ProjectionService):
    def __init__(self, *args, gate: TransactionGate, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._gate = gate

    def _claim_candidates(self, *, now):
        candidates = super()._claim_candidates(now=now)
        self._gate.block()
        return candidates


class _AttemptBoundaryGateService(ProjectionService):
    def __init__(self, *args, gate: TransactionGate, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._gate = gate

    def _lock_event_path(self, event_id):
        self._gate.block()
        return super()._lock_event_path(event_id)


class _EventAdmissionGateService(ProjectionService):
    def __init__(self, *args, gate: TransactionGate, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._gate = gate

    def _active_epoch_for_generation(self, generation_id, *, shared):
        epoch = super()._active_epoch_for_generation(generation_id, shared=shared)
        self._gate.block()
        return epoch


class _SettlementGateService(ProjectionService):
    def __init__(self, *args, gate: TransactionGate, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._gate = gate

    def _lock_attempt_path(self, attempt_id):
        path = super()._lock_attempt_path(attempt_id)
        self._gate.block()
        return path


class _RetirementStartedService(ProjectionService):
    def __init__(self, *args, started: Event, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._started = started

    def _lock_epoch(self, projection_epoch_id, *, shared):
        if not shared:
            self._started.set()
        return super()._lock_epoch(projection_epoch_id, shared=shared)


def test_native_disable_during_candidate_selection_prevents_claim(core_db) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    event_id = _seed_projection(factory, ids, context, task_id)
    with session_scope(factory) as session:
        epoch_id = session.get(tx.ProjectionOutboxEvent, event_id).projection_epoch_id

    gate = TransactionGate(label="projection claim candidate selection")
    engine = factory.kw["bind"]

    with independent_connections(engine) as (claim_connection, disable_connection):
        def claim_after_selection():
            with managed_session(claim_connection) as session:
                return _CandidateSelectionGateService(
                    session,
                    uuid_factory=uuid.uuid4,
                    gate=gate,
                ).claim_next(
                    worker_id="candidate-worker",
                    now=NOW,
                    ttl=timedelta(minutes=1),
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(claim_after_selection)
            gate.wait_until_blocked()
            assert_transaction_blocked(future)
            try:
                with managed_session(disable_connection) as session:
                    ProjectionService(session, uuid_factory=uuid.uuid4).set_external_effects_enabled(
                        projection_epoch_id=epoch_id,
                        enabled=False,
                        reason="native candidate selection race",
                    )
            finally:
                gate.release()
            assert future.result(timeout=30) is None

    with session_scope(factory) as session:
        epoch = session.get(tx.ProjectionEpoch, epoch_id)
        event = session.get(tx.ProjectionOutboxEvent, event_id)
        assert epoch.external_effects_enabled is False
        assert event.state == "pending"
        assert event.claim_owner is None
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionAttempt).where(
                tx.ProjectionAttempt.projection_event_id == event_id
            )
        ) == 0


def test_native_disable_after_claim_blocks_durable_dispatch_attempt(core_db) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    event_id = _seed_projection(factory, ids, context, task_id)
    with session_scope(factory) as session:
        service = ProjectionService(session, uuid_factory=uuid.uuid4)
        claim = service.claim_next(
            worker_id="claimed-worker",
            now=NOW,
            ttl=timedelta(minutes=1),
        )
        assert claim is not None
        epoch_id = session.get(tx.ProjectionOutboxEvent, event_id).projection_epoch_id

    gate = TransactionGate(label="durable dispatch attempt boundary")
    engine = factory.kw["bind"]

    with independent_connections(engine) as (attempt_connection, disable_connection):
        def begin_attempt(session):
            return _AttemptBoundaryGateService(
                session,
                uuid_factory=uuid.uuid4,
                gate=gate,
            ).begin_attempt(
                event_id=event_id,
                claim_token=claim.claim_token,
                claim_revision=claim.claim_revision,
                worker_id="claimed-worker",
                request_identity="disable-race-request",
                request_payload={"notes": "v2"},
                intended_external_id="123456789",
                started_at=NOW,
            )

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(execute_transaction, attempt_connection, begin_attempt)
            gate.wait_until_blocked()
            assert_transaction_blocked(future)
            try:
                with managed_session(disable_connection) as session:
                    ProjectionService(session, uuid_factory=uuid.uuid4).set_external_effects_enabled(
                        projection_epoch_id=epoch_id,
                        enabled=False,
                        reason="native durable attempt race",
                    )
            finally:
                gate.release()
            outcome = future.result(timeout=30)

    assert_transaction_aborted(outcome, error_type=TransitionAuthorityError)
    with session_scope(factory) as session:
        event = session.get(tx.ProjectionOutboxEvent, event_id)
        assert event.state == "claimed"
        assert event.claim_token == claim.claim_token
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionAttempt).where(
                tx.ProjectionAttempt.projection_event_id == event_id
            )
        ) == 0


def test_native_event_insertion_admitted_before_retirement_is_superseded(core_db) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    with session_scope(factory) as session:
        projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
        epoch = projection.activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="native insertion retirement race",
            created_at=NOW,
            external_effects_enabled=True,
        )
        epoch_id = epoch.projection_epoch_id
        execution_id = _claimed_execution(session, ids, context, task_id)

    gate = TransactionGate(label="event admitted under epoch lock")
    retirement_started = Event()
    engine = factory.kw["bind"]

    with independent_connections(engine) as (insert_connection, retire_connection):
        def insert_event():
            with managed_session(insert_connection) as session:
                return _EventAdmissionGateService(
                    session,
                    uuid_factory=uuid.uuid4,
                    gate=gate,
                ).record(
                    generation_id=context["generation_id"],
                    execution_id=execution_id,
                    task_id=task_id,
                    event_type="update_task_document",
                    payload={"content_version_id": "retirement-race"},
                    created_at=NOW,
                )

        def retire_epoch():
            with managed_session(retire_connection) as session:
                _RetirementStartedService(
                    session,
                    uuid_factory=uuid.uuid4,
                    started=retirement_started,
                ).retire_epoch(
                    projection_epoch_id=epoch_id,
                    retired_at=NOW + timedelta(seconds=1),
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            insert_future = pool.submit(insert_event)
            gate.wait_until_blocked()
            retire_future = pool.submit(retire_epoch)
            assert retirement_started.wait(timeout=30)
            assert_transaction_blocked(retire_future)
            try:
                gate.release()
                event_id = insert_future.result(timeout=30)
                retire_future.result(timeout=30)
            finally:
                gate.release()

    with session_scope(factory) as session:
        epoch = session.get(tx.ProjectionEpoch, epoch_id)
        event = session.get(tx.ProjectionOutboxEvent, event_id)
        assert epoch.status == "retired"
        assert event.state == "superseded"
        assert event.terminal_at == NOW + timedelta(seconds=1)
        assert event.claim_owner is None


def test_native_confirmed_settlement_waiting_before_retirement_is_preserved(core_db) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    event_id = _seed_projection(factory, ids, context, task_id)
    with session_scope(factory) as session:
        service = ProjectionService(session, uuid_factory=uuid.uuid4)
        claim = service.claim_next(
            worker_id="settlement-worker",
            now=NOW,
            ttl=timedelta(minutes=1),
        )
        assert claim is not None
        attempt = service.begin_attempt(
            event_id=event_id,
            claim_token=claim.claim_token,
            claim_revision=claim.claim_revision,
            worker_id="settlement-worker",
            request_identity="settlement-race-request",
            request_payload={"notes": "v2"},
            intended_external_id="123456789",
            started_at=NOW,
        )
        attempt_id = attempt.attempt_id
        request_sha256 = attempt.request_sha256
        epoch_id = session.get(tx.ProjectionOutboxEvent, event_id).projection_epoch_id

    gate = TransactionGate(label="confirmed settlement holds lifecycle locks")
    retirement_started = Event()
    engine = factory.kw["bind"]

    with independent_connections(engine) as (settle_connection, retire_connection):
        def settle_attempt():
            with managed_session(settle_connection) as session:
                return _SettlementGateService(
                    session,
                    uuid_factory=uuid.uuid4,
                    gate=gate,
                ).record_observation_and_adjudicate(
                    attempt_id=attempt_id,
                    observation_kind="reread",
                    observed_applied=True,
                    observed_identity=request_sha256,
                    reread_complete=True,
                    evidence=_external_evidence(request_sha256),
                    decided_by="automatic",
                    decision_reason="native confirmed settlement race",
                    observed_at=NOW + timedelta(seconds=1),
                    claim_token=claim.claim_token,
                    claim_revision=claim.claim_revision,
                    worker_id="settlement-worker",
                ).outcome

        def retire_epoch():
            with managed_session(retire_connection) as session:
                _RetirementStartedService(
                    session,
                    uuid_factory=uuid.uuid4,
                    started=retirement_started,
                ).retire_epoch(
                    projection_epoch_id=epoch_id,
                    retired_at=NOW + timedelta(seconds=2),
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            settle_future = pool.submit(settle_attempt)
            gate.wait_until_blocked()
            retire_future = pool.submit(retire_epoch)
            assert retirement_started.wait(timeout=30)
            assert_transaction_blocked(retire_future)
            try:
                gate.release()
                assert settle_future.result(timeout=30) == "confirmed"
                retire_future.result(timeout=30)
            finally:
                gate.release()

    with session_scope(factory) as session:
        epoch = session.get(tx.ProjectionEpoch, epoch_id)
        event = session.get(tx.ProjectionOutboxEvent, event_id)
        attempt = session.get(tx.ProjectionAttempt, attempt_id)
        assert epoch.status == "retired"
        assert event.state == "applied"
        assert event.terminal_at == NOW + timedelta(seconds=1)
        assert attempt.state == "confirmed"
        assert attempt.terminal_at == NOW + timedelta(seconds=1)


