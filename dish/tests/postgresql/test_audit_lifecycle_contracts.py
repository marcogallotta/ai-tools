from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy import select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.command_contract import ADMIN_COMMANDS, COMMAND_DEFINITIONS
from dish_pg.database import session_scope
from dish_pg.document_authority import parse_canonical_document
from dish_pg.workflow import WorkflowAuthorityError
from tests.support.canonical import TASK
from tests.support.postgresql.command import (
    _add_destination_section,
    _add_verification_queue,
    _call,
    _inspect,
    _port,
    _prepare_for_verification,
    _start_initial,
    _start_verification,
    _verification_ready,
)
from tests.support.postgresql.workflow import _next, _register_run, workflow_db


def test_approval_creates_ready_occurrence_and_submit_derives_destination(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, author_run, verifier_run, started, prepared, _inspection = _verification_ready(
            session, ids, context, task_id
        )
        destination_section_id = _add_destination_section(session, ids, context)
        reviewed = session.get(
            models.ContentVersion, uuid.UUID(prepared.data["content_version_id"])
        )
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
        signed = session.get(
            models.ContentVersion,
            uuid.UUID(approved.data["signed_content_version_id"]),
        )
        parsed = parse_canonical_document(
            title=signed.title, body=signed.body, expected_status="ready"
        )
        assert parsed.document.state.values["Status"] == "ready"
        forbidden = port.execute(
            _call(
                "submit",
                run_id=author_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                    "destination_section_gid": "1217084805070731",
                },
            )
        )
        submitted = port.execute(
            _call(
                "submit",
                run_id=author_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "operation_id": started.data["operation_id"],
                },
            )
        )
        assert forbidden.code == "UNEXPECTED_DESTINATION_ARGUMENT"
        assert submitted.ok
        state = session.get(
            models.DishState,
            (context["generation_id"], task_id),
        )
        operation = session.get(
            wf.WorkflowOperation, uuid.UUID(started.data["operation_id"])
        )
        assert state.section_id == destination_section_id
        assert operation.lifecycle == "completed"
        assert state.completed is False


def test_human_review_decision_resumes_same_operation_with_legacy_arguments(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, _author_run, verifier_run, started, _prepared, _inspection = _verification_ready(
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
                    "route": "human-review",
                    "reason": "Marco must choose the route",
                },
            )
        )
        assert rejected.ok
        admin_run = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
            agent="claude",
        )
        decided = port.execute(
            _call(
                "record-human-decision",
                run_id=admin_run,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={
                    "submission_id": started.data["operation_id"],
                    "detail": "Use the dry route to preserve the intended crisp texture",
                    "resume_status": "pending-verification",
                    "expected_task_gid": "123456789",
                    "expected_cycle_id": rejected.data["cycle_id"],
                },
            )
        )
        assert decided.ok
        assert decided.data["resume_status"] == "pending-verification"
        assert decided.data["cycle_id"] != rejected.data["cycle_id"]
        requirement = session.get(
            wf.HumanReviewRequirement, uuid.UUID(rejected.data["requirement_id"])
        )
        assert requirement.state == "decided"
        state = session.get(models.DishState, (context["generation_id"], task_id))
        resumed = session.get(models.ContentVersion, state.current_content_version_id)
        parsed = parse_canonical_document(
            title=resumed.title,
            body=resumed.body,
            expected_status="pending-verification",
        )
        assert any(
            line.startswith("Human — Marco: human_review resolved — Use the dry route")
            for line in parsed.document.decisions
        )
        operation = session.get(
            wf.WorkflowOperation, uuid.UUID(started.data["operation_id"])
        )
        assert operation.phase == "await_verification"
        assert operation.persisted_actions == ["inspect"]


def test_expected_authority_failure_rolls_back_domain_mutations_and_replays(
    workflow_db, monkeypatch
) -> None:
    factory, ids, context, task_id = workflow_db
    request_id = _next(ids)
    with session_scope(factory) as session:
        port, _author_run, verifier_run, started, prepared, _inspection = _verification_ready(
            session, ids, context, task_id
        )
        operation_id = started.data["operation_id"]
        reviewed = session.get(
            models.ContentVersion, uuid.UUID(prepared.data["content_version_id"])
        )
        state = session.get(models.DishState, (context["generation_id"], task_id))
        baseline_content_version_id = state.current_content_version_id
        baseline_count = session.query(models.ContentVersion).filter_by(task_id=task_id).count()

        def fail_signoff(**_kwargs):
            raise WorkflowAuthorityError("injected signoff authority failure")

        monkeypatch.setattr(port.workflow, "signoff_verification", fail_signoff)
        call = _call(
            "approve",
            run_id=verifier_run,
            request_id=request_id,
            owner="verifier-owner",
            principal="verification",
            arguments={
                "task_id": str(task_id),
                "operation_id": operation_id,
                "agent": "codex",
                "model": "o3",
                "correction": "none",
                "reviewed_identity": reviewed.content_identity,
                "semantic_review_complete": True,
                "provenance_complete": True,
            },
        )
        first = port.execute(call)
        second = port.execute(call)
        assert first.code == "AUTHORITY_MISMATCH"
        assert second.code == first.code
        assert second.request_replayed is True
        session.refresh(state)
        assert state.current_content_version_id == baseline_content_version_id
        assert session.query(models.ContentVersion).filter_by(task_id=task_id).count() == baseline_count
        assert session.scalar(
            select(wf.VerificationSignoff).where(
                wf.VerificationSignoff.cycle_id == uuid.UUID(prepared.data["cycle_id"])
            )
        ) is None
        cycle = session.get(wf.VerificationCycle, uuid.UUID(prepared.data["cycle_id"]))
        assert cycle.lifecycle == "open"


def test_missing_approval_evidence_is_immutable_and_has_no_signoff(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    request_id = _next(ids)
    with session_scope(factory) as session:
        port, _author_run, verifier_run, started, prepared, _inspection = _verification_ready(
            session, ids, context, task_id
        )
        operation_id = started.data["operation_id"]
        reviewed = session.get(
            models.ContentVersion, uuid.UUID(prepared.data["content_version_id"])
        )
        call = _call(
            "approve",
            run_id=verifier_run,
            request_id=request_id,
            owner="verifier-owner",
            principal="verification",
            arguments={
                "task_id": str(task_id),
                "operation_id": operation_id,
                "agent": "codex",
                "model": "o3",
                "correction": "none",
                "reviewed_identity": reviewed.content_identity,
                "semantic_review_complete": True,
            },
        )
        first = port.execute(call)
        second = port.execute(call)
        assert first.code == "PROVENANCE_REVIEW_REQUIRED"
        assert second.code == first.code
        assert second.data == first.data
        assert second.request_replayed is True
        cycle = session.get(
            wf.VerificationCycle, uuid.UUID(prepared.data["cycle_id"])
        )
        operation = session.get(wf.WorkflowOperation, uuid.UUID(operation_id))
        assert cycle.lifecycle == "open"
        assert operation.phase == "await_verification"
        assert session.scalar(
            select(wf.VerificationSignoff).where(
                wf.VerificationSignoff.cycle_id == cycle.cycle_id
            )
        ) is None
