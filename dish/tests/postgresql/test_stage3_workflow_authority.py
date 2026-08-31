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
from dish_pg.repositories import DishRepository, ScalarMutationSource
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
    assert "causality_edges" not in wf.STAGE3_TABLE_NAMES
    assert "causality_edges" not in wf.STAGE3_IMMUTABLE_TABLE_NAMES
    assert "causality_edges" not in models.Base.metadata.tables
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
        state = session.get(models.DishState, (context["generation_id"], task_id))
        membership = session.get(
            models.TaskMembershipHead, (context["generation_id"], task_id)
        )
        assert state is not None and membership is not None
        mutation = DishRepository(session).begin_scalar_mutation(
            generation_id=context["generation_id"],
            task_id=task_id,
            expected_dish_version=state.dish_version,
            expected_placement_version=state.placement_version,
            expected_catalog_version_id=state.catalog_version_id,
            source=ScalarMutationSource(
                route="import",
                import_run_id=context["import_run_id"],
                occurred_at=NOW,
            ),
        )
        mutation.place(
            section_id=state.section_id,
            catalog_version_id=state.catalog_version_id,
        )
        mutation.finalize()
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


def _create_revocation_test_operation(
    service: WorkflowAuthorityService,
    ids,
    *,
    context: dict[str, uuid.UUID],
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    owner: str = "owner-1",
) -> tuple[uuid.UUID, uuid.UUID]:
    request_id, execution_id, operation_id = _next(ids), _next(ids), _next(ids)
    _admit(
        service,
        request_id=request_id,
        generation_id=context["generation_id"],
        run_id=run_id,
        owner=owner,
        command="prepare",
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
    service.repo.capture_task_fence(
        execution_id=execution_id,
        generation_id=context["generation_id"],
        task_id=task_id,
        at=NOW,
    )
    service.create_operation(
        operation_id=operation_id,
        execution_id=execution_id,
        task_id=task_id,
        kind="initial",
        phase="prepare_required",
        persisted_actions=["prepare"],
        created_at=NOW,
    )
    return execution_id, operation_id


def test_exact_operation_run_revocation_blocks_grants_without_global_over_revocation(workflow_db) -> None:
    from dish_pg.workflow import OperationRunRevoked
    from tests.support.postgresql.core import _import_one

    factory, ids, context, task_id = workflow_db
    killed_run = _next(ids)
    successor_run = _next(ids)
    revocation_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=killed_run)
        _register_run(session, generation_id=context["generation_id"], run_id=successor_run)
        service = WorkflowAuthorityService(session, uuid_factory=lambda: _next(ids))
        execution_id, operation_id = _create_revocation_test_operation(
            service, ids, context=context, task_id=task_id, run_id=killed_run
        )
        lease_id = _next(ids)
        lease = service.acquire_actor_lease(
            lease_id=lease_id,
            execution_id=execution_id,
            operation_id=operation_id,
            run_id=killed_run,
            owner_id="owner-1",
            actor_role="constructor",
            actor_attempt_sequence=1,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=10),
        )
        revocation = service.repo.revoke_operation_run(
            revocation_id=revocation_id,
            generation_id=context["generation_id"],
            operation_id=operation_id,
            owner_id="owner-1",
            run_id=killed_run,
            source_lease_id=lease_id,
            reason="operator killed exact operation run",
            revoked_at=NOW + timedelta(seconds=1),
        )
        assert revocation.run_id == killed_run
        assert session.get(wf.ServiceRun, killed_run).status == "active"
        assert lease.state == "active"

        with pytest.raises(OperationRunRevoked):
            service.renew_lease(
                lease_id=lease_id,
                execution_id=execution_id,
                run_id=killed_run,
                owner_id="owner-1",
                now=NOW + timedelta(minutes=1),
                new_expiry=NOW + timedelta(minutes=20),
            )
        with pytest.raises(OperationRunRevoked):
            service.acquire_actor_lease(
                lease_id=_next(ids),
                execution_id=execution_id,
                operation_id=operation_id,
                run_id=killed_run,
                owner_id="owner-1",
                actor_role="constructor",
                actor_attempt_sequence=2,
                issued_at=NOW + timedelta(minutes=1),
                expires_at=NOW + timedelta(minutes=11),
            )

        claim_request, claim_execution = _next(ids), _next(ids)
        _admit(
            service,
            request_id=claim_request,
            generation_id=context["generation_id"],
            run_id=killed_run,
            command="prepare",
        )
        service.begin_execution(
            ExecutionSpec(
                execution_id=claim_execution,
                request_id=claim_request,
                generation_id=context["generation_id"],
                task_id=task_id,
                operation_id=operation_id,
                command_name="prepare",
                transaction_profile="L",
                canonical_intent={"command": "prepare"},
                pinned_inputs={"now": NOW.isoformat()},
                contract_binding_id=context["binding_id"],
                admitted_at=NOW,
            )
        )
        with pytest.raises(OperationRunRevoked):
            service.repo.claim_execution(
                execution_id=claim_execution,
                claimant=f"owner-1:{killed_run}",
                claim_token=_next(ids),
                now=NOW + timedelta(seconds=2),
                ttl=timedelta(minutes=2),
            )

        service.repo.assert_operation_run_not_revoked(
            generation_id=context["generation_id"],
            operation_id=operation_id,
            owner_id="owner-1",
            run_id=successor_run,
        )

        unrelated = _import_one(
            session,
            ids,
            context,
            task_id=_next(ids),
            asana_gid="987654322",
        )
        _, unrelated_operation = _create_revocation_test_operation(
            service,
            ids,
            context=context,
            task_id=unrelated.task_id,
            run_id=killed_run,
        )
        service.repo.assert_operation_run_not_revoked(
            generation_id=context["generation_id"],
            operation_id=unrelated_operation,
            owner_id="owner-1",
            run_id=killed_run,
        )

    with factory() as session:
        revocation = session.get(wf.OperationRunRevocation, revocation_id)
        assert revocation is not None
        revocation.reason = "attempted mutation"
        with pytest.raises(IntegrityError, match="immutable authority row"):
            session.flush()
        session.rollback()
        revocation = session.get(wf.OperationRunRevocation, revocation_id)
        assert revocation is not None
        session.delete(revocation)
        with pytest.raises(IntegrityError, match="immutable authority row"):
            session.flush()
