from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, select

from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.command_port import CommandCall, PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService
from tests.support.canonical import TASK
from tests.support.postgresql.command import _add_verification_queue
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db

SECRET = b"operation-serialization-secret-32"


def _port(session, ids) -> PostgresCommandPort:
    return PostgresCommandPort(
        session,
        cursor_secret=SECRET,
        uuid_factory=lambda: _next(ids),
        projection_recorder=ProjectionService(
            session, uuid_factory=lambda: _next(ids)
        ),
    )


def _seed_open_operation(factory, ids, context, task_id):
    author_run, admin_run = _next(ids), _next(ids)
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=author_run,
        )
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
        )
        ProjectionService(
            session, uuid_factory=lambda: _next(ids)
        ).activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="discard prepare serialization regression",
            created_at=NOW,
            external_effects_enabled=True,
        )
        started = _port(session, ids).execute(
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
) -> CommandCall:
    arguments = {"task_id": str(task_id), "operation_id": str(operation_id)}
    if command_name == "prepare":
        arguments["file_text"] = TASK
    return CommandCall(
        command_name=command_name,
        arguments=arguments,
        owner_id=owner_id,
        principal_class=principal_class,
        run_id=run_id,
        request_id=request_id,
        now=at,
    )


def _artifact_counts(session, operation_id) -> tuple[int, int, int]:
    cycles = int(
        session.scalar(
            select(func.count()).select_from(wf.VerificationCycle).where(
                wf.VerificationCycle.operation_id == operation_id
            )
        )
        or 0
    )
    steps = int(
        session.scalar(
            select(func.count()).select_from(wf.OperationStep).where(
                wf.OperationStep.operation_id == operation_id
            )
        )
        or 0
    )
    projections = int(
        session.scalar(
            select(func.count())
            .select_from(tx.ProjectionOutboxEvent)
            .join(
                wf.CommandExecution,
                wf.CommandExecution.execution_id
                == tx.ProjectionOutboxEvent.command_execution_id,
            )
            .where(wf.CommandExecution.operation_id == operation_id)
        )
        or 0
    )
    return cycles, steps, projections


def test_discarded_operation_rejects_later_prepare_without_artifacts(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    author_run, admin_run, operation_id = _seed_open_operation(
        factory, ids, context, task_id
    )
    with session_scope(factory) as session:
        discarded = _port(session, ids).execute(
            _call(
                "discard",
                run_id=admin_run,
                request_id=_next(ids),
                task_id=task_id,
                operation_id=operation_id,
                owner_id="Marco",
                principal_class="admin",
                at=NOW + timedelta(seconds=1),
            )
        )
        assert discarded.ok
    with session_scope(factory) as session:
        prepared = _port(session, ids).execute(
            _call(
                "prepare",
                run_id=author_run,
                request_id=_next(ids),
                task_id=task_id,
                operation_id=operation_id,
                owner_id="owner-1",
                principal_class="agent",
                at=NOW + timedelta(seconds=2),
            )
        )
        assert prepared.ok is False
    with session_scope(factory) as session:
        operation = session.get(wf.WorkflowOperation, operation_id)
        assert (operation.lifecycle, operation.phase) == (
            "cancelled_by_marco",
            "cancelled",
        )
        assert _artifact_counts(session, operation_id) == (0, 0, 0)


def test_prepared_operation_rejects_later_discard_and_retains_atomic_intent(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    author_run, admin_run, operation_id = _seed_open_operation(
        factory, ids, context, task_id
    )
    with session_scope(factory) as session:
        prepared = _port(session, ids).execute(
            _call(
                "prepare",
                run_id=author_run,
                request_id=_next(ids),
                task_id=task_id,
                operation_id=operation_id,
                owner_id="owner-1",
                principal_class="agent",
                at=NOW + timedelta(seconds=1),
            )
        )
        assert prepared.ok
    with session_scope(factory) as session:
        discarded = _port(session, ids).execute(
            _call(
                "discard",
                run_id=admin_run,
                request_id=_next(ids),
                task_id=task_id,
                operation_id=operation_id,
                owner_id="Marco",
                principal_class="admin",
                at=NOW + timedelta(seconds=2),
            )
        )
        assert discarded.ok is False
    with session_scope(factory) as session:
        operation = session.get(wf.WorkflowOperation, operation_id)
        assert (operation.lifecycle, operation.phase) == (
            "open",
            "await_verification",
        )
        assert _artifact_counts(session, operation_id) == (1, 1, 2)
