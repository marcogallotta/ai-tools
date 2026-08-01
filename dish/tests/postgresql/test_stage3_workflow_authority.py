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
from tests.postgresql.test_stage2_core_authority import _bootstrap_registry, _import_one, _uuid_stream

ROOT = Path(__file__).resolve().parents[2]
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


def test_ten_way_same_task_actor_lease_has_one_winner(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    contenders: list[tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, str]] = []
    with session_scope(factory) as session:
        for index in range(10):
            run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
            owner = f"owner-{index}"
            _register_run(
                session,
                generation_id=context["generation_id"],
                run_id=run_id,
                owner=owner,
            )
            service = WorkflowAuthorityService(session)
            _admit(
                service,
                request_id=request_id,
                generation_id=context["generation_id"],
                run_id=run_id,
                owner=owner,
            )
            _execution(
                service,
                execution_id=execution_id,
                request_id=request_id,
                generation_id=context["generation_id"],
                task_id=task_id,
                binding_id=context["binding_id"],
            )
            contenders.append((run_id, request_id, execution_id, _next(ids), owner))
        owner_execution = contenders[0][2]
        service = WorkflowAuthorityService(session)
        service.repo.capture_task_fence(
            execution_id=owner_execution,
            generation_id=context["generation_id"],
            task_id=task_id,
            at=NOW,
        )
        operation = service.create_operation(
            operation_id=_next(ids),
            execution_id=owner_execution,
            task_id=task_id,
            kind="initial",
            phase="prepare_required",
            persisted_actions=["prepare"],
            created_at=NOW,
        )
        operation_id = operation.operation_id

    barrier = threading.Barrier(10)

    def contend(index: int) -> str:
        run_id, _request_id, execution_id, lease_id, owner = contenders[index]
        barrier.wait()
        for attempt in range(5):
            try:
                with session_scope(factory) as session:
                    service = WorkflowAuthorityService(session)
                    service.acquire_actor_lease(
                        lease_id=lease_id,
                        execution_id=execution_id,
                        operation_id=operation_id,
                        run_id=run_id,
                        owner_id=owner,
                        actor_role="constructor",
                        actor_attempt_sequence=index + 1,
                        issued_at=NOW,
                        expires_at=NOW + timedelta(minutes=10),
                    )
                return "won"
            except (ContentionLost, IntegrityError):
                return "lost"
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
        return "locked"

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(contend, range(10)))
    assert results.count("won") == 1
    assert results.count("lost") == 9
    with session_scope(factory) as session:
        active = session.scalars(
            select(wf.ServiceLease).where(
                wf.ServiceLease.task_id == task_id,
                wf.ServiceLease.state == "active",
            )
        ).all()
        assert len(active) == 1


def test_independent_tasks_do_not_share_a_global_lease_fence(workflow_db) -> None:
    factory, ids, context, first_task_id = workflow_db
    with session_scope(factory) as session:
        second = _import_one(
            session,
            ids,
            context,
            task_id=_next(ids),
            asana_gid="987654321",
        )
        for index, task_id in enumerate((first_task_id, second.task_id), start=1):
            run_id, request_id, execution_id = _next(ids), _next(ids), _next(ids)
            owner = f"independent-{index}"
            _register_run(session, generation_id=context["generation_id"], run_id=run_id, owner=owner)
            service = WorkflowAuthorityService(session)
            _admit(
                service,
                request_id=request_id,
                generation_id=context["generation_id"],
                run_id=run_id,
                owner=owner,
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
            service.acquire_actor_lease(
                lease_id=_next(ids),
                execution_id=execution_id,
                operation_id=operation.operation_id,
                run_id=run_id,
                owner_id=owner,
                actor_role="constructor",
                actor_attempt_sequence=index,
                issued_at=NOW,
                expires_at=NOW + timedelta(minutes=10),
            )
        assert session.scalar(
            select(func.count()).select_from(wf.ServiceLease).where(wf.ServiceLease.state == "active")
        ) == 2


def test_ten_way_marco_authorization_reservation_is_single_winner(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    contenders: list[tuple[uuid.UUID, uuid.UUID]] = []
    with session_scope(factory) as session:
        admin_run = _next(ids)
        _register_run(
            session, generation_id=context["generation_id"], run_id=admin_run, owner="marco", agent="marco"
        )
        grant_request, grant_execution = _next(ids), _next(ids)
        service = WorkflowAuthorityService(session)
        _admit(
            service,
            request_id=grant_request,
            generation_id=context["generation_id"],
            run_id=admin_run,
            command="authorize-governed-change",
            owner="marco",
            principal="admin",
        )
        _execution(
            service,
            execution_id=grant_execution,
            request_id=grant_request,
            generation_id=context["generation_id"],
            task_id=task_id,
            binding_id=context["binding_id"],
            command="authorize-governed-change",
        )
        grant_id = _next(ids)
        service.grant_marco_authorization(
            grant_id=grant_id,
            execution_id=grant_execution,
            task_id=task_id,
            operation_id=None,
            field_name="title",
            before_value="old",
            after_value="new",
            reason="approved exact governed change",
            actor="Marco",
            run_id=admin_run,
            granted_at=NOW,
        )
        for index in range(10):
            request_id, execution_id = _next(ids), _next(ids)
            _admit(
                service,
                request_id=request_id,
                generation_id=context["generation_id"],
                run_id=admin_run,
                command="prepare",
                owner="marco",
                principal="admin",
                payload={"candidate": index},
            )
            _execution(
                service,
                execution_id=execution_id,
                request_id=request_id,
                generation_id=context["generation_id"],
                task_id=task_id,
                binding_id=context["binding_id"],
                command="prepare",
            )
            contenders.append((execution_id, _next(ids)))

    barrier = threading.Barrier(10)

    def reserve(item: tuple[uuid.UUID, uuid.UUID]) -> str:
        execution_id, token = item
        barrier.wait()
        for attempt in range(5):
            try:
                with session_scope(factory) as session:
                    WorkflowAuthorityService(session).reserve_marco_authorization(
                        grant_id=grant_id,
                        reservation_token=token,
                        execution_id=execution_id,
                        reserved_at=NOW,
                    )
                return "won"
            except ContentionLost:
                return "lost"
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
        return "locked"

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(reserve, contenders))
    assert results.count("won") == 1
    assert results.count("lost") == 9
    with session_scope(factory) as session:
        state = session.get(wf.MarcoAuthorizationState, grant_id)
        assert state is not None and state.state == "reserved"
        assert session.scalar(
            select(func.count()).select_from(wf.MarcoAuthorizationEvent).where(
                wf.MarcoAuthorizationEvent.grant_id == grant_id,
                wf.MarcoAuthorizationEvent.event_kind == "reserved",
            )
        ) == 1
