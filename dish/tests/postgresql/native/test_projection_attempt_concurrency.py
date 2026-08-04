"""Native PostgreSQL projection-attempt concurrency regressions."""
from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.projection_worker import ExternalAttempt, ExternalObservation, ProjectionWorker
from dish_pg.transition import ProjectionService, TransitionAuthorityError
from dish_pg.workflow import sha256_json
from tests.support.postgresql.core import core_db
from tests.support.postgresql.projection_attempts import native_workflow_db
from tests.support.postgresql.workflow import NOW, _claimed_execution, _next

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


@pytest.fixture(autouse=True)
def _require_native_postgresql(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--postgresql"):
        pytest.skip("native PostgreSQL concurrency certification requires --postgresql")


@contextmanager
def _bound_session(connection):
    session = Session(bind=connection, expire_on_commit=False)
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()



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


def _native_stale_settlement_race(
    core_db,
) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    event_id = _seed_projection(factory, ids, context, task_id)
    with session_scope(factory) as session:
        service = ProjectionService(session, uuid_factory=uuid.uuid4)
        claim_a = service.claim_next(
            worker_id="worker-a", now=NOW, ttl=timedelta(minutes=1)
        )
        attempt_a = service.begin_attempt(
            event_id=event_id,
            claim_token=claim_a.claim_token,
            claim_revision=claim_a.claim_revision,
            worker_id="worker-a",
            request_identity="logical-request",
            request_payload={"notes": "v2"},
            intended_external_id="123456789",
            started_at=NOW,
        )

    reclaimed_at = NOW + timedelta(minutes=2)
    with session_scope(factory) as session:
        claim_b = ProjectionService(session, uuid_factory=uuid.uuid4).claim_next(
            worker_id="worker-b", now=reclaimed_at, ttl=timedelta(minutes=1)
        )
        assert claim_b is not None and claim_b.recovery_required

    barrier = threading.Barrier(2)
    engine = factory.kw["bind"]
    stale_connection = engine.connect()
    current_connection = engine.connect()

    def stale_settle() -> str:
        barrier.wait(timeout=10)
        try:
            with _bound_session(stale_connection) as session:
                ProjectionService(
                    session, uuid_factory=uuid.uuid4
                ).record_observation_and_adjudicate(
                    attempt_id=attempt_a.attempt_id,
                    observation_kind="reread",
                    observed_applied=True,
                    observed_identity=attempt_a.request_sha256,
                    reread_complete=True,
                    evidence=_external_evidence(attempt_a.request_sha256),
                    decided_by="automatic",
                    decision_reason="stale worker late result",
                    observed_at=reclaimed_at,
                    claim_token=claim_a.claim_token,
                    claim_revision=claim_a.claim_revision,
                    worker_id="worker-a",
                )
        except TransitionAuthorityError:
            return "rejected"
        return "unsafe-success"

    def current_owner_settle() -> str:
        barrier.wait(timeout=10)
        with _bound_session(current_connection) as session:
            service = ProjectionService(session, uuid_factory=uuid.uuid4)
            recovery = service.begin_recovery_attempt(
                event_id=event_id,
                claim_token=claim_b.claim_token,
                claim_revision=claim_b.claim_revision,
                worker_id="worker-b",
                prior_attempt_id=claim_b.recovery_attempt.attempt_id,
                started_at=reclaimed_at,
            )
            result = service.record_observation_and_adjudicate(
                attempt_id=recovery.attempt_id,
                observation_kind="reread",
                observed_applied=True,
                observed_identity=recovery.request_sha256,
                reread_complete=True,
                evidence=_external_evidence(recovery.request_sha256),
                decided_by="automatic",
                decision_reason="current owner external reread",
                observed_at=reclaimed_at,
                claim_token=claim_b.claim_token,
                claim_revision=claim_b.claim_revision,
                worker_id="worker-b",
            )
            return result.outcome

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            stale_future = pool.submit(stale_settle)
            current_future = pool.submit(current_owner_settle)
            assert stale_future.result(timeout=20) == "rejected"
            assert current_future.result(timeout=20) == "confirmed"
    finally:
        stale_connection.close()
        current_connection.close()

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
            ("recovery", "confirmed"),
        ]
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionObservation).where(
                tx.ProjectionObservation.attempt_id == attempt_a.attempt_id
            )
        ) == 1


def test_native_stale_settlement_races_current_owner_and_cannot_change_terminal_state(
    core_db,
) -> None:
    _native_stale_settlement_race(core_db)


class _Crash(RuntimeError):
    pass


class _NativeCrashAdapter:
    def __init__(self) -> None:
        self.dispatches = 0
        self.recovery_reads = 0

    def prepare(self, claim) -> ExternalAttempt:
        return ExternalAttempt(
            request_identity=f"native:{claim.event_id}",
            request_payload={"notes": "v2"},
            intended_external_id="123456789",
        )

    def attempt_and_observe(self, claim, attempt) -> ExternalObservation:
        self.dispatches += 1
        raise _Crash("external mutation completed, worker died")

    def observe_recovery(self, claim, attempt) -> ExternalObservation:
        self.recovery_reads += 1
        identity = sha256_json(dict(attempt.request_payload))
        return ExternalObservation(
            observed_applied=True,
            observed_identity=identity,
            reread_complete=True,
            evidence=_external_evidence(identity),
            decision_reason="native PostgreSQL recovery reread",
        )


def test_native_worker_restart_observes_without_second_dispatch(core_db) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    event_id = _seed_projection(factory, ids, context, task_id)
    adapter = _NativeCrashAdapter()
    first = ProjectionWorker(
        session_maker=factory,
        adapter=adapter,
        worker_id="worker-a",
        claim_ttl=timedelta(minutes=1),
        clock=lambda: NOW,
    )
    with pytest.raises(_Crash):
        first.run_once()
    second = ProjectionWorker(
        session_maker=factory,
        adapter=adapter,
        worker_id="worker-b",
        claim_ttl=timedelta(minutes=1),
        clock=lambda: NOW + timedelta(minutes=2),
    )
    assert second.run_once() is True
    assert adapter.dispatches == 1
    assert adapter.recovery_reads == 1
    with session_scope(factory) as session:
        assert session.get(tx.ProjectionOutboxEvent, event_id).state == "applied"
