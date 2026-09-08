"""Workflow authority fixture shared by PostgreSQL behavior tests."""
from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import models
from dish_pg.database import session_scope
from dish_pg.workflow import ExecutionSpec, RequestSpec, WorkflowAuthorityService
from tests.support.postgresql.core import _bootstrap_registry, _import_one, _uuid_stream

NOW = datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc)


def _next(ids: Iterator[uuid.UUID]) -> uuid.UUID:
    return next(ids)


@pytest.fixture
def workflow_db(tmp_path: Path):
    path = tmp_path / "workflow.sqlite3"
    engine = create_engine(
        f"sqlite+pysqlite:///{path}",
        future=True,
        connect_args={"timeout": 30, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def _configure(dbapi_connection, _connection_record) -> None:
        dbapi_connection.execute("PRAGMA foreign_keys = ON")
        dbapi_connection.execute("PRAGMA journal_mode = WAL")
        dbapi_connection.execute("PRAGMA busy_timeout = 30000")

    models.Base.metadata.create_all(engine)
    factory = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )
    ids = _uuid_stream()
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        task = _import_one(session, ids, context)
    yield factory, ids, context, task.task_id
    engine.dispose()


def _register_run(
    session: Session,
    *,
    generation_id: uuid.UUID,
    run_id: uuid.UUID,
    owner: str = "owner-1",
    agent: str = "claude",
) -> None:
    WorkflowAuthorityService(session).register_run(
        run_id=run_id,
        generation_id=generation_id,
        owner_id=owner,
        agent=agent,
        capability_digest=run_id.bytes + run_id.bytes,
        registered_at=NOW,
    )


def _simulate_future_unarchive(
    session: Session,
    ids: Iterator[uuid.UUID],
    context: dict[str, uuid.UUID],
    task_id: uuid.UUID,
) -> None:
    """Expose an unarchived state through the existing scalar-authority contract."""
    run_id = _next(ids)
    request_id = _next(ids)
    execution_id = _next(ids)
    occurred_at = NOW + timedelta(microseconds=1)
    _register_run(
        session,
        generation_id=context["generation_id"],
        run_id=run_id,
        owner="Marco",
    )
    service = WorkflowAuthorityService(session)
    _admit(
        service,
        request_id=request_id,
        generation_id=context["generation_id"],
        run_id=run_id,
        command="future-unarchive-fixture",
        payload={"task_id": str(task_id)},
        owner="Marco",
        principal="admin",
    )
    execution = _execution(
        service,
        execution_id=execution_id,
        request_id=request_id,
        generation_id=context["generation_id"],
        task_id=task_id,
        binding_id=context["binding_id"],
        command="future-unarchive-fixture",
    )
    execution.status = "committed"
    execution.terminal_at = occurred_at

    state = session.get(models.DishState, (context["generation_id"], task_id))
    assert state is not None and state.archived_at is not None
    next_version = state.dish_version + 1
    session.add(
        models.DishMutationReceipt(
            generation_id=context["generation_id"],
            task_id=task_id,
            dish_version=next_version,
            source_route="command_execution",
            import_run_id=None,
            command_execution_id=execution_id,
            content_changed=False,
            placement_changed=False,
            completion_changed=False,
            archive_changed=True,
            occurred_at=occurred_at,
        )
    )
    session.flush()
    state.dish_version = next_version
    state.archived_at = None
    state.updated_at = occurred_at
    session.flush()


def _admit(
    service: WorkflowAuthorityService,
    *,
    request_id: uuid.UUID,
    generation_id: uuid.UUID,
    run_id: uuid.UUID,
    command: str = "start",
    payload: dict | None = None,
    owner: str = "owner-1",
    principal: str = "agent",
):
    return service.admit_request(
        RequestSpec(
            request_id=request_id,
            generation_id=generation_id,
            run_id=run_id,
            owner_id=owner,
            principal_class=principal,
            command_name=command,
            canonical_payload=payload or {"task": "fixture"},
            protocol_release="protocol-1",
            dish_release="dish-42619b9",
            admitted_at=NOW,
        )
    )


def _execution(
    service: WorkflowAuthorityService,
    *,
    execution_id: uuid.UUID,
    request_id: uuid.UUID,
    generation_id: uuid.UUID,
    task_id: uuid.UUID,
    binding_id: uuid.UUID,
    command: str = "start",
):
    return service.begin_execution(
        ExecutionSpec(
            execution_id=execution_id,
            request_id=request_id,
            generation_id=generation_id,
            task_id=task_id,
            operation_id=None,
            command_name=command,
            transaction_profile="L",
            canonical_intent={"command": command},
            pinned_inputs={"now": NOW.isoformat()},
            contract_binding_id=binding_id,
            admitted_at=NOW,
        )
    )

def _claimed_execution(
    session: Session,
    ids: Iterator[uuid.UUID],
    context: dict[str, uuid.UUID],
    task_id: uuid.UUID,
    *,
    command_name: str = "prepare",
) -> uuid.UUID:
    run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
    _register_run(session, generation_id=context["generation_id"], run_id=run_id)
    workflow = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids))
    _admit(
        workflow,
        request_id=request_id,
        generation_id=context["generation_id"],
        run_id=run_id,
        command=command_name,
        payload={"task_id": str(task_id)},
    )
    _execution(
        workflow,
        execution_id=execution_id,
        request_id=request_id,
        generation_id=context["generation_id"],
        task_id=task_id,
        binding_id=context["binding_id"],
        command=command_name,
    )
    workflow.repo.claim_execution(
        execution_id=execution_id,
        claimant=f"owner-1:{run_id}",
        claim_token=_next(ids),
        now=NOW,
        ttl=timedelta(minutes=2),
    )
    return execution_id
