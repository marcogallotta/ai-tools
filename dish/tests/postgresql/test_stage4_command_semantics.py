from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_port import PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.read_model import PostgresReadModel
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db
from tests.support.postgresql.release import _prepare_candidate
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


def test_inspection_rejects_author_or_same_agent_as_verifier(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    author_run = _next(ids)
    other_run = _next(ids)
    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=other_run,
            owner="other-owner",
            agent="claude",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=author_run)
        _prepare_for_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=author_run,
        )
        same_run = port.execute(
            _call(
                "start",
                run_id=author_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "kind": "verification",
                    "agent": "codex",
                    "independence_attestation": "independent",
                },
            )
        )
        same_agent = port.execute(
            _call(
                "start",
                run_id=other_run,
                request_id=_next(ids),
                owner="other-owner",
                arguments={
                    "task_id": str(task_id),
                    "kind": "verification",
                    "agent": "claude",
                    "independence_attestation": "independent",
                },
            )
        )
        assert same_run.code == "VERIFIER_NOT_INDEPENDENT"
        assert same_agent.code == "VERIFIER_NOT_INDEPENDENT"
        assert session.scalar(select(func.count()).select_from(wf.VerificationInspectionOccurrence)) == 0


def test_small_correction_binds_inspection_correction_activation_and_signoff(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, _author_run, verifier_run, started, prepared, inspected = _verification_ready(
            session, ids, context, task_id
        )
        reviewed = session.get(models.ContentVersion, uuid.UUID(prepared.data["content_version_id"]))
        approved = port.execute(
            _call(
                "approve",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                principal="verification",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "agent": "codex",
                    "model": "o3",
                    "correction": "small",
                    "file_text": __import__("tests.support.canonical", fromlist=["TASK"]).TASK.replace(
                        "A compact side dish for testing texture.",
                        "A compact corrected side dish for testing texture.",
                    ),
                    "reviewed_identity": reviewed.content_identity,
                    "semantic_review_complete": True,
                    "provenance_complete": True,
                },
            )
        )
        assert approved.ok
        correction = session.scalar(select(wf.VerificationCorrection))
        signoff = session.get(wf.VerificationSignoff, uuid.UUID(approved.data["signoff_id"]))
        head = session.get(models.TaskAuthorityHead, (context["generation_id"], task_id))
        activation = session.get(models.ContentActivation, head.current_content_activation_id)
        assert correction.source_content_version_id == uuid.UUID(prepared.data["content_version_id"])
        assert correction.corrected_content_version_id == activation.content_version_id
        assert signoff.inspection_id == uuid.UUID(inspected.data["inspection_id"])
        assert signoff.signed_content_version_id == activation.content_version_id


def test_large_rejection_creates_exact_corrected_occurrence_and_new_cycle(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, _author_run, verifier_run, started, prepared, _inspected = _verification_ready(
            session, ids, context, task_id
        )
        rejected = port.execute(
            _call(
                "reject",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                principal="verification",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "agent": "codex",
                    "route": "large",
                    "reason": "material correction",
                    "file_text": __import__("tests.support.canonical", fromlist=["TASK"]).TASK.replace(
                        "A compact side dish for testing texture.",
                        "A materially corrected side dish for testing texture.",
                    ),
                },
            )
        )
        assert rejected.ok
        correction = session.scalar(select(wf.VerificationCorrection))
        cycles = session.scalars(
            select(wf.VerificationCycle)
            .where(wf.VerificationCycle.operation_id == uuid.UUID(started.data["operation_id"]))
            .order_by(wf.VerificationCycle.created_at)
        ).all()
        operation = session.get(wf.WorkflowOperation, uuid.UUID(started.data["operation_id"]))
        assert correction.correction_class == "large"
        assert correction.source_content_version_id == uuid.UUID(prepared.data["content_version_id"])
        assert len(cycles) == 2 and cycles[0].lifecycle == "rejected" and cycles[1].lifecycle == "open"
        assert cycles[1].reviewed_content_version_id == correction.corrected_content_version_id
        assert operation.phase == "await_verification" and operation.persisted_actions == ["inspect"]


def test_verifier_reconstruction_is_bound_to_latest_verification_cycle(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    verifier_run = _next(ids)
    new_verifier_run = _next(ids)
    operation_id: uuid.UUID

    with session_scope(factory) as session:
        _add_verification_queue(session, ids, context)
        author_run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=author_run)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=verifier_run,
            owner="verifier-owner",
            agent="codex",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=author_run)
        operation_id = uuid.UUID(started.data["operation_id"])
        _prepare_for_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=author_run,
        )
        _start_verification(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=verifier_run,
        )
        _inspect(
            port,
            ids,
            task_id=task_id,
            operation_id=started.data["operation_id"],
            run_id=verifier_run,
        )
        rejected = port.execute(
            _call(
                "reject",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                principal="verification",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "agent": "codex",
                    "route": "large",
                    "reason": "material correction",
                    "file_text": __import__(
                        "tests.support.canonical", fromlist=["TASK"]
                    ).TASK.replace(
                        "A compact side dish for testing texture.",
                        "A materially corrected side dish for testing texture.",
                    ),
                },
            )
        )
        assert rejected.ok
        cycles = session.scalars(
            select(wf.VerificationCycle)
            .where(wf.VerificationCycle.operation_id == operation_id)
            .order_by(wf.VerificationCycle.cycle_sequence)
        ).all()
        assert len(cycles) == 2
        assert cycles[0].cycle_id != cycles[1].cycle_id
        assert session.scalar(
            select(func.count()).select_from(wf.OperationActorFact).where(
                wf.OperationActorFact.operation_id == operation_id,
                wf.OperationActorFact.actor_role == "verification",
            )
        ) == 1

    with session_scope(factory) as session:
        operation = session.get(wf.WorkflowOperation, operation_id)
        head = session.get(models.TaskAuthorityHead, (context["generation_id"], task_id))
        activation = session.get(models.ContentActivation, head.current_content_activation_id)
        version = session.get(models.ContentVersion, activation.content_version_id)
        snapshot = PostgresReadModel(
            session, cursor_secret=b"cycle-bound-read-model-secret-32"
        )._workflow_snapshot(
            generation_id=context["generation_id"],
            task_id=task_id,
            title=version.title,
            body=version.body,
            operation=operation,
        )
        assert snapshot.verifier_established is False
        assert snapshot.dish_inspect_current is False

        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=new_verifier_run,
            owner="new-verifier-owner",
            agent="codex",
        )
        _start_verification(
            _port(session, ids),
            ids,
            task_id=task_id,
            operation_id=str(operation_id),
            run_id=new_verifier_run,
            owner="new-verifier-owner",
            agent="codex",
        )

    with session_scope(factory) as session:
        operation = session.get(wf.WorkflowOperation, operation_id)
        head = session.get(models.TaskAuthorityHead, (context["generation_id"], task_id))
        activation = session.get(models.ContentActivation, head.current_content_activation_id)
        version = session.get(models.ContentVersion, activation.content_version_id)
        snapshot = PostgresReadModel(
            session, cursor_secret=b"cycle-bound-read-model-secret-32"
        )._workflow_snapshot(
            generation_id=context["generation_id"],
            task_id=task_id,
            title=version.title,
            body=version.body,
            operation=operation,
        )
        assert snapshot.verifier_established is True
        assert snapshot.dish_inspect_current is False

def test_submit_rejects_when_current_content_no_longer_matches_signoff(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, _author_run, verifier_run, started, prepared, _inspected = _verification_ready(
            session, ids, context, task_id
        )
        reviewed = session.get(models.ContentVersion, uuid.UUID(prepared.data["content_version_id"]))
        approved = port.execute(
            _call(
                "approve",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                principal="verification",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "agent": "codex",
                    "model": "o3",
                    "correction": "none",
                    "reviewed_identity": reviewed.content_identity,
                    "semantic_review_complete": True,
                    "provenance_complete": True,
                },
            )
        )
        assert approved.ok
        head = session.get(models.TaskAuthorityHead, (context["generation_id"], task_id))
        replacement_id = _next(ids)
        activation_id = _next(ids)
        body = "Drifted after signoff\n---\nStatus: pending-verification\n"
        session.add(
            models.ContentVersion(
                content_version_id=replacement_id,
                generation_id=context["generation_id"],
                task_id=task_id,
                representation_kind="document",
                title="[ready] Exact imported task",
                body=body,
                identity_scheme="sha256-title-body-v1",
                content_identity=hashlib.sha256(("[ready] Exact imported task\0" + body).encode()).hexdigest(),
                creator_route="import",
                import_run_id=context["import_run_id"],
                command_execution_id=None,
                predecessor_content_version_id=reviewed.content_version_id,
                contract_binding_id=context["binding_id"],
                created_at=NOW,
            )
        )
        session.flush()
        session.add(
            models.ContentActivation(
                content_activation_id=activation_id,
                generation_id=context["generation_id"],
                task_id=task_id,
                content_version_id=replacement_id,
                activation_route="import",
                import_run_id=context["import_run_id"],
                command_execution_id=None,
                task_revision=head.task_revision + 1,
                activated_at=NOW,
            )
        )
        session.flush()
        head.current_content_activation_id = activation_id
        head.task_revision += 1
        submitted = port.execute(
            _call(
                "submit",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "destination_section_id": str(context["section_id"]),
                },
            )
        )
        assert submitted.code == "ACTION_NOT_LEGAL"
        operation = session.get(wf.WorkflowOperation, uuid.UUID(started.data["operation_id"]))
        assert operation.lifecycle == "open"


def test_discard_requires_unchanged_creation_baseline_and_no_effect_or_step(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run_id = _next(ids)
        admin_run = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        _register_run(session, generation_id=context["generation_id"], run_id=admin_run, owner="Marco", agent="claude")
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=run_id)
        discarded = port.execute(
            _call(
                "discard",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={"task_id": str(task_id), "operation_id": started.data["operation_id"]},
            )
        )
        assert discarded.ok
        operation = session.get(wf.WorkflowOperation, uuid.UUID(started.data["operation_id"]))
        lease = session.get(wf.ServiceLease, uuid.UUID(started.data["lease_id"]))
        assert operation.lifecycle == "cancelled_by_marco"
        assert lease.state == "released"


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


def test_abandonment_terminal_migration_completes_only_durable_published_success(workflow_db) -> None:
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
        assert result.ok
        abandonment_id = uuid.UUID(result.data["abandonment_id"])
        attempt = session.get(wf.AbandonmentAttempt, abandonment_id)
        attempt.state = "published"
        attempt.terminal_at = None

    engine = factory.kw["bind"]
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL)")
        )
        connection.execute(
            text(
                "INSERT INTO alembic_version(version_num) "
                "VALUES ('0016_honest_binding_null_identity')"
            )
        )

    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", str(engine.url))
    command.upgrade(config, "head")

    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT state, terminal_at FROM abandonment_attempts "
                "WHERE abandonment_id = :abandonment_id"
            ),
            {"abandonment_id": abandonment_id.hex},
        ).one()
        assert row.state == "completed"
        assert row.terminal_at is not None
        assert connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalar_one() == "0017_abandonment_terminal_state"
