from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_port import PostgresCommandPort
from dish_pg.database import session_scope
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db
from tests.support.postgresql.command import (
    _add_verification_queue,
    _call,
    _inspect,
    _port,
    _prepare_for_verification,
    _start_initial,
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
                "inspect",
                run_id=author_run,
                request_id=_next(ids),
                principal="verification",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "agent": "codex",
                    "independence_attestation": "independent",
                },
            )
        )
        same_agent = port.execute(
            _call(
                "inspect",
                run_id=other_run,
                request_id=_next(ids),
                owner="other-owner",
                principal="verification",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
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
                    "correction": "small",
                    "file_text": "Small corrected body\n---\nStatus: pending-verification\n",
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
                    "file_text": "Large corrected body\n---\nStatus: pending-verification\n",
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
        assert abandoned.ok and abandoned.data["state"] == "published"
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
        assert session.scalar(select(func.count()).select_from(wf.OperationSuccessionEdge)) == 1
