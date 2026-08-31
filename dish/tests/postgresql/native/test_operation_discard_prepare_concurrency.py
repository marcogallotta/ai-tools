"""Native PostgreSQL discard/prepare serialization regressions."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from dish_pg import models
from dish_pg import stage3_models as wf
import dish_pg.test_generation_rollover as rollover_module
from dish_pg import stage5_models as tx
from dish_pg.command_port import CommandCall, PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.test_generation_rollover import _rollover_generation_transaction
from dish_pg.transition import ProjectionService, ShadowService
from dish_pg.workflow import StaleAuthorityError, WorkflowAuthorityRepository
from tests.support.canonical import TASK
from tests.support.postgresql.command import _add_verification_queue
from tests.support.postgresql.concurrency import (
    TransactionGate,
    assert_transaction_blocked,
    independent_connections,
    managed_session,
)
from tests.support.postgresql.core import core_db
from tests.support.postgresql.projection_attempts import native_workflow_db
from tests.support.postgresql.workflow import NOW, _next, _register_run

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]
SECRET = b"native-operation-concurrency-secret"


@pytest.fixture(autouse=True)
def _require_native_postgresql(request: pytest.FixtureRequest) -> None:
    if not request.config.getoption("--postgresql"):
        pytest.skip("native PostgreSQL concurrency certification requires --postgresql")



class _DelayedOperationRepository(WorkflowAuthorityRepository):
    def __init__(self, *args, gate: TransactionGate, **kwargs):
        super().__init__(*args, **kwargs)
        self._gate = gate

    def _locked_operation(self, *, generation_id, operation_id):
        self._gate.block()
        return super()._locked_operation(
            generation_id=generation_id,
            operation_id=operation_id,
        )


class _DelayedOperationPort(PostgresCommandPort):
    def __init__(
        self,
        *args,
        gate: TransactionGate,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.workflow.repo = _DelayedOperationRepository(self.session, gate=gate)


def _command_port(session, *, delayed=None) -> PostgresCommandPort:
    cls = _DelayedOperationPort if delayed is not None else PostgresCommandPort
    kwargs = dict(
        cursor_secret=SECRET,
        uuid_factory=uuid.uuid4,
        projection_recorder=ProjectionService(session, uuid_factory=uuid.uuid4),
    )
    if delayed is not None:
        kwargs.update(gate=delayed)
    return cls(session, **kwargs)


def _call(
    command_name,
    *,
    run_id,
    request_id,
    task_id,
    operation_id,
    owner_id,
    principal_class,
    at,
):
    arguments = {"task_id": str(task_id), "operation_id": str(operation_id)}
    if command_name == "prepare":
        arguments.update(
            file_text=TASK,
            agent="claude",
            model="native-concurrency-test",
        )
    return CommandCall(
        command_name=command_name,
        arguments=arguments,
        owner_id=owner_id,
        principal_class=principal_class,
        run_id=run_id,
        request_id=request_id,
        now=at,
    )


def _seed_open_operation(factory, ids, context, task_id):
    author_run, admin_run = _next(ids), _next(ids)
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
        )
        ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="native PostgreSQL discard prepare race",
            created_at=NOW,
            external_effects_enabled=True,
        )
        started = _command_port(session).execute(
            CommandCall(
                command_name="start",
                arguments={
                    "task_id": str(task_id),
                    "kind": "initial",
                    "agent": "claude",
                },
                owner_id="owner-1",
                principal_class="agent",
                run_id=author_run,
                request_id=_next(ids),
                now=NOW,
            )
        )
        assert started.ok
        return author_run, admin_run, uuid.UUID(started.data["operation_id"])


def test_native_discard_commits_before_prepare_lock_and_leaves_no_actionable_intent(
    core_db,
) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    author_run, admin_run, operation_id = _seed_open_operation(
        factory, ids, context, task_id
    )
    gate = TransactionGate(label="prepare waits before operation lock")
    engine = factory.kw["bind"]

    def delayed_prepare():
        with managed_session(delayed_connection) as session:
            return _command_port(session, delayed=gate).execute(
                _call(
                    "prepare",
                    run_id=author_run,
                    request_id=uuid.uuid4(),
                    task_id=task_id,
                    operation_id=operation_id,
                    owner_id="owner-1",
                    principal_class="agent",
                    at=NOW + timedelta(seconds=1),
                )
            )

    with independent_connections(engine) as (delayed_connection, winner_connection):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(delayed_prepare)
            gate.wait_until_blocked()
            assert_transaction_blocked(future)
            try:
                with managed_session(winner_connection) as session:
                    discarded = _command_port(session).execute(
                        _call(
                            "discard",
                            run_id=admin_run,
                            request_id=uuid.uuid4(),
                            task_id=task_id,
                            operation_id=operation_id,
                            owner_id="Marco",
                            principal_class="admin",
                            at=NOW + timedelta(seconds=2),
                        )
                    )
                    assert discarded.ok
            finally:
                gate.release()
            prepared = future.result(timeout=20)
    assert prepared.ok is False

    with session_scope(factory) as session:
        operation = session.get(wf.WorkflowOperation, operation_id)
        assert operation.lifecycle == "cancelled_by_marco"
        assert operation.phase == "cancelled"
        assert session.scalar(
            select(func.count()).select_from(wf.VerificationCycle).where(
                wf.VerificationCycle.operation_id == operation_id
            )
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(wf.OperationStep).where(
                wf.OperationStep.operation_id == operation_id
            )
        ) == 0
        assert session.scalar(
            select(func.count())
            .select_from(tx.ProjectionOutboxEvent)
            .join(
                wf.CommandExecution,
                wf.CommandExecution.execution_id
                == tx.ProjectionOutboxEvent.command_execution_id,
            )
            .where(wf.CommandExecution.operation_id == operation_id)
        ) == 0


def test_native_prepare_commits_before_discard_lock_and_discard_cannot_cancel(
    core_db,
) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    author_run, admin_run, operation_id = _seed_open_operation(
        factory, ids, context, task_id
    )
    gate = TransactionGate(label="discard waits before operation lock")
    engine = factory.kw["bind"]

    def delayed_discard():
        with managed_session(delayed_connection) as session:
            return _command_port(session, delayed=gate).execute(
                _call(
                    "discard",
                    run_id=admin_run,
                    request_id=uuid.uuid4(),
                    task_id=task_id,
                    operation_id=operation_id,
                    owner_id="Marco",
                    principal_class="admin",
                    at=NOW + timedelta(seconds=2),
                )
            )

    with independent_connections(engine) as (delayed_connection, winner_connection):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(delayed_discard)
            gate.wait_until_blocked()
            assert_transaction_blocked(future)
            try:
                with managed_session(winner_connection) as session:
                    prepared = _command_port(session).execute(
                        _call(
                            "prepare",
                            run_id=author_run,
                            request_id=uuid.uuid4(),
                            task_id=task_id,
                            operation_id=operation_id,
                            owner_id="owner-1",
                            principal_class="agent",
                            at=NOW + timedelta(seconds=1),
                        )
                    )
                    assert prepared.ok
            finally:
                gate.release()
            discarded = future.result(timeout=20)
    assert discarded.ok is False

    with session_scope(factory) as session:
        operation = session.get(wf.WorkflowOperation, operation_id)
        assert operation.lifecycle == "open"
        assert operation.phase == "await_verification"
        assert session.scalar(
            select(func.count()).select_from(wf.VerificationCycle).where(
                wf.VerificationCycle.operation_id == operation_id
            )
        ) == 1
        assert session.scalar(
            select(func.count()).select_from(wf.OperationStep).where(
                wf.OperationStep.operation_id == operation_id
            )
        ) == 1
        assert session.scalar(
            select(func.count())
            .select_from(tx.ProjectionOutboxEvent)
            .join(
                wf.CommandExecution,
                wf.CommandExecution.execution_id
                == tx.ProjectionOutboxEvent.command_execution_id,
            )
            .where(wf.CommandExecution.operation_id == operation_id)
        ) == 2


SOURCE_COMMIT = "f" * 40
ROLLOVER_CURSOR_SECRET = b"generation-liveness-native-secret"


class _AdmissionGateRepository(WorkflowAuthorityRepository):
    """Pause after mutation admission has acquired its generation-liveness fence."""

    def __init__(self, session: Session, *, gate: TransactionGate) -> None:
        super().__init__(session)
        self._gate = gate

    def admit_request(self, spec):
        admission = super().admit_request(spec)
        self._gate.block()
        return admission


class _AdmissionGatePort(PostgresCommandPort):
    def __init__(self, *args, gate: TransactionGate, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.workflow.repo = _AdmissionGateRepository(self.session, gate=gate)


class _RolloverLockNotifyingSession(Session):
    """Signal immediately before rollover attempts its first authoritative row lock."""

    def __init__(self, *args, before_first_scalar: Event, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._before_first_scalar = before_first_scalar
        self._first_scalar_seen = False

    def scalar(self, statement, *args, **kwargs):
        if not self._first_scalar_seen:
            self._first_scalar_seen = True
            self._before_first_scalar.set()
        return super().scalar(statement, *args, **kwargs)


def _rollover_port(session: Session, *, gate: TransactionGate | None = None) -> PostgresCommandPort:
    cls = _AdmissionGatePort if gate is not None else PostgresCommandPort
    kwargs = {
        "cursor_secret": ROLLOVER_CURSOR_SECRET,
        "uuid_factory": uuid.uuid4,
        "projection_recorder": ProjectionService(session, uuid_factory=uuid.uuid4),
    }
    if gate is not None:
        kwargs["gate"] = gate
    return cls(session, **kwargs)


def _install_synthetic_contamination(monkeypatch, factory, context):
    """Isolate the handoff race from the separately tested TEST-fixture signature gate."""

    with session_scope(factory) as session:
        baseline = ShadowService(session, uuid_factory=uuid.uuid4).create_baseline(
            generation_id=context["generation_id"],
            source_generation_identity="native-rollover-race",
            source_commit=SOURCE_COMMIT,
            created_at=NOW,
        )
        epoch = ProjectionService(session, uuid_factory=uuid.uuid4).activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="native generation-liveness race",
            created_at=NOW,
            external_effects_enabled=True,
        )

    candidate_id = uuid.uuid4()
    cutover_run_id = uuid.uuid4()
    reservation_id = uuid.uuid4()
    evidence = rollover_module.ContaminationEvidence(
        candidate_id=candidate_id,
        cutover_run_id=cutover_run_id,
        reservation_id=reservation_id,
        shadow_baseline_id=baseline.shadow_baseline_id,
        projection_epoch_id=epoch.projection_epoch_id,
    )

    def synthetic_contamination(
        _session,
        predecessor_generation_id,
        *,
        contaminated_candidate_id,
        contaminated_cutover_run_id,
        contaminated_reservation_id,
    ):
        assert predecessor_generation_id == context["generation_id"]
        assert contaminated_candidate_id == candidate_id
        assert contaminated_cutover_run_id == cutover_run_id
        assert contaminated_reservation_id == reservation_id
        return evidence

    monkeypatch.setattr(
        rollover_module,
        "_contamination_evidence",
        synthetic_contamination,
    )
    return candidate_id, cutover_run_id, reservation_id


def _current_content(session: Session, generation_id: uuid.UUID, task_id: uuid.UUID):
    state = session.get(models.DishState, (generation_id, task_id))
    assert state is not None
    version = session.get(models.ContentVersion, state.current_content_version_id)
    assert version is not None
    return version


def _rollover(
    session: Session,
    *,
    context,
    verification_section_id,
    candidate_id,
    cutover_run_id,
    reservation_id,
):
    return _rollover_generation_transaction(
        session,
        predecessor_generation_id=context["generation_id"],
        contaminated_candidate_id=candidate_id,
        contaminated_cutover_run_id=cutover_run_id,
        contaminated_reservation_id=reservation_id,
        research_queue_section_id=context["section_id"],
        verification_queue_section_id=verification_section_id,
        source_commit=SOURCE_COMMIT,
        uuid_factory=uuid.uuid4,
        clock=lambda: NOW + timedelta(hours=1),
    )


def test_operation_bound_command_commit_precedes_rollover_and_is_cloned_to_successor(
    core_db,
    monkeypatch,
) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    with session_scope(factory) as session:
        verification_section_id = _add_verification_queue(session, ids, context)
    candidate_id, cutover_run_id, reservation_id = _install_synthetic_contamination(
        monkeypatch, factory, context
    )

    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
        )
        started = _rollover_port(session).execute(
            CommandCall(
                command_name="start",
                arguments={"task_id": str(task_id), "kind": "initial", "agent": "claude"},
                owner_id="owner-1",
                principal_class="agent",
                run_id=run_id,
                request_id=uuid.uuid4(),
                now=NOW,
            )
        )
        assert started.ok
        operation_id = uuid.UUID(started.data["operation_id"])

    request_id = uuid.uuid4()
    gate = TransactionGate(label="operation command holds shared generation-liveness fence")
    rollover_lock_attempted = Event()
    engine = factory.kw["bind"]

    with independent_connections(engine) as (command_connection, rollover_connection):
        def prepare_command():
            with managed_session(command_connection) as session:
                return _rollover_port(session, gate=gate).execute(
                    CommandCall(
                        command_name="prepare",
                        arguments={
                            "task_id": str(task_id),
                            "operation_id": str(operation_id),
                            "file_text": TASK,
                            "agent": "claude",
                            "model": "native-generation-liveness",
                        },
                        owner_id="owner-1",
                        principal_class="agent",
                        run_id=run_id,
                        request_id=request_id,
                        now=NOW + timedelta(seconds=1),
                    )
                )

        def rollover():
            session = _RolloverLockNotifyingSession(
                bind=rollover_connection,
                expire_on_commit=False,
                before_first_scalar=rollover_lock_attempted,
            )
            try:
                result = _rollover(
                    session,
                    context=context,
                    verification_section_id=verification_section_id,
                    candidate_id=candidate_id,
                    cutover_run_id=cutover_run_id,
                    reservation_id=reservation_id,
                )
                session.commit()
                return result
            except BaseException:
                session.rollback()
                raise
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            command_future = pool.submit(prepare_command)
            gate.wait_until_blocked()
            rollover_future = pool.submit(rollover)
            assert rollover_lock_attempted.wait(timeout=30)
            assert_transaction_blocked(rollover_future)
            gate.release()
            prepared = command_future.result()
            result = rollover_future.result()

    assert prepared.ok
    assert prepared.code == "OK"
    with session_scope(factory) as session:
        predecessor = session.get(models.AuthorityGeneration, context["generation_id"])
        successor = session.get(models.AuthorityGeneration, result.generation_id)
        assert predecessor is not None and predecessor.status == "retired"
        assert successor is not None and successor.status == "active"
        predecessor_content = _current_content(session, context["generation_id"], task_id)
        successor_content = _current_content(session, result.generation_id, task_id)
        assert successor_content.content_identity == predecessor_content.content_identity
        assert successor_content.body == predecessor_content.body
        assert successor_content.title == predecessor_content.title
        outcome = session.scalar(
            select(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == request_id
            )
        )
        assert outcome is not None and outcome.outcome_class == "success"


def test_rollover_precedes_no_projection_command_and_stale_predecessor_cannot_succeed(
    core_db,
    monkeypatch,
) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    with session_scope(factory) as session:
        verification_section_id = _add_verification_queue(session, ids, context)
    candidate_id, cutover_run_id, reservation_id = _install_synthetic_contamination(
        monkeypatch, factory, context
    )

    author_run_id = _next(ids)
    admin_run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=author_run_id,
        )
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run_id,
            owner="Marco",
            agent="marco",
        )
        started = _rollover_port(session).execute(
            CommandCall(
                command_name="start",
                arguments={"task_id": str(task_id), "kind": "initial", "agent": "claude"},
                owner_id="owner-1",
                principal_class="agent",
                run_id=author_run_id,
                request_id=uuid.uuid4(),
                now=NOW,
            )
        )
        assert started.ok
        lease_id = uuid.UUID(started.data["lease_id"])

    request_id = uuid.uuid4()
    engine = factory.kw["bind"]
    with independent_connections(engine) as (stale_connection, rollover_connection):
        stale_session = Session(bind=stale_connection, expire_on_commit=False)
        try:
            stale_generation = stale_session.scalar(
                select(models.AuthorityGeneration).where(
                    models.AuthorityGeneration.generation_id == context["generation_id"]
                )
            )
            assert stale_generation is not None and stale_generation.status == "active"
            stale_lease = stale_session.get(wf.ServiceLease, lease_id)
            assert stale_lease is not None and stale_lease.state == "active"

            with managed_session(rollover_connection) as session:
                result = _rollover(
                    session,
                    context=context,
                    verification_section_id=verification_section_id,
                    candidate_id=candidate_id,
                    cutover_run_id=cutover_run_id,
                    reservation_id=reservation_id,
                )

            port = _rollover_port(stale_session)
            # Model a process that selected predecessor G before rollover committed. The
            # admission fence must refresh that cached row under FOR SHARE and reject it.
            port.reads = SimpleNamespace(active_generation=lambda: stale_generation)
            with pytest.raises(StaleAuthorityError, match="generation is not active"):
                port.execute(
                    CommandCall(
                        command_name="expire-lease",
                        arguments={"task_id": str(task_id), "lease_id": str(lease_id)},
                        owner_id="Marco",
                        principal_class="admin",
                        run_id=admin_run_id,
                        request_id=request_id,
                        now=NOW + timedelta(hours=1, seconds=1),
                    )
                )
            stale_session.rollback()
        finally:
            stale_session.close()

    with session_scope(factory) as session:
        successor = session.get(models.AuthorityGeneration, result.generation_id)
        assert successor is not None and successor.status == "active"
        lease = session.get(wf.ServiceLease, lease_id)
        assert lease is not None and lease.state == "active"
        assert session.get(wf.ServiceRequest, request_id) is None
        assert session.scalar(
            select(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == request_id
            )
        ) is None


class _TaskFenceGateRepository(WorkflowAuthorityRepository):
    def __init__(
        self,
        *args,
        before_lock: TransactionGate | None = None,
        after_lock: TransactionGate | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._before_lock = before_lock
        self._after_lock = after_lock

    def capture_task_fence(self, **kwargs):
        if self._before_lock is not None:
            self._before_lock.block()
        return super().capture_task_fence(**kwargs)

    def assert_task_fence(self, execution_id):
        state = super().assert_task_fence(execution_id)
        if self._after_lock is not None:
            self._after_lock.block()
        return state


def _task_fence_port(
    session,
    *,
    before_lock: TransactionGate | None = None,
    after_lock: TransactionGate | None = None,
) -> PostgresCommandPort:
    port = _command_port(session)
    if before_lock is not None or after_lock is not None:
        port.workflow.repo = _TaskFenceGateRepository(
            session,
            before_lock=before_lock,
            after_lock=after_lock,
        )
    return port


def _seed_task_fence_runs(factory, ids, context):
    start_run, cooked_run = _next(ids), _next(ids)
    with session_scope(factory) as session:
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=start_run,
        )
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=cooked_run,
        )
        ProjectionService(session, uuid_factory=lambda: _next(ids)).activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="native PostgreSQL task-fence concurrency",
            created_at=NOW,
            external_effects_enabled=True,
        )
    return start_run, cooked_run


def test_native_task_transition_commits_before_final_fence_and_stale_start_rejects(
    core_db,
) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    start_run, cooked_run = _seed_task_fence_runs(factory, ids, context)
    before_lock = TransactionGate(label="stale start waits before final task-fence lock")
    engine = factory.kw["bind"]

    def stale_start():
        with managed_session(stale_connection) as session:
            stale_state = session.get(
                models.DishState, (context["generation_id"], task_id)
            )
            stale_membership = session.get(
                models.TaskMembershipHead, (context["generation_id"], task_id)
            )
            assert stale_state is not None and stale_membership is not None
            return _task_fence_port(session, before_lock=before_lock).execute(
                CommandCall(
                    command_name="start",
                    arguments={
                        "task_id": str(task_id),
                        "kind": "initial",
                        "agent": "claude",
                    },
                    owner_id="owner-1",
                    principal_class="agent",
                    run_id=start_run,
                    request_id=uuid.uuid4(),
                    now=NOW + timedelta(seconds=1),
                )
            )

    with independent_connections(engine) as (stale_connection, winner_connection):
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(stale_start)
            before_lock.wait_until_blocked()
            assert_transaction_blocked(future)

            with managed_session(winner_connection) as session:
                winner = _task_fence_port(session).execute(
                    CommandCall(
                        command_name="cooked",
                        arguments={"task_id": str(task_id)},
                        owner_id="owner-1",
                        principal_class="agent",
                        run_id=cooked_run,
                        request_id=uuid.uuid4(),
                        now=NOW + timedelta(seconds=2),
                    )
                )
                assert winner.ok is True

            before_lock.release()
            stale = future.result()

    assert stale.ok is False
    assert stale.code == "AUTHORITY_MISMATCH"
    assert "task fence is stale" in stale.data["message"]
    with session_scope(factory) as session:
        state = session.get(models.DishState, (context["generation_id"], task_id))
        assert state is not None and state.completed is True
        assert session.scalar(
            select(func.count())
            .select_from(wf.WorkflowOperation)
            .where(wf.WorkflowOperation.task_id == task_id)
        ) == 0


def test_native_start_holds_final_task_fence_until_commit_and_cooked_observes_operation(
    core_db,
) -> None:
    factory, ids, context, task_id = native_workflow_db(core_db)
    start_run, cooked_run = _seed_task_fence_runs(factory, ids, context)
    after_lock = TransactionGate(label="start holds final task-fence lock before mutation")
    cooked_entered = Event()
    engine = factory.kw["bind"]

    def locked_start():
        with managed_session(start_connection) as session:
            return _task_fence_port(session, after_lock=after_lock).execute(
                CommandCall(
                    command_name="start",
                    arguments={
                        "task_id": str(task_id),
                        "kind": "initial",
                        "agent": "claude",
                    },
                    owner_id="owner-1",
                    principal_class="agent",
                    run_id=start_run,
                    request_id=uuid.uuid4(),
                    now=NOW + timedelta(seconds=1),
                )
            )

    def blocked_cooked():
        with managed_session(cooked_connection) as session:
            cooked_entered.set()
            return _task_fence_port(session).execute(
                CommandCall(
                    command_name="cooked",
                    arguments={"task_id": str(task_id)},
                    owner_id="owner-1",
                    principal_class="agent",
                    run_id=cooked_run,
                    request_id=uuid.uuid4(),
                    now=NOW + timedelta(seconds=2),
                )
            )

    with independent_connections(engine) as (start_connection, cooked_connection):
        with ThreadPoolExecutor(max_workers=2) as pool:
            start_future = pool.submit(locked_start)
            after_lock.wait_until_blocked()
            assert_transaction_blocked(start_future)

            cooked_future = pool.submit(blocked_cooked)
            assert cooked_entered.wait(timeout=10)
            assert_transaction_blocked(cooked_future)

            after_lock.release()
            started = start_future.result()
            cooked = cooked_future.result()

    assert started.ok is True
    assert cooked.ok is False
    assert cooked.code == "TASK_NOT_RESTING"
    with session_scope(factory) as session:
        state = session.get(models.DishState, (context["generation_id"], task_id))
        assert state is not None and state.completed is False
        operations = list(
            session.scalars(
                select(wf.WorkflowOperation).where(
                    wf.WorkflowOperation.task_id == task_id,
                    wf.WorkflowOperation.lifecycle == "open",
                )
            )
        )
        assert len(operations) == 1
        assert operations[0].operation_id == uuid.UUID(started.data["operation_id"])
