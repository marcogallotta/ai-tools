from __future__ import annotations

import io
import threading
import time
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, event, func, inspect, select, text
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.workflow import (
    ContentionLost,
    ExecutionSpec,
    RequestIdentityConflict,
    RequestSpec,
    StaleAuthorityError,
    StoredOutcome,
    WorkflowAuthorityRepository,
    WorkflowAuthorityService,
)
from tests.support.postgresql.workflow import (
    _admit,
    _execution,
    _next,
    _register_run,
    workflow_db,
)


ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc)


def test_stage3_schema_adds_named_authorities_but_not_projection() -> None:
    assert set(wf.STAGE3_TABLE_NAMES).issubset(models.Base.metadata.tables)
    required = {
        "service_requests",
        "service_request_outcomes",
        "command_executions",
        "task_execution_fences",
        "workflow_operations",
        "service_leases",
        "planning_intent_challenges",
        "marco_authorization_grants",
        "verification_cycles",
        "evidence_holds",
        "human_review_requirements",
        "abandonment_attempts",
        "governed_audit_events",
        "invocation_audit_obligations",
    }
    assert required.issubset(wf.STAGE3_TABLE_NAMES)
    assert {
        "projection_outbox_events",
        "projection_attempts",
        "shadow_envelopes",
    }.isdisjoint(wf.STAGE3_TABLE_NAMES)


def test_stage3_migration_renders_guards_and_reaches_head(tmp_path: Path) -> None:
    config = Config(str(ROOT / "alembic.ini"))
    buffer = io.StringIO()
    config.attributes["output_buffer"] = buffer
    command.upgrade(config, "0003_workflow_authority", sql=True)
    rendered = buffer.getvalue()
    assert "CREATE TABLE service_requests" in rendered
    assert "CREATE TABLE workflow_operations" in rendered
    assert "CREATE TABLE invocation_audit_obligations" in rendered
    assert "dish_reject_immutable_workflow_authority" in rendered
    assert "dish_validate_request_run_generation" in rendered

    path = tmp_path / "stage3.sqlite3"
    online = Config(str(ROOT / "alembic.ini"))
    online.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    command.upgrade(online, "0003_workflow_authority")
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        assert set(wf.STAGE3_TABLE_NAMES).issubset(inspect(engine).get_table_names())
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
                "0003_workflow_authority"
            )
    finally:
        engine.dispose()


def test_exact_request_replay_and_identity_conflict(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id = _next(ids)
    request_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        service = WorkflowAuthorityService(session)
        first = _admit(
            service,
            request_id=request_id,
            generation_id=context["generation_id"],
            run_id=run_id,
        )
        replay = _admit(
            service,
            request_id=request_id,
            generation_id=context["generation_id"],
            run_id=run_id,
        )
        assert first.replayed is False
        assert replay.replayed is True
        assert replay.outcome is None
        with pytest.raises(RequestIdentityConflict):
            _admit(
                service,
                request_id=request_id,
                generation_id=context["generation_id"],
                run_id=run_id,
                payload={"task": "different"},
            )


def test_planning_first_call_creates_only_request_challenge_and_outcome(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    run_id, request_id, challenge_id = (_next(ids), _next(ids), _next(ids))
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        service = WorkflowAuthorityService(session)
        _admit(
            service,
            request_id=request_id,
            generation_id=context["generation_id"],
            run_id=run_id,
        )
        service.issue_planning_challenge(
            challenge_id=challenge_id,
            issuing_request_id=request_id,
            task_id=task_id,
            issued_at=NOW,
        )
        service.repo.record_outcome(
            request_id=request_id,
            outcome=StoredOutcome(
                outcome_id=_next(ids),
                outcome_class="rule_error",
                result_code="CONFIRMATION_REQUIRED",
                http_status=409,
                result_payload={"challenge_id": str(challenge_id)},
                immutable_success=False,
                recorded_at=NOW,
            ),
            execution_id=None,
            audit_event_id=_next(ids),
            audit_event_type="planning_intent_challenge_issued",
            actor="owner-1",
            audit_payload={"challenge_id": str(challenge_id)},
            task_id=task_id,
            operation_id=None,
            obligation_id=_next(ids),
            invocation_metadata={"surface": "action"},
        )
        assert session.scalar(select(func.count()).select_from(wf.CommandExecution)) == 0
        assert session.scalar(select(func.count()).select_from(wf.WorkflowOperation)) == 0
        assert session.scalar(select(func.count()).select_from(wf.ServiceLease)) == 0


def test_task_and_operation_fences_reject_stale_execution(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    run_id, request_id, execution_id, operation_id = (
        _next(ids),
        _next(ids),
        _next(ids),
        _next(ids),
    )
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        service = WorkflowAuthorityService(session)
        _admit(service, request_id=request_id, generation_id=context["generation_id"], run_id=run_id)
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
            operation_id=operation_id,
            execution_id=execution_id,
            task_id=task_id,
            kind="initial",
            phase="prepare_required",
            persisted_actions=["prepare"],
            created_at=NOW,
        )
        service.repo.capture_operation_fence(
            execution_id=execution_id, operation_id=operation_id, at=NOW
        )
        head = session.get(models.TaskAuthorityHead, (context["generation_id"], task_id))
        assert head is not None
        head.task_revision += 1
        operation.phase = "await_verification"
        operation.operation_revision += 1
        session.flush()
        with pytest.raises(StaleAuthorityError, match="task fence"):
            service.repo.assert_task_fence(execution_id)
        with pytest.raises(StaleAuthorityError, match="operation fence"):
            service.repo.assert_operation_fence(execution_id)


def test_outcome_audit_and_invocation_obligation_are_atomic(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
    with pytest.raises(RuntimeError, match="rollback"):
        with session_scope(factory) as session:
            _register_run(session, generation_id=context["generation_id"], run_id=run_id)
            service = WorkflowAuthorityService(session)
            _admit(service, request_id=request_id, generation_id=context["generation_id"], run_id=run_id)
            _execution(
                service,
                execution_id=execution_id,
                request_id=request_id,
                generation_id=context["generation_id"],
                task_id=task_id,
                binding_id=context["binding_id"],
            )
            service.repo.record_outcome(
                request_id=request_id,
                outcome=StoredOutcome(
                    outcome_id=_next(ids),
                    outcome_class="success",
                    result_code="OK",
                    http_status=200,
                    result_payload={"ok": True},
                    immutable_success=True,
                    recorded_at=NOW,
                ),
                execution_id=execution_id,
                audit_event_id=_next(ids),
                audit_event_type="command_committed",
                actor="owner-1",
                audit_payload={"command": "start"},
                task_id=task_id,
                operation_id=None,
                obligation_id=_next(ids),
                invocation_metadata={"surface": "action"},
            )
            raise RuntimeError("rollback")
    with session_scope(factory) as session:
        assert session.get(wf.ServiceRequest, request_id) is None
        assert session.scalar(select(func.count()).select_from(wf.GovernedAuditEvent)) == 0
        assert session.scalar(select(func.count()).select_from(wf.InvocationAuditObligation)) == 0


def test_stale_generation_run_cannot_admit_new_request(workflow_db) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        generation = session.get(models.AuthorityGeneration, context["generation_id"])
        assert generation is not None
        generation.status = "retired"
        generation.retired_at = NOW
    with session_scope(factory) as session:
        service = WorkflowAuthorityService(session)
        with pytest.raises(StaleAuthorityError):
            _admit(
                service,
                request_id=_next(ids),
                generation_id=context["generation_id"],
                run_id=run_id,
            )
