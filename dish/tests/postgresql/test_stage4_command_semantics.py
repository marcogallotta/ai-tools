from __future__ import annotations
import hashlib
import uuid
from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.command_contract import ACTION_COMMANDS, definition_for
from dish_pg.command_effects import effect_spec_for
from dish_pg.command_port import PostgresCommandPort
from dish_pg.database import session_scope
from dish_pg.frontend_board_query import FrontendBoardQuery
from dish_pg.planner import (
    AuthorityFence,
    AuthoritativeSnapshot,
    CanonicalCommandIntent,
    plan_command,
)
from dish_pg.read_model import PostgresReadModel
from dish_pg.repositories import DishRepository, ScalarMutationSource
from dish_pg.services import CoreAuthorityService, ImportedTaskSpec
from dish_pg.shadow_worker import (
    _semantic_shadow_command,
    _semantic_shadow_request_id,
    _shadow_uuid,
)
from dish_pg.workflow import RequestIdentityConflict
from dish_tool.workflow_policy import WorkflowSnapshot, legal_actions
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db
from tests.support.postgresql.release import _prepare_candidate
from tests.support.verification import TASK as PENDING_RESEARCH_TASK
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
from tests.support.postgresql.stage4_command_semantics import _case_test_verifier_reconstruction_is_bound_to_latest_verification_cycle



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
        state = session.get(models.DishState, (context["generation_id"], task_id))
        assert correction.source_content_version_id == uuid.UUID(prepared.data["content_version_id"])
        assert correction.corrected_content_version_id == state.current_content_version_id
        assert signoff.inspection_id == uuid.UUID(inspected.data["inspection_id"])
        assert signoff.signed_content_version_id == state.current_content_version_id


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


def _valid_hold_reject_planner_state() -> tuple[AuthoritativeSnapshot, CanonicalCommandIntent]:
    workflow = WorkflowSnapshot(
        operation_status="open",
        operation_phase="prepare_required",
        persisted_actions=("prepare",),
        live_status="pending-research",
        live_section_gid="rq",
        verification_queue_gid="vq",
        verifier_established=False,
        latest_cycle_outcome=None,
        latest_cycle_route=None,
        validation_rules=(),
        operation_kind="initial",
    )
    intent = CanonicalCommandIntent(
        "hold-reject",
        {
            "route": "evidence",
            "resume_status": "pending-research",
            "reason": "need evidence",
        },
        "agent",
        "owner-1",
        "run-1",
    )
    snapshot = AuthoritativeSnapshot(
        generation_id=str(uuid.uuid4()),
        task_id=str(uuid.uuid4()),
        fence=AuthorityFence(1, 1, 1, "prepare_required"),
        workflow=workflow,
        task_exists=True,
        hold_reject_baseline_matches=True,
        hold_reject_author_owner_id="owner-1",
        hold_reject_author_run_id="run-1",
        hold_reject_author_lease_id="lease-1",
        hold_reject_author_lease_expires_at=NOW + timedelta(minutes=5),
        hold_reject_registered_agent_matches=True,
    )
    return snapshot, intent


def test_hold_reject_is_internal_and_does_not_widen_shared_workflow_actions() -> None:
    definition = definition_for("hold-reject")
    assert (
        definition.profile,
        definition.principal,
        definition.request_replay,
        definition.task_required,
        definition.operation_required,
        definition.action_exposed,
        definition.workflow_action,
    ) == ("L", "agent", True, True, True, False, None)
    assert "hold-reject" not in ACTION_COMMANDS
    snapshot, intent = _valid_hold_reject_planner_state()
    assert legal_actions(snapshot.workflow) == ["prepare"]
    plan = plan_command(snapshot=snapshot, intent=intent, pinned_now=NOW)
    assert plan.legal is True
    assert [mutation.kind for mutation in plan.mutations] == [
        "open_evidence_hold",
        "advance_operation",
    ]
    assert plan.projection_intents == ()
    assert effect_spec_for("hold-reject", intent.arguments).projection_event_types == ()


def test_hold_reject_planner_fails_closed_for_every_occurrence_fence() -> None:
    snapshot, intent = _valid_hold_reject_planner_state()
    workflow = snapshot.workflow
    assert workflow is not None
    invalid = [
        replace(snapshot, workflow=replace(workflow, operation_status="completed")),
        replace(snapshot, workflow=replace(workflow, operation_kind="change")),
        replace(snapshot, workflow=replace(workflow, operation_phase="await_verification")),
        replace(snapshot, workflow=replace(workflow, persisted_actions=("prepare", "reject"))),
        replace(snapshot, hold_reject_cycle_exists=True),
        replace(snapshot, hold_reject_evidence_hold_exists=True),
        replace(snapshot, hold_reject_human_review_exists=True),
        replace(snapshot, hold_reject_baseline_matches=False),
        replace(snapshot, hold_reject_candidate_activation_exists=True),
        replace(snapshot, hold_reject_author_owner_id="other-owner"),
        replace(snapshot, hold_reject_author_run_id="other-run"),
        replace(snapshot, hold_reject_author_lease_id=None),
        replace(snapshot, hold_reject_author_lease_expires_at=NOW),
        replace(snapshot, hold_reject_registered_agent_matches=False),
    ]
    for candidate in invalid:
        plan = plan_command(snapshot=candidate, intent=intent, pinned_now=NOW)
        assert plan.legal is False
        assert plan.result_code == "ACTION_NOT_LEGAL"
    wrong_principal = replace(intent, principal_class="verification")
    plan = plan_command(snapshot=snapshot, intent=wrong_principal, pinned_now=NOW)
    assert plan.legal is False
    assert plan.result_code == "PRINCIPAL_SCOPE_MISMATCH"


def test_hold_reject_rejects_invalid_preconstruction_payloads(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=run_id)
        base = {
            "task_id": str(task_id),
            "operation_id": started.data["operation_id"],
            "route": "evidence",
            "reason": "source evidence is required before construction",
            "resume_status": "pending-research",
        }
        cases = (
            ({"route": "human-review"}, "INVALID_REJECTION_ROUTE"),
            ({"resume_status": "pending-verification"}, "INVALID_RESUME_STATUS"),
            ({"reason": ""}, "REJECTION_REASON_REQUIRED"),
            ({"file_text": "unexpected"}, "PRECONSTRUCTION_CANDIDATE_UNEXPECTED"),
            ({"model": "o3"}, "PRECONSTRUCTION_CANDIDATE_UNEXPECTED"),
            ({"independence_attestation": "unexpected"}, "PRECONSTRUCTION_CANDIDATE_UNEXPECTED"),
        )
        for override, expected_code in cases:
            result = port.execute(
                _call(
                    "hold-reject",
                    run_id=run_id,
                    request_id=_next(ids),
                    arguments={**base, **override},
                )
            )
            assert result.ok is False
            assert result.code == expected_code
        operation = session.get(wf.WorkflowOperation, uuid.UUID(started.data["operation_id"]))
        assert operation.phase == "prepare_required"
        assert operation.persisted_actions == ["prepare"]
        assert session.scalar(
            select(func.count()).select_from(wf.EvidenceHold).where(
                wf.EvidenceHold.operation_id == operation.operation_id
            )
        ) == 0


def test_hold_reject_creates_only_preconstruction_evidence_hold_and_replays(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    run_id, request_id = _next(ids), _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=run_id)
        operation_id = uuid.UUID(started.data["operation_id"])
        state = session.get(models.DishState, (context["generation_id"], task_id))
        baseline_content_version_id = state.current_content_version_id
        projection_count = session.scalar(select(func.count()).select_from(tx.ProjectionOutboxEvent))
        call = _call(
            "hold-reject",
            run_id=run_id,
            request_id=request_id,
            arguments={
                "task_id": str(task_id),
                "operation_id": str(operation_id),
                "route": "evidence",
                "reason": "source evidence is required before construction",
                "resume_status": "pending-research",
            },
        )
        result = port.execute(call)
        replay = port.execute(call)
        operation = session.get(wf.WorkflowOperation, operation_id)
        hold = session.scalar(
            select(wf.EvidenceHold).where(wf.EvidenceHold.operation_id == operation_id)
        )
        state_after = session.get(models.DishState, (context["generation_id"], task_id))
        assert result.ok is True
        assert replay.ok is True and replay.request_replayed is True
        assert hold is not None and hold.state == "open" and hold.cycle_id is None
        assert hold.baseline_content_version_id == baseline_content_version_id
        assert operation.lifecycle == "open"
        assert operation.phase == "held_evidence"
        assert operation.persisted_actions == ["supply-evidence"]
        assert state_after.current_content_version_id == baseline_content_version_id
        assert session.scalar(
            select(func.count()).select_from(wf.VerificationCycle).where(
                wf.VerificationCycle.operation_id == operation_id
            )
        ) == 0
        assert session.scalar(select(func.count()).select_from(tx.ProjectionOutboxEvent)) == projection_count
        assert session.scalar(
            select(func.count()).select_from(wf.EvidenceHold).where(
                wf.EvidenceHold.operation_id == operation_id
            )
        ) == 1

        with pytest.raises(RequestIdentityConflict):
            port.execute(
                _call(
                    "hold-reject",
                    run_id=run_id,
                    request_id=request_id,
                    arguments={
                        "task_id": str(task_id),
                        "operation_id": str(operation_id),
                        "route": "evidence",
                        "reason": "different logical request",
                        "resume_status": "pending-research",
                    },
                )
            )
        assert session.scalar(
            select(func.count()).select_from(wf.EvidenceHold).where(
                wf.EvidenceHold.operation_id == operation_id
            )
        ) == 1


def test_verification_evidence_reject_remains_verification_reject(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        port, _author_run, verifier_run, started, prepared, _inspected = _verification_ready(
            session, ids, context, task_id
        )
        operation_id = uuid.UUID(started.data["operation_id"])
        projection_count = session.scalar(select(func.count()).select_from(tx.ProjectionOutboxEvent))
        rejected = port.execute(
            _call(
                "reject",
                run_id=verifier_run,
                request_id=_next(ids),
                owner="verifier-owner",
                principal="verification",
                arguments={
                    "task_id": str(task_id),
                    "operation_id": str(operation_id),
                    "agent": "codex",
                    "route": "evidence",
                    "reason": "verification needs more evidence",
                },
            )
        )
        hold = session.scalar(
            select(wf.EvidenceHold).where(wf.EvidenceHold.operation_id == operation_id)
        )
        assert rejected.ok is True and rejected.command == "reject"
        assert hold is not None and hold.cycle_id is not None
        assert hold.baseline_content_version_id != uuid.UUID(prepared.data["content_version_id"])
        assert session.scalar(select(func.count()).select_from(tx.ProjectionOutboxEvent)) == projection_count + 1


def test_shadow_reject_translation_requires_source_preconstruction_proof_and_namespaces_request() -> None:
    operation_id = uuid.uuid4()
    envelope = SimpleNamespace(
        command_name="reject",
        shadow_baseline_id=uuid.uuid4(),
        source_authority_generation="legacy-1",
        source_request_identity="legacy-reject-1",
        pinned_inputs={"capture_schema": 3},
        source_pre_state={
            "selected_tables": ["operations"],
            "tables": {
                "operations": [
                    {
                        "operation_id": str(operation_id),
                        "status": "open",
                        "operation_kind": "initial",
                        "phase": "prepare_required",
                        "content_write_completed_at": None,
                    }
                ]
            },
        },
    )
    arguments = {
        "submission_id": str(operation_id),
        "route": "evidence",
        "reason": "need source evidence",
        "resume_status": "pending-research",
    }
    assert _semantic_shadow_command(envelope, arguments) == "hold-reject"
    translated_id = _semantic_shadow_request_id(
        envelope, target_command_name="hold-reject"
    )
    assert translated_id == _semantic_shadow_request_id(
        envelope, target_command_name="hold-reject"
    )
    assert translated_id != _shadow_uuid(
        envelope, label="request", value=envelope.source_request_identity
    )

    verification_envelope = SimpleNamespace(
        **{
            **envelope.__dict__,
            "source_pre_state": {
                "selected_tables": ["operations"],
                "tables": {
                    "operations": [
                        {
                            "operation_id": str(operation_id),
                            "status": "open",
                            "operation_kind": "initial",
                            "phase": "await_verification",
                            "content_write_completed_at": NOW.isoformat(),
                        }
                    ]
                },
            },
        }
    )
    assert _semantic_shadow_command(verification_envelope, arguments) == "reject"
    assert _semantic_shadow_request_id(
        verification_envelope, target_command_name="reject"
    ) == _shadow_uuid(
        verification_envelope,
        label="request",
        value=verification_envelope.source_request_identity,
    )


def test_verifier_reconstruction_is_bound_to_latest_verification_cycle(workflow_db) -> None:
    return _case_test_verifier_reconstruction_is_bound_to_latest_verification_cycle(workflow_db)


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
        state = session.get(models.DishState, (context["generation_id"], task_id))
        membership = session.get(
            models.TaskMembershipHead, (context["generation_id"], task_id)
        )
        replacement_id = _next(ids)
        body = "Drifted after signoff\n---\nStatus: pending-verification\n"
        mutation = DishRepository(session, uuid_factory=lambda: _next(ids)).begin_scalar_mutation(
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
        mutation.replace_content(
            title="[ready] Exact imported task",
            body=body,
            identity_scheme="sha256-title-body-v1",
            content_identity=hashlib.sha256(
                ("[ready] Exact imported task\0" + body).encode()
            ).hexdigest(),
            contract_binding_id=context["binding_id"],
            predecessor_content_version_id=state.current_content_version_id,
            content_version_id=replacement_id,
        )
        mutation.finalize()
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


def test_hold_reject_supply_evidence_resumes_preconstruction_baseline(workflow_db) -> None:
    factory, ids, context, _fixture_task_id = workflow_db
    author_run, admin_run = _next(ids), _next(ids)
    with session_scope(factory) as session:
        title, body = PENDING_RESEARCH_TASK.split("\n", 1)
        task_id = _next(ids)
        CoreAuthorityService(
            session, uuid_factory=lambda: _next(ids)
        ).import_task_document(
            generation_id=context["generation_id"],
            import_run_id=context["import_run_id"],
            contract_binding_id=context["binding_id"],
            spec=ImportedTaskSpec(
                task_id=task_id,
                asana_task_gid="123456790",
                title=title,
                body=body,
                identity_scheme="legacy-sha256-v1",
                content_identity=hashlib.sha256(
                    (title + "\0" + body).encode()
                ).hexdigest(),
                project_ids=(context["project_id"],),
                section_id=context["section_id"],
                completed=False,
                observed_at=NOW,
            ),
        )

        _register_run(
            session, generation_id=context["generation_id"], run_id=author_run
        )
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=admin_run,
            owner="Marco",
            agent="claude",
        )
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=author_run)
        operation_id = uuid.UUID(started.data["operation_id"])
        state = session.get(models.DishState, (context["generation_id"], task_id))
        baseline_content_version_id = state.current_content_version_id
        version_count = session.scalar(
            select(func.count())
            .select_from(models.ContentVersion)
            .where(models.ContentVersion.task_id == task_id)
        )
        projection_count = session.scalar(
            select(func.count()).select_from(tx.ProjectionOutboxEvent)
        )

        held = port.execute(
            _call(
                "hold-reject",
                run_id=author_run,
                request_id=_next(ids),
                arguments={
                    "task_id": str(task_id),
                    "operation_id": str(operation_id),
                    "route": "evidence",
                    "reason": "source evidence is required before construction",
                    "resume_status": "pending-research",
                },
            )
        )
        assert held.ok is True
        hold_id = uuid.UUID(held.data["hold_id"])

        request_id = _next(ids)
        call = _call(
            "supply-evidence",
            run_id=admin_run,
            request_id=request_id,
            owner="Marco",
            principal="admin",
            arguments={
                "task_id": str(task_id),
                "operation_id": str(operation_id),
                "hold_id": str(hold_id),
                "detail": "the missing source evidence is now available",
                "resume_status": "pending-research",
            },
        )
        supplied = port.execute(call)
        replay = port.execute(call)

        operation = session.get(wf.WorkflowOperation, operation_id)
        hold = session.get(wf.EvidenceHold, hold_id)
        state_after = session.get(
            models.DishState, (context["generation_id"], task_id)
        )
        assert supplied.ok is True
        assert supplied.data["resume_status"] == "pending-research"
        assert supplied.data["baseline_content_version_id"] == str(
            baseline_content_version_id
        )
        assert supplied.data["cycle_id"] is None
        assert supplied.data["projection_event_id"] is None
        assert supplied.data["phase"] == "prepare_required"
        assert replay.ok is True and replay.request_replayed is True
        assert hold is not None and hold.state == "supplied" and hold.cycle_id is None
        assert operation.lifecycle == "open"
        assert operation.phase == "prepare_required"
        assert operation.persisted_actions == ["prepare"]
        assert state_after.current_content_version_id == baseline_content_version_id
        assert session.scalar(
            select(func.count())
            .select_from(models.ContentVersion)
            .where(models.ContentVersion.task_id == task_id)
        ) == version_count
        assert session.scalar(
            select(func.count())
            .select_from(wf.VerificationCycle)
            .where(wf.VerificationCycle.operation_id == operation_id)
        ) == 0
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionOutboxEvent)
        ) == projection_count
        assert session.scalar(
            select(func.count())
            .select_from(wf.EvidenceHoldEvent)
            .where(
                wf.EvidenceHoldEvent.hold_id == hold_id,
                wf.EvidenceHoldEvent.event_kind == "supplied",
            )
        ) == 1

def test_cooked_uses_completion_authority_and_replay_idempotently(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    run_id, request_id = _next(ids), _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        definition = definition_for("cooked")
        assert (
            definition.principal,
            definition.request_replay,
            definition.task_required,
            definition.operation_required,
            definition.action_exposed,
        ) == ("agent", True, True, False, False)
        assert "cooked" not in ACTION_COMMANDS

        before_page = PostgresReadModel(session, cursor_secret=b"r" * 32).section_tasks(
            section_reference=context["section_id"]
        )
        assert [item.task_id for item in before_page.items] == [task_id]
        projection_count = session.scalar(
            select(func.count()).select_from(tx.ProjectionOutboxEvent)
        )
        call = _call(
            "cooked",
            run_id=run_id,
            request_id=request_id,
            arguments={"task_id": str(task_id)},
        )

        first = port.execute(call)
        replay = port.execute(call)

        assert first.ok is True
        assert first.data["completed"] is True
        assert first.data["completion_reason"] == "cooked"
        assert first.data["completion_state"] == "cooked"
        assert replay.ok is True
        assert replay.request_replayed is True
        assert replay.data == first.data
        assert session.scalar(select(func.count()).select_from(tx.ProjectionOutboxEvent)) == projection_count
        current = session.get(
            models.DishState, (context["generation_id"], task_id)
        )
        assert current is not None and current.completed is True
        assert current.archived_at is None
        latest = session.get(
            models.DishMutationReceipt,
            (context["generation_id"], task_id, current.completion_version),
        )
        assert latest is not None
        assert latest.completion_changed is True
        assert latest.command_execution_id is not None
        assert session.scalar(
            select(func.count())
            .select_from(models.DishMutationReceipt)
            .where(
                models.DishMutationReceipt.task_id == task_id,
                models.DishMutationReceipt.completion_changed.is_(True),
                models.DishMutationReceipt.command_execution_id.is_not(None),
            )
        ) == 1

        view = PostgresReadModel(session, cursor_secret=b"r" * 32).task_view(task_id)
        assert view.completed is True
        assert view.completion_reason == "cooked"
        assert view.completion_state == "cooked"
        after_page = PostgresReadModel(session, cursor_secret=b"r" * 32).section_tasks(
            section_reference=context["section_id"]
        )
        assert after_page.items == ()


def test_archive_alone_accepts_private_admin_principal_without_projection(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        before_projection_count = session.scalar(
            select(func.count()).select_from(tx.ProjectionOutboxEvent)
        )
        before = session.get(models.DishState, (context["generation_id"], task_id))
        assert before is not None
        before_completion = (before.completed, before.completion_reason)
        before_completion_version = before.completion_version

        archived = port.execute(
            _call(
                "archive",
                run_id=run_id,
                request_id=_next(ids),
                principal="admin",
                arguments={"task_id": str(task_id), "confirmed": True},
            )
        )

        assert definition_for("archive").admin_exposed is True
        assert definition_for("inspect").admin_exposed is True
        assert definition_for("cooked").admin_exposed is False
        assert archived.ok is True
        assert archived.data["completion_state"] == "archived"
        assert archived.data["system_reason"] == "admin_archive"
        assert archived.data["authority_mode"] == "postgresql"
        current = session.get(models.DishState, (context["generation_id"], task_id))
        assert current is not None and current.archived_at is not None
        assert (current.completed, current.completion_reason) == before_completion
        assert current.completion_version == before_completion_version
        receipt = session.get(
            models.DishMutationReceipt,
            (context["generation_id"], task_id, current.dish_version),
        )
        assert receipt is not None
        assert receipt.archive_changed is True
        assert receipt.completion_changed is False
        assert archived.data["completed"] is False
        assert archived.data["completion_reason"] == before_completion[1]
        view = PostgresReadModel(session, cursor_secret=b"r" * 32).task_view(task_id)
        assert view.completion_state == "archived"
        assert view.completed is False
        archive = FrontendBoardQuery(session).archived_tasks(max_results=10)
        assert [item.task_id for item in archive.results] == [task_id]
        page = PostgresReadModel(session, cursor_secret=b"r" * 32).section_tasks(
            section_reference=context["section_id"]
        )
        assert task_id not in {item.task_id for item in page.items}
        board = FrontendBoardQuery(session)
        assert task_id not in {
            item.task_id
            for item in board.active_cards(
                registry=board.bootstrap_registry(),
                projection_delay=timedelta(minutes=15),
                max_cards=10,
            )
        }
        assert task_id not in {
            item.task_id
            for item in board.search_titles(
                query="Dish",
                projection_delay=timedelta(minutes=15),
                max_results=10,
            ).results
        }
        assert session.scalar(
            select(func.count()).select_from(tx.ProjectionOutboxEvent)
        ) == before_projection_count
        assert session.get(models.DishTask, task_id) is not None


def test_archive_admin_principal_cannot_bypass_confirmation(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        result = _port(session, ids).execute(
            _call(
                "archive",
                run_id=run_id,
                request_id=_next(ids),
                principal="admin",
                arguments={"task_id": str(task_id)},
            )
        )

        assert result.code == "CONFIRMATION_REQUIRED"
        assert PostgresReadModel(
            session, cursor_secret=b"r" * 32
        ).task_view(task_id).completed is False


def test_newly_created_incomplete_archive_reason_remains_active_not_archived(
    workflow_db,
) -> None:
    factory, ids, context, _task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        created = port.execute(
            _call(
                "create",
                run_id=run_id,
                request_id=_next(ids),
                arguments={"title": "New incomplete Dish"},
            )
        )
        assert created.ok is True
        task_id = uuid.UUID(created.data["task_id"])
        current = session.get(
            models.DishState, (context["generation_id"], task_id)
        )
        assert current is not None and current.completed is False
        assert current.completion_reason == "archive"
        assert current.archived_at is None
        assert FrontendBoardQuery(session).archived_tasks(max_results=10).results == ()

        view = PostgresReadModel(session, cursor_secret=b"r" * 32).task_view(task_id)

        assert view.completed is False
        assert view.completion_reason == "archive"
        assert view.completion_state == "active"
        page = PostgresReadModel(session, cursor_secret=b"r" * 32).section_tasks(
            section_reference=context["section_id"]
        )
        assert task_id in {item.task_id for item in page.items}


@pytest.mark.parametrize("command_name", ("cooked", "archive"))
def test_cooked_and_archive_reject_open_workflow_without_partial_completion(
    workflow_db, command_name: str
) -> None:
    factory, ids, context, task_id = workflow_db
    run_id = _next(ids)
    with session_scope(factory) as session:
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        port = _port(session, ids)
        started = _start_initial(port, ids, task_id=task_id, run_id=run_id)
        assert started.ok is True
        before = session.get(
            models.DishState, (context["generation_id"], task_id)
        )
        assert before is not None
        before_identity = (before.dish_version, before.completion_version, before.completed)

        result = port.execute(
            _call(
                command_name,
                run_id=run_id,
                request_id=_next(ids),
                arguments={"task_id": str(task_id)},
            )
        )

        assert result.ok is False
        assert result.code == "TASK_NOT_RESTING"
        assert "open_operation_id" in result.data["guidance"]
        current = session.get(
            models.DishState, (context["generation_id"], task_id)
        )
        assert current is not None
        assert (current.dish_version, current.completion_version, current.completed) == before_identity
