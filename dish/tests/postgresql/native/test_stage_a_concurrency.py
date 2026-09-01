from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import timedelta
from threading import Barrier, Event

import pytest
from sqlalchemy import event as sa_event, func, select
from sqlalchemy.orm import Session

from dish_pg import reservation_models as reservations
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.workflow import (
    ContentionLost,
    OperationRunRevoked,
    RequestIdentityConflict,
    RequestSpec,
    WorkflowAuthorityRepository,
    WorkflowAuthorityService,
)
from tests.support.postgresql.concurrency import (
    DEFAULT_RACE_TIMEOUT_SECONDS,
    TransactionGate,
    assert_transaction_blocked,
    independent_connections,
    managed_session,
    run_concurrent_workers,
    wait_at_barrier,
)
from tests.support.postgresql.core import _bootstrap_registry, _import_one, core_db
from tests.support.postgresql.native_first_request_reservation_single_gate import (
    _seed as _seed_first_request,
)
from tests.support.postgresql.native_first_request_reservation_single_gate import (
    _spec as _first_request_spec,
)
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
        except ContentionLost:
            return "contention_lost"

    return run_concurrent_workers(10, admit)


def _wait_for_backend_blocked_by(
    session: Session, *, blocked_pid: int, blocking_pid: int
) -> None:
    deadline = time.monotonic() + DEFAULT_RACE_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        blocking_pids = session.scalar(select(func.pg_blocking_pids(blocked_pid)))
        if blocking_pids is not None and blocking_pid in blocking_pids:
            return
        time.sleep(0.01)
    raise AssertionError(
        f"backend {blocked_pid} was not observed blocked by backend {blocking_pid}"
    )


def _admit_with_insert_probe(
    connection,
    spec: RequestSpec,
    *,
    backend_pid: dict[str, int],
    backend_ready: Event,
    insert_started: Event,
):
    def before_cursor_execute(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        normalized = " ".join(statement.upper().split())
        if "INSERT INTO SERVICE_REQUESTS" in normalized and "ON CONFLICT" in normalized:
            insert_started.set()

    with managed_session(connection) as session:
        backend_pid["value"] = session.scalar(select(func.pg_backend_pid()))
        backend_ready.set()
        sa_event.listen(connection, "before_cursor_execute", before_cursor_execute)
        try:
            return WorkflowAuthorityService(session).admit_request(spec)
        finally:
            sa_event.remove(connection, "before_cursor_execute", before_cursor_execute)


def _admit_with_reservation_lock_probe(
    connection,
    spec: RequestSpec,
    *,
    backend_pid: dict[str, int],
    backend_ready: Event,
    lock_started: Event,
):
    def before_cursor_execute(
        _conn, _cursor, statement, _parameters, _context, _executemany
    ) -> None:
        normalized = " ".join(statement.upper().split())
        if "FROM FIRST_REQUEST_RESERVATIONS" in normalized and "FOR UPDATE" in normalized:
            lock_started.set()

    with managed_session(connection) as session:
        backend_pid["value"] = session.scalar(select(func.pg_backend_pid()))
        backend_ready.set()
        sa_event.listen(connection, "before_cursor_execute", before_cursor_execute)
        try:
            return WorkflowAuthorityService(session).admit_request(spec)
        finally:
            sa_event.remove(connection, "before_cursor_execute", before_cursor_execute)


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
    run_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    if run_id is None:
        run_id = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
            owner=owner_id,
        )
    request_id, execution_id = _next(ids), _next(ids)
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


class _OperationLockNotifyingRepository(WorkflowAuthorityRepository):
    """Signal immediately before attempting the authoritative operation-row lock."""

    def __init__(self, session: Session, *, before_operation_lock: Event) -> None:
        super().__init__(session)
        self._before_operation_lock = before_operation_lock

    def _locked_operation(
        self, *, generation_id: uuid.UUID, operation_id: uuid.UUID
    ) -> wf.WorkflowOperation:
        self._before_operation_lock.set()
        return super()._locked_operation(
            generation_id=generation_id, operation_id=operation_id
        )


def _service_signaling_operation_lock(
    session: Session, *, before_operation_lock: Event
) -> WorkflowAuthorityService:
    service = WorkflowAuthorityService(session)
    service.repo = _OperationLockNotifyingRepository(
        session, before_operation_lock=before_operation_lock
    )
    return service


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


def test_exact_duplicate_request_recovers_after_overlapping_insert_contention(
    core_db,
) -> None:
    factory, ids = core_db
    spec = _prepare_duplicate_admission_race(factory, ids)
    engine = factory.kw["bind"]

    with independent_connections(engine) as (winner_connection, loser_connection):
        winner = Session(bind=winner_connection, expire_on_commit=False)
        try:
            winner_pid = winner.scalar(select(func.pg_backend_pid()))
            winning_admission = WorkflowAuthorityService(winner).admit_request(spec)
            assert winning_admission.replayed is False

            loser_pid: dict[str, int] = {}
            loser_ready = Event()
            insert_started = Event()
            with ThreadPoolExecutor(max_workers=1) as pool:
                loser_future = pool.submit(
                    _admit_with_insert_probe,
                    loser_connection,
                    spec,
                    backend_pid=loser_pid,
                    backend_ready=loser_ready,
                    insert_started=insert_started,
                )
                assert loser_ready.wait(timeout=DEFAULT_RACE_TIMEOUT_SECONDS)
                assert insert_started.wait(timeout=DEFAULT_RACE_TIMEOUT_SECONDS)
                _wait_for_backend_blocked_by(
                    winner,
                    blocked_pid=loser_pid["value"],
                    blocking_pid=winner_pid,
                )
                assert_transaction_blocked(loser_future)

                winner.commit()
                recovered = loser_future.result(timeout=DEFAULT_RACE_TIMEOUT_SECONDS)

            assert recovered.replayed is True
            assert recovered.request.request_id == spec.request_id
            assert recovered.outcome is None
        finally:
            winner.rollback()
            winner.close()

    with session_scope(factory) as session:
        assert session.scalar(
            select(func.count()).select_from(wf.ServiceRequest).where(
                wf.ServiceRequest.request_id == spec.request_id
            )
        ) == 1


def test_payload_mismatch_request_fails_distinctly_after_overlapping_insert_contention(
    core_db,
) -> None:
    factory, ids = core_db
    spec = _prepare_duplicate_admission_race(factory, ids)
    conflicting_spec = replace(
        spec, canonical_payload={"task": "different-duplicate-delivery"}
    )
    engine = factory.kw["bind"]

    with independent_connections(engine) as (winner_connection, loser_connection):
        winner = Session(bind=winner_connection, expire_on_commit=False)
        try:
            winner_pid = winner.scalar(select(func.pg_backend_pid()))
            WorkflowAuthorityService(winner).admit_request(spec)

            loser_pid: dict[str, int] = {}
            loser_ready = Event()
            insert_started = Event()
            with ThreadPoolExecutor(max_workers=1) as pool:
                loser_future = pool.submit(
                    _admit_with_insert_probe,
                    loser_connection,
                    conflicting_spec,
                    backend_pid=loser_pid,
                    backend_ready=loser_ready,
                    insert_started=insert_started,
                )
                assert loser_ready.wait(timeout=DEFAULT_RACE_TIMEOUT_SECONDS)
                assert insert_started.wait(timeout=DEFAULT_RACE_TIMEOUT_SECONDS)
                _wait_for_backend_blocked_by(
                    winner,
                    blocked_pid=loser_pid["value"],
                    blocking_pid=winner_pid,
                )
                assert_transaction_blocked(loser_future)

                winner.commit()
                with pytest.raises(
                    RequestIdentityConflict, match="service request identity conflict"
                ):
                    loser_future.result(timeout=DEFAULT_RACE_TIMEOUT_SECONDS)
        finally:
            winner.rollback()
            winner.close()


def test_exact_duplicate_reserved_first_request_recovers_after_reservation_contention(
    core_db,
) -> None:
    factory, ids = core_db
    generation_id, request_id, run_id, payload = _seed_first_request(factory, ids)
    spec = _first_request_spec(
        request_id=request_id,
        generation_id=generation_id,
        run_id=run_id,
        payload=payload,
    )
    engine = factory.kw["bind"]

    with independent_connections(engine) as (winner_connection, loser_connection):
        winner = Session(bind=winner_connection, expire_on_commit=False)
        try:
            winner_pid = winner.scalar(select(func.pg_backend_pid()))
            winning_admission = WorkflowAuthorityService(winner).admit_request(spec)
            assert winning_admission.replayed is False

            loser_pid: dict[str, int] = {}
            loser_ready = Event()
            lock_started = Event()
            with ThreadPoolExecutor(max_workers=1) as pool:
                loser_future = pool.submit(
                    _admit_with_reservation_lock_probe,
                    loser_connection,
                    spec,
                    backend_pid=loser_pid,
                    backend_ready=loser_ready,
                    lock_started=lock_started,
                )
                assert loser_ready.wait(timeout=DEFAULT_RACE_TIMEOUT_SECONDS)
                assert lock_started.wait(timeout=DEFAULT_RACE_TIMEOUT_SECONDS)
                _wait_for_backend_blocked_by(
                    winner,
                    blocked_pid=loser_pid["value"],
                    blocking_pid=winner_pid,
                )
                assert_transaction_blocked(loser_future)

                winner.commit()
                recovered = loser_future.result(timeout=DEFAULT_RACE_TIMEOUT_SECONDS)

            assert recovered.replayed is True
            assert recovered.request.request_id == request_id
            assert recovered.outcome is None
        finally:
            winner.rollback()
            winner.close()

    with session_scope(factory) as session:
        assert session.scalar(
            select(func.count()).select_from(wf.ServiceRequest).where(
                wf.ServiceRequest.request_id == request_id
            )
        ) == 1
        reservation = session.scalar(select(reservations.FirstRequestReservation))
        assert reservation is not None and reservation.state == "consumed"


def test_payload_mismatch_reserved_first_request_conflicts_after_reservation_contention(
    core_db,
) -> None:
    factory, ids = core_db
    generation_id, request_id, run_id, payload = _seed_first_request(factory, ids)
    spec = _first_request_spec(
        request_id=request_id,
        generation_id=generation_id,
        run_id=run_id,
        payload=payload,
    )
    conflicting_spec = replace(
        spec, canonical_payload={"command": "start", "arguments": {"task_id": "different"}}
    )
    engine = factory.kw["bind"]

    with independent_connections(engine) as (winner_connection, loser_connection):
        winner = Session(bind=winner_connection, expire_on_commit=False)
        try:
            winner_pid = winner.scalar(select(func.pg_backend_pid()))
            WorkflowAuthorityService(winner).admit_request(spec)

            loser_pid: dict[str, int] = {}
            loser_ready = Event()
            lock_started = Event()
            with ThreadPoolExecutor(max_workers=1) as pool:
                loser_future = pool.submit(
                    _admit_with_reservation_lock_probe,
                    loser_connection,
                    conflicting_spec,
                    backend_pid=loser_pid,
                    backend_ready=loser_ready,
                    lock_started=lock_started,
                )
                assert loser_ready.wait(timeout=DEFAULT_RACE_TIMEOUT_SECONDS)
                assert lock_started.wait(timeout=DEFAULT_RACE_TIMEOUT_SECONDS)
                _wait_for_backend_blocked_by(
                    winner,
                    blocked_pid=loser_pid["value"],
                    blocking_pid=winner_pid,
                )
                assert_transaction_blocked(loser_future)

                winner.commit()
                with pytest.raises(
                    RequestIdentityConflict, match="service request identity conflict"
                ):
                    loser_future.result(timeout=DEFAULT_RACE_TIMEOUT_SECONDS)
        finally:
            winner.rollback()
            winner.close()

    with session_scope(factory) as session:
        request = session.get(wf.ServiceRequest, request_id)
        assert request is not None
        assert request.canonical_payload == payload
        reservation = session.scalar(select(reservations.FirstRequestReservation))
        assert reservation is not None and reservation.state == "consumed"


@pytest.mark.flake_stress
def test_ten_simultaneous_duplicate_request_admissions_perform_one_logical_execution(
    core_db,
) -> None:
    factory, ids = core_db
    spec = _prepare_duplicate_admission_race(factory, ids)
    results = _run_duplicate_admission_race(factory, spec)
    assert results.count("inserted") == 1
    assert results.count("replayed") == 9
    assert results.count("contention_lost") == 0

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


@pytest.mark.flake_stress
def test_native_revocation_commits_before_claim_rechecks_after_operation_lock_and_is_operation_scoped(
    core_db,
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        first_task = _import_one(session, ids, context)
        second_task = _import_one(
            session,
            ids,
            context,
            task_id=_next(ids),
            asana_gid="987654322",
        )
        run_id, execution_id, operation_id = _create_operation_authority(
            session,
            ids,
            context=context,
            task_id=first_task.task_id,
            owner_id="owner-1",
        )
        _, unrelated_execution_id, unrelated_operation_id = _create_operation_authority(
            session,
            ids,
            context=context,
            task_id=second_task.task_id,
            owner_id="owner-1",
            run_id=run_id,
        )
    revocation_id, claim_token = _next(ids), _next(ids)
    revocation_holds_lock = TransactionGate(label="exact revocation owns operation-row lock")
    claim_attempted_operation_lock = Event()
    engine = factory.kw["bind"]

    def revoke_first() -> str:
        with managed_session(revocation_connection) as session:
            WorkflowAuthorityService(session).repo.revoke_operation_run(
                revocation_id=revocation_id,
                generation_id=context["generation_id"],
                operation_id=operation_id,
                owner_id="owner-1",
                run_id=run_id,
                reason="native exact-revocation wins before claim",
                revoked_at=NOW,
            )
            revocation_holds_lock.block()
        return "revocation_committed"

    def claim_after_lock() -> str:
        try:
            with managed_session(claim_connection) as session:
                service = _service_signaling_operation_lock(
                    session,
                    before_operation_lock=claim_attempted_operation_lock,
                )
                service.repo.claim_execution(
                    execution_id=execution_id,
                    claimant=f"owner-1:{run_id}",
                    claim_token=claim_token,
                    now=NOW,
                    ttl=timedelta(minutes=2),
                )
            return "claim_committed"
        except OperationRunRevoked:
            return "claim_rejected_revoked"

    with independent_connections(engine) as (revocation_connection, claim_connection):
        with ThreadPoolExecutor(max_workers=2) as pool:
            revocation_future = pool.submit(revoke_first)
            revocation_holds_lock.wait_until_blocked()
            claim_future = pool.submit(claim_after_lock)
            assert claim_attempted_operation_lock.wait(timeout=20)
            assert_transaction_blocked(claim_future)
            revocation_holds_lock.release()
            assert revocation_future.result(timeout=20) == "revocation_committed"
            assert claim_future.result(timeout=20) == "claim_rejected_revoked"

    with session_scope(factory) as session:
        execution = session.get(wf.CommandExecution, execution_id)
        revocation = session.get(wf.OperationRunRevocation, revocation_id)
        assert execution is not None and execution.status == "pending"
        assert revocation is not None
        assert revocation.operation_id == operation_id
        assert revocation.owner_id == "owner-1"
        assert revocation.run_id == run_id

        unrelated = WorkflowAuthorityService(session).repo.claim_execution(
            execution_id=unrelated_execution_id,
            claimant=f"owner-1:{run_id}",
            claim_token=_next(ids),
            now=NOW,
            ttl=timedelta(minutes=2),
        )
        assert unrelated.operation_id == unrelated_operation_id
        assert unrelated.status == "claimed"


@pytest.mark.flake_stress
def test_native_revocation_commits_before_acquire_and_renew_recheck_after_operation_lock(
    core_db,
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        acquire_task = _import_one(session, ids, context)
        renew_task = _import_one(
            session,
            ids,
            context,
            task_id=_next(ids),
            asana_gid="987654323",
        )
        acquire_run_id, acquire_execution_id, acquire_operation_id = _create_operation_authority(
            session,
            ids,
            context=context,
            task_id=acquire_task.task_id,
            owner_id="owner-acquire",
        )
        renew_run_id, renew_execution_id, renew_operation_id = _create_operation_authority(
            session,
            ids,
            context=context,
            task_id=renew_task.task_id,
            owner_id="owner-renew",
        )
        renewal_lease_id = _next(ids)
        WorkflowAuthorityService(session).acquire_actor_lease(
            lease_id=renewal_lease_id,
            execution_id=renew_execution_id,
            operation_id=renew_operation_id,
            run_id=renew_run_id,
            owner_id="owner-renew",
            actor_role="constructor",
            actor_attempt_sequence=1,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )

    engine = factory.kw["bind"]
    acquire_revocation_id = _next(ids)
    acquire_revocation_holds_lock = TransactionGate(
        label="exact revocation owns operation row before actor lease acquisition"
    )
    acquire_attempted_operation_lock = Event()
    acquire_lease_id = _next(ids)

    def revoke_before_acquire() -> str:
        with managed_session(revocation_connection) as session:
            WorkflowAuthorityService(session).repo.revoke_operation_run(
                revocation_id=acquire_revocation_id,
                generation_id=context["generation_id"],
                operation_id=acquire_operation_id,
                owner_id="owner-acquire",
                run_id=acquire_run_id,
                reason="native exact-revocation wins before actor lease acquisition",
                revoked_at=NOW,
            )
            acquire_revocation_holds_lock.block()
        return "revocation_committed"

    def acquire_after_lock() -> str:
        try:
            with managed_session(acquire_connection) as session:
                service = _service_signaling_operation_lock(
                    session,
                    before_operation_lock=acquire_attempted_operation_lock,
                )
                service.acquire_actor_lease(
                    lease_id=acquire_lease_id,
                    execution_id=acquire_execution_id,
                    operation_id=acquire_operation_id,
                    run_id=acquire_run_id,
                    owner_id="owner-acquire",
                    actor_role="constructor",
                    actor_attempt_sequence=1,
                    issued_at=NOW,
                    expires_at=NOW + timedelta(minutes=10),
                )
            return "lease_acquired"
        except OperationRunRevoked:
            return "lease_rejected_revoked"

    with independent_connections(engine) as (revocation_connection, acquire_connection):
        with ThreadPoolExecutor(max_workers=2) as pool:
            revocation_future = pool.submit(revoke_before_acquire)
            acquire_revocation_holds_lock.wait_until_blocked()
            acquire_future = pool.submit(acquire_after_lock)
            assert acquire_attempted_operation_lock.wait(timeout=20)
            assert_transaction_blocked(acquire_future)
            acquire_revocation_holds_lock.release()
            assert revocation_future.result(timeout=20) == "revocation_committed"
            assert acquire_future.result(timeout=20) == "lease_rejected_revoked"

    renew_revocation_id = _next(ids)
    renew_revocation_holds_lock = TransactionGate(
        label="exact revocation owns operation row before lease renewal"
    )
    renew_attempted_operation_lock = Event()

    def revoke_before_renew() -> str:
        with managed_session(revocation_connection) as session:
            WorkflowAuthorityService(session).repo.revoke_operation_run(
                revocation_id=renew_revocation_id,
                generation_id=context["generation_id"],
                operation_id=renew_operation_id,
                owner_id="owner-renew",
                run_id=renew_run_id,
                reason="native exact-revocation wins before lease renewal",
                revoked_at=NOW,
            )
            renew_revocation_holds_lock.block()
        return "revocation_committed"

    def renew_after_lock() -> str:
        try:
            with managed_session(renew_connection) as session:
                service = _service_signaling_operation_lock(
                    session,
                    before_operation_lock=renew_attempted_operation_lock,
                )
                service.renew_lease(
                    lease_id=renewal_lease_id,
                    execution_id=renew_execution_id,
                    run_id=renew_run_id,
                    owner_id="owner-renew",
                    now=NOW + timedelta(minutes=1),
                    new_expiry=NOW + timedelta(minutes=20),
                )
            return "lease_renewed"
        except OperationRunRevoked:
            return "renew_rejected_revoked"

    with independent_connections(engine) as (revocation_connection, renew_connection):
        with ThreadPoolExecutor(max_workers=2) as pool:
            revocation_future = pool.submit(revoke_before_renew)
            renew_revocation_holds_lock.wait_until_blocked()
            renew_future = pool.submit(renew_after_lock)
            assert renew_attempted_operation_lock.wait(timeout=20)
            assert_transaction_blocked(renew_future)
            renew_revocation_holds_lock.release()
            assert revocation_future.result(timeout=20) == "revocation_committed"
            assert renew_future.result(timeout=20) == "renew_rejected_revoked"

    with session_scope(factory) as session:
        assert session.get(wf.ServiceLease, acquire_lease_id) is None
        renewal_lease = session.get(wf.ServiceLease, renewal_lease_id)
        assert renewal_lease is not None
        assert renewal_lease.state == "active"
        assert renewal_lease.lease_revision == 1
        assert renewal_lease.expires_at == NOW + timedelta(minutes=10)
        assert session.get(wf.OperationRunRevocation, acquire_revocation_id) is not None
        assert session.get(wf.OperationRunRevocation, renew_revocation_id) is not None


@pytest.mark.flake_stress
def test_native_claim_commits_before_revocation_rechecks_after_operation_lock(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        task = _import_one(session, ids, context)
        run_id, execution_id, operation_id = _create_operation_authority(
            session,
            ids,
            context=context,
            task_id=task.task_id,
            owner_id="owner-1",
        )
    revocation_id, claim_token = _next(ids), _next(ids)
    claim_holds_lock = TransactionGate(label="execution claim owns operation-row lock")
    revocation_attempted_operation_lock = Event()
    engine = factory.kw["bind"]

    def claim_first() -> str:
        with managed_session(claim_connection) as session:
            WorkflowAuthorityService(session).repo.claim_execution(
                execution_id=execution_id,
                claimant=f"owner-1:{run_id}",
                claim_token=claim_token,
                now=NOW,
                ttl=timedelta(minutes=2),
            )
            claim_holds_lock.block()
        return "claim_committed"

    def revoke_after_lock() -> str:
        try:
            with managed_session(revocation_connection) as session:
                service = _service_signaling_operation_lock(
                    session,
                    before_operation_lock=revocation_attempted_operation_lock,
                )
                service.repo.revoke_operation_run(
                    revocation_id=revocation_id,
                    generation_id=context["generation_id"],
                    operation_id=operation_id,
                    owner_id="owner-1",
                    run_id=run_id,
                    reason="native execution claim wins before exact revocation",
                    revoked_at=NOW,
                )
            return "revocation_committed"
        except ContentionLost as exc:
            assert "active claimed execution" in str(exc)
            return "revocation_rejected_claimed"

    with independent_connections(engine) as (claim_connection, revocation_connection):
        with ThreadPoolExecutor(max_workers=2) as pool:
            claim_future = pool.submit(claim_first)
            claim_holds_lock.wait_until_blocked()
            revocation_future = pool.submit(revoke_after_lock)
            assert revocation_attempted_operation_lock.wait(timeout=20)
            assert_transaction_blocked(revocation_future)
            claim_holds_lock.release()
            assert claim_future.result(timeout=20) == "claim_committed"
            assert revocation_future.result(timeout=20) == "revocation_rejected_claimed"

    with session_scope(factory) as session:
        execution = session.get(wf.CommandExecution, execution_id)
        assert execution is not None and execution.status == "claimed"
        assert session.get(wf.OperationRunRevocation, revocation_id) is None
