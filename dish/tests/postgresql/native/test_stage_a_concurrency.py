from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.workflow import ContentionLost, RequestSpec, WorkflowAuthorityService
from tests.support.postgresql.concurrency import run_concurrent_workers, wait_at_barrier
from tests.support.postgresql.core import _bootstrap_registry, _import_one, core_db
from tests.support.postgresql.workflow import NOW, _admit, _execution, _next, _register_run

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


@dataclass(frozen=True)
class _LeaseContender:
    run_id: uuid.UUID
    owner_id: str
    lease_id: uuid.UUID
    actor_attempt_sequence: int


@dataclass(frozen=True)
class _ReservationContender:
    execution_id: uuid.UUID
    reservation_token: uuid.UUID


def _prepare_duplicate_admission_race(factory, ids) -> RequestSpec:
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        generation_id = context["generation_id"]
    return RequestSpec(
        request_id=_next(ids),
        generation_id=generation_id,
        run_id=run_id,
        owner_id="owner-1",
        principal_class="agent",
        command_name="start",
        canonical_payload={"task": "duplicate-delivery"},
        protocol_release="protocol-1",
        dish_release="dish-42619b9",
        admitted_at=NOW,
    )


def _run_duplicate_admission_race(factory, spec: RequestSpec) -> list[str]:
    def admit(index: int, barrier: Barrier) -> str:
        try:
            with session_scope(factory) as session:
                wait_at_barrier(barrier, checkpoint="duplicate admission ready")
                admission = WorkflowAuthorityService(session).admit_request(spec)
            return "replayed" if admission.replayed else "inserted"
        except ContentionLost as exc:
            assert str(exc) == "concurrent request admission won"
            return "contention_lost"

    return run_concurrent_workers(10, admit)


def _prepare_authorization_race(factory, ids) -> tuple[uuid.UUID, list[_ReservationContender]]:
    contenders: list[_ReservationContender] = []
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        task = _import_one(session, ids, context)
        run_id = _next(ids)
        _register_run(
            session, generation_id=context["generation_id"], run_id=run_id,
            owner="marco", agent="marco",
        )
        service = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids))
        grant_request_id, grant_execution_id = _next(ids), _next(ids)
        _admit(
            service, request_id=grant_request_id, generation_id=context["generation_id"],
            run_id=run_id, command="authorize-governed-change", owner="marco",
            principal="admin",
        )
        _execution(
            service, execution_id=grant_execution_id, request_id=grant_request_id,
            generation_id=context["generation_id"], task_id=task.task_id,
            binding_id=context["binding_id"], command="authorize-governed-change",
        )
        grant_id = _next(ids)
        service.grant_marco_authorization(
            grant_id=grant_id, execution_id=grant_execution_id, task_id=task.task_id,
            operation_id=None, field_name="title", before_value="old", after_value="new",
            reason="approved exact governed change", actor="Marco", run_id=run_id,
            granted_at=NOW,
        )
        for index in range(10):
            request_id, execution_id = _next(ids), _next(ids)
            _admit(
                service, request_id=request_id, generation_id=context["generation_id"],
                run_id=run_id, command="prepare", owner="marco", principal="admin",
                payload={"reservation_contender": index},
            )
            _execution(
                service, execution_id=execution_id, request_id=request_id,
                generation_id=context["generation_id"], task_id=task.task_id,
                binding_id=context["binding_id"], command="prepare",
            )
            contenders.append(_ReservationContender(execution_id, _next(ids)))
    return grant_id, contenders


def _run_authorization_race(factory, grant_id, contenders) -> list[str]:
    def reserve(index: int, barrier: Barrier) -> str:
        contender = contenders[index]
        try:
            with session_scope(factory) as session:
                state = session.get(wf.MarcoAuthorizationState, grant_id)
                assert state is not None and state.state == "available"
                assert state.authorization_revision == 1
                wait_at_barrier(barrier, checkpoint="authorization reservation ready")
                WorkflowAuthorityService(session).reserve_marco_authorization(
                    grant_id=grant_id, reservation_token=contender.reservation_token,
                    execution_id=contender.execution_id, reserved_at=NOW,
                )
            return "committed"
        except ContentionLost as exc:
            assert str(exc) == "authorization reservation lost"
            return "contention_lost"

    return run_concurrent_workers(10, reserve)


@pytest.fixture(autouse=True)
def _require_native_postgresql(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--postgresql"):
        pytest.skip("native PostgreSQL concurrency certification requires --postgresql")


def _create_operation_authority(
    session: Session,
    ids,
    *,
    context: dict[str, uuid.UUID],
    task_id: uuid.UUID,
    owner_id: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
    _register_run(
        session,
        generation_id=context["generation_id"],
        run_id=run_id,
        owner=owner_id,
    )
    service = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids))
    _admit(
        service,
        request_id=request_id,
        generation_id=context["generation_id"],
        run_id=run_id,
        owner=owner_id,
    )
    _execution(
        service,
        execution_id=execution_id,
        request_id=request_id,
        generation_id=context["generation_id"],
        task_id=task_id,
        binding_id=context["binding_id"],
    )
    service.repo.capture_task_fence(
        execution_id=execution_id,
        generation_id=context["generation_id"],
        task_id=task_id,
        at=NOW,
    )
    operation = service.create_operation(
        operation_id=_next(ids),
        execution_id=execution_id,
        task_id=task_id,
        kind="initial",
        phase="prepare_required",
        persisted_actions=["prepare"],
        created_at=NOW,
    )
    return run_id, execution_id, operation.operation_id


@pytest.mark.flake_stress
def test_ten_simultaneous_actor_lease_acquisitions_have_one_winner(core_db) -> None:
    factory, ids = core_db
    contenders: list[_LeaseContender] = []
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        task = _import_one(session, ids, context)
        owner_run, execution_id, operation_id = _create_operation_authority(
            session,
            ids,
            context=context,
            task_id=task.task_id,
            owner_id="owner-0",
        )
        contenders.append(
            _LeaseContender(owner_run, "owner-0", _next(ids), 1)
        )
        for index in range(1, 10):
            run_id = _next(ids)
            owner_id = f"owner-{index}"
            _register_run(
                session,
                generation_id=context["generation_id"],
                run_id=run_id,
                owner=owner_id,
            )
            contenders.append(
                _LeaseContender(run_id, owner_id, _next(ids), index + 1)
            )

    def acquire(index: int, barrier: Barrier) -> str:
        contender = contenders[index]
        try:
            with session_scope(factory) as session:
                session.get(wf.CommandExecution, execution_id)
                session.get(wf.WorkflowOperation, operation_id)
                wait_at_barrier(barrier, checkpoint="actor lease ready")
                WorkflowAuthorityService(session).acquire_actor_lease(
                    lease_id=contender.lease_id,
                    execution_id=execution_id,
                    operation_id=operation_id,
                    run_id=contender.run_id,
                    owner_id=contender.owner_id,
                    actor_role="constructor",
                    actor_attempt_sequence=contender.actor_attempt_sequence,
                    issued_at=NOW,
                    expires_at=NOW + timedelta(minutes=10),
                )
            return "committed"
        except ContentionLost as exc:
            assert str(exc) == "another active actor lease or attempt sequence already exists"
            return "contention_lost"

    results = run_concurrent_workers(10, acquire)
    assert results.count("committed") == 1
    assert results.count("contention_lost") == 9

    with session_scope(factory) as session:
        leases = session.scalars(
            select(wf.ServiceLease).where(wf.ServiceLease.operation_id == operation_id)
        ).all()
        assert len(leases) == 1
        assert leases[0].state == "active"
        events = session.scalars(
            select(wf.LeaseEvent).where(wf.LeaseEvent.lease_id == leases[0].lease_id)
        ).all()
        assert len(events) == 1
        assert events[0].event_kind == "issued"
        assert session.scalar(
            select(func.count()).select_from(wf.LeaseEvent).where(
                wf.LeaseEvent.command_execution_id == execution_id
            )
        ) == 1


@pytest.mark.flake_stress
def test_ten_simultaneous_marco_reservations_have_one_winner(core_db) -> None:
    factory, ids = core_db
    grant_id, contenders = _prepare_authorization_race(factory, ids)
    results = _run_authorization_race(factory, grant_id, contenders)
    assert results.count("committed") == 1
    assert results.count("contention_lost") == 9

    with session_scope(factory) as session:
        state = session.get(wf.MarcoAuthorizationState, grant_id)
        assert state is not None
        assert state.state == "reserved"
        assert state.authorization_revision == 2
        events = session.scalars(
            select(wf.MarcoAuthorizationEvent).where(
                wf.MarcoAuthorizationEvent.grant_id == grant_id,
                wf.MarcoAuthorizationEvent.event_kind == "reserved",
            )
        ).all()
        assert len(events) == 1
        assert state.reservation_token == events[0].reservation_token


@pytest.mark.flake_stress
def test_independent_tasks_acquire_authority_without_global_serialization(core_db) -> None:
    factory, ids = core_db
    authorities: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, str]] = []
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        first = _import_one(session, ids, context)
        second = _import_one(
            session,
            ids,
            context,
            task_id=_next(ids),
            asana_gid="987654321",
        )
        for index, task_id in enumerate((first.task_id, second.task_id)):
            owner_id = f"independent-{index}"
            run_id, execution_id, operation_id = _create_operation_authority(
                session,
                ids,
                context=context,
                task_id=task_id,
                owner_id=owner_id,
            )
            authorities.append((run_id, execution_id, operation_id, _next(ids), owner_id))

    after_write = Barrier(2)

    def acquire(index: int, start: Barrier) -> str:
        run_id, execution_id, operation_id, lease_id, owner_id = authorities[index]
        with session_scope(factory) as session:
            session.get(wf.CommandExecution, execution_id)
            session.get(wf.WorkflowOperation, operation_id)
            wait_at_barrier(start, checkpoint="independent lease ready")
            WorkflowAuthorityService(session).acquire_actor_lease(
                lease_id=lease_id,
                execution_id=execution_id,
                operation_id=operation_id,
                run_id=run_id,
                owner_id=owner_id,
                actor_role="constructor",
                actor_attempt_sequence=1,
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            )
            # Both transactions must reach this point before either is allowed to commit.
            # A shared global database fence would prevent the second writer from arriving.
            wait_at_barrier(after_write, checkpoint="independent leases flushed")
        return "committed"

    results = run_concurrent_workers(2, acquire)
    assert results == ["committed", "committed"]

    operation_ids = [item[2] for item in authorities]
    with session_scope(factory) as session:
        assert session.scalar(
            select(func.count()).select_from(wf.ServiceLease).where(
                wf.ServiceLease.operation_id.in_(operation_ids),
                wf.ServiceLease.state == "active",
            )
        ) == 2
        assert session.scalar(
            select(func.count()).select_from(wf.LeaseEvent).where(
                wf.LeaseEvent.event_kind == "issued"
            )
        ) == 2


@pytest.mark.flake_stress
def test_ten_simultaneous_duplicate_request_admissions_perform_one_logical_execution(
    core_db,
) -> None:
    factory, ids = core_db
    spec = _prepare_duplicate_admission_race(factory, ids)
    results = _run_duplicate_admission_race(factory, spec)
    assert results.count("inserted") == 1
    assert results.count("replayed") + results.count("contention_lost") == 9

    with session_scope(factory) as session:
        rows = session.scalars(
            select(wf.ServiceRequest).where(wf.ServiceRequest.request_id == spec.request_id)
        ).all()
        assert len(rows) == 1
        assert rows[0].canonical_payload == spec.canonical_payload
        assert session.scalar(
            select(func.count()).select_from(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == spec.request_id
            )
        ) == 0
