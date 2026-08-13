"""Native PostgreSQL discard/prepare serialization regressions."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.command_port import CommandCall, PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService
from dish_pg.workflow import WorkflowAuthorityRepository
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
