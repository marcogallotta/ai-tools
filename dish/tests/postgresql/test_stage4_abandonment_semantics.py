from __future__ import annotations
import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_port import CommandRuleError, PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.read_model import PostgresReadModel
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db
from tests.support.postgresql.release import _prepare_candidate
from tests.support.postgresql.abandonment_terminal_migration import build_0016_abandonment_fixture
from tests.support.postgresql.command import (
    _add_verification_queue,
    _call,
    _inspect,
    _port,
    _prepare_for_verification,
    _start_initial,
    _start_verification,
    _verification_ready,
)


def test_retired_generation_abandonment_cannot_fence_reconcile_or_seed_successor(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run_id = _next(ids)
        admin_run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
            agent="claude",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=run_id)
        source = session.get(
            wf.WorkflowOperation, uuid.UUID(started.data["operation_id"])
        )
        source.phase = "held_evidence"
        session.flush()
        abandoned = port.execute(
            _call(
                "abandon-operation",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "lease_id": started.data["lease_id"],
                    "reason": "generation-isolation regression",
                },
            )
        )
        assert abandoned.ok and abandoned.data["state"] == "blocked"
        abandonment_id = uuid.UUID(abandoned.data["abandonment_id"])

        predecessor = session.get(
            models.AuthorityGeneration, context["generation_id"]
        )
        predecessor.status = "retired"
        predecessor.retired_at = NOW
        session.flush()
        successor_generation_id = _next(ids)
        session.add(
            models.AuthorityGeneration(
                generation_id=successor_generation_id,
                predecessor_generation_id=context["generation_id"],
                creation_reason="destructive_restore",
                external_restore_control_id=f"restore-{successor_generation_id}",
                schema_head=predecessor.schema_head,
                dish_release=predecessor.dish_release,
                status="active",
                created_at=NOW,
                retired_at=None,
            )
        )
        session.flush()
        successor_port = PostgresCommandPort(
            session,
            cursor_secret=b"stage-4-cursor-secret-32-bytes!!",
            uuid_factory=lambda: _next(ids),
        )

        task = session.get(models.DishTask, task_id)
        assert successor_port._open_abandonment_id(
            successor_generation_id, task_id
        ) is None
        assert successor_port._open_abandonment_id(
            context["generation_id"], task_id
        ) == str(abandonment_id)

        successor_execution = SimpleNamespace(
            generation_id=successor_generation_id,
            execution_id=_next(ids),
        )
        with pytest.raises(CommandRuleError) as exc_info:
            successor_port._reconcile_abandonment(
                _call(
                    "reconcile-abandonment",
                    run_id=admin_run,
                    owner="Marco",
                    principal="admin",
                    arguments={
                        "task_id": str(task_id),
                        "abandonment_id": str(abandonment_id),
                    },
                ),
                None,
                None,
                successor_execution,
                task,
                None,
            )
        assert exc_info.value.code == "ABANDONMENT_GENERATION_MISMATCH"

        attempt = session.get(wf.AbandonmentAttempt, abandonment_id)
        assert attempt.state == "blocked"
        assert attempt.successor_operation_id is None
        assert session.scalar(
            select(wf.OperationSuccessionEdge.succession_id).where(
                wf.OperationSuccessionEdge.abandonment_id == abandonment_id
            )
        ) is None

        retired_source = session.get(
            wf.WorkflowOperation, attempt.source_operation_id
        )
        with pytest.raises(CommandRuleError) as publish_exc_info:
            successor_port._publish_abandonment_successor(
                attempt, retired_source, successor_execution, NOW
            )
        assert publish_exc_info.value.code == "ABANDONMENT_GENERATION_MISMATCH"


def test_abandonment_publishes_route_preserving_successor_once(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run_id = _next(ids)
        admin_run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        _register_run(session, generation_id=context["generation_id"], run_id=admin_run, owner="Marco", agent="claude")
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=run_id)
        abandoned = port.execute(
            _call(
                "abandon-operation",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "lease_id": started.data["lease_id"],
                    "reason": "run permanently unavailable",
                },
            )
        )
        assert abandoned.ok and abandoned.data["state"] == "completed"
        successor = session.get(wf.WorkflowOperation, uuid.UUID(abandoned.data["successor_operation_id"]))
        source = session.get(wf.WorkflowOperation, uuid.UUID(started.data["operation_id"]))
        edge = session.scalar(select(wf.OperationSuccessionEdge))
        assert source.lifecycle == "abandoned"
        assert (successor.kind, successor.phase, successor.persisted_actions) == (
            source.kind,
            source.phase,
            source.persisted_actions,
        )
        assert edge.source_operation_id == source.operation_id
        attempt = session.get(wf.AbandonmentAttempt, uuid.UUID(abandoned.data["abandonment_id"]))
        assert attempt.state == "completed"
        assert attempt.successor_operation_id == successor.operation_id
        assert attempt.terminal_at is not None
        assert attempt.terminal_at.replace(tzinfo=NOW.tzinfo) == NOW
        assert session.scalar(select(func.count()).select_from(wf.OperationSuccessionEdge)) == 1

def test_completed_abandonment_does_not_block_release_but_unresolved_states_do(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run_id = _next(ids)
        admin_run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
            agent="claude",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=run_id)
        abandoned = port.execute(
            _call(
                "abandon-operation",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "lease_id": started.data["lease_id"],
                    "reason": "run permanently unavailable",
                },
            )
        )
        assert abandoned.ok and abandoned.data["state"] == "completed"
        discarded = port.execute(
            _call(
                "discard",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": abandoned.data["successor_operation_id"],
                },
            )
        )
        assert discarded.ok

        service, candidate_id = _prepare_candidate(session, ids, context, task_id)
        completed_check = next(
            check
            for check in service.evaluate_candidate(candidate_id=candidate_id).checks
            if check.code == "legacy_and_target_authority_resolved"
        )
        assert completed_check.details["abandonments"] == 0

        completed = session.get(
            wf.AbandonmentAttempt, uuid.UUID(abandoned.data["abandonment_id"])
        )
        unresolved = wf.AbandonmentAttempt(
            abandonment_id=_next(ids),
            generation_id=completed.generation_id,
            task_id=completed.task_id,
            source_operation_id=completed.source_operation_id,
            source_lease_id=completed.source_lease_id,
            source_actor_attempt_sequence=completed.source_actor_attempt_sequence,
            source_cycle_id=completed.source_cycle_id,
            source_owner_id=completed.source_owner_id,
            source_run_id=completed.source_run_id,
            baseline_content_activation_id=completed.baseline_content_activation_id,
            baseline_placement_event_id=completed.baseline_placement_event_id,
            reason="published state still awaiting terminalization",
            state="published",
            request_id=completed.request_id,
            command_execution_id=completed.command_execution_id,
            successor_operation_id=completed.successor_operation_id,
            created_at=NOW,
            terminal_at=None,
        )
        session.add(unresolved)
        session.flush()
        published_check = next(
            check
            for check in service.evaluate_candidate(candidate_id=candidate_id).checks
            if check.code == "legacy_and_target_authority_resolved"
        )
        assert not published_check.passed
        assert published_check.details["abandonments"] == 1

        unresolved.state = "blocked"
        unresolved.successor_operation_id = None
        unresolved.reason = "publication failed and requires reconciliation"
        session.flush()
        blocked_check = next(
            check
            for check in service.evaluate_candidate(candidate_id=candidate_id).checks
            if check.code == "legacy_and_target_authority_resolved"
        )
        assert not blocked_check.passed
        assert blocked_check.details["abandonments"] == 1

def test_completed_abandonment_requires_successor_evidence(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run_id = _next(ids)
        admin_run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
            agent="claude",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=run_id)
        result = port.execute(
            _call(
                "abandon-operation",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "lease_id": started.data["lease_id"],
                    "reason": "run permanently unavailable",
                },
            )
        )
        assert result.ok and result.data["state"] == "completed"
        abandonment_id = uuid.UUID(result.data["abandonment_id"])

    with session_scope(factory) as session:
        attempt = session.get(wf.AbandonmentAttempt, abandonment_id)
        attempt.successor_operation_id = None
        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()
