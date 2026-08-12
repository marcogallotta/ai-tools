from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_pg.frontend_admin_query import FrontendAdminQuery
from dish_pg.services import CoreAuthorityService, ImportedTaskSpec
from dish_pg.workflow import WorkflowAuthorityService
from dish_service.frontend_admin import FrontendAdminConfig, FrontendAdminService
from tests.support.postgresql.core import NOW, _bootstrap_registry, _next, core_db
from tests.support.postgresql.workflow import (
    _admit,
    _execution,
    _next as _workflow_next,
    _register_run,
    workflow_db,
)


def _import(session, ids, context, *, title: str = "Admin-visible dish"):
    body = "Canonical body\n---\nStatus: ready\n"
    return CoreAuthorityService(session, uuid_factory=lambda: _next(ids)).import_task_document(
        generation_id=context["generation_id"],
        import_run_id=context["import_run_id"],
        contract_binding_id=context["binding_id"],
        spec=ImportedTaskSpec(
            task_id=_next(ids),
            asana_task_gid="91001",
            title=title,
            body=body,
            identity_scheme="legacy-sha256-v1",
            content_identity=hashlib.sha256((title + "\0" + body).encode()).hexdigest(),
            project_ids=(context["project_id"],),
            section_id=context["section_id"],
            completed=False,
            observed_at=NOW,
        ),
    )


def _service(session) -> FrontendAdminService:
    return FrontendAdminService(
        FrontendAdminQuery(session),
        environment="test",
        config=FrontendAdminConfig(projection_delay=timedelta(minutes=15)),
    )


def test_admin_query_reuses_active_board_scope_and_bounds_events(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
        )
        imported = _import(session, ids, context)

    with session_scope(factory) as session:
        facts = FrontendAdminQuery(session).capture(
            projection_delay=timedelta(minutes=15),
            max_cards=10,
            max_events=10,
        )

    assert [card.task_id for card in facts.cards] == [imported.task_id]
    assert facts.sections[0].section_label == "Research Queue"
    assert facts.sections[0].workflow_role == "research_queue"
    assert facts.events == ()
    assert facts.human_reviews == ()


def test_admin_queue_work_is_derived_from_real_registry_role(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
            section_display_name="Imported section 1216891250619908",
            section_workflow_role="verification_queue",
        )
        imported = _import(session, ids, context, title="Verify me")

    with session_scope(factory) as session:
        payload = _service(session).read()

    assert payload["summary"] == {
        "needs_you": 0,
        "human_review": 0,
        "recovery": 0,
        "workflow_queue": 1,
        "research": 0,
        "verification": 1,
        "system_activity": 0,
        "affected_dishes": 1,
    }
    assert payload["dishes"] == [
        {
            "task_id": str(imported.task_id),
            "title": "Verify me",
            "section_label": "Verification Queue",
            "workflow_status": {"state": "no_active_operation"},
            "bucket": "workflow_queue",
            "attention": [
                {
                    "code": "verification_required",
                    "label": "Needs verification",
                    "message": "This dish is waiting in the Verification queue.",
                }
            ],
            "last_activity_at": None,
            "diagnostics": {"attention_codes": ["verification_required"]},
        }
    ]


def test_admin_reads_real_open_human_review_expired_lease_and_uncertainty(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    wall_now = datetime.now(timezone.utc)
    question = "Marco must decide: " + ("material evidence and consequence; " * 160)
    assert len(question) > 4096
    with session_scope(factory) as session:
        run_id = _workflow_next(ids)
        request_id = _workflow_next(ids)
        execution_id = _workflow_next(ids)
        operation_id = _workflow_next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        workflow = WorkflowAuthorityService(session, uuid_factory=lambda: _workflow_next(ids))
        _admit(
            workflow,
            request_id=request_id,
            generation_id=context["generation_id"],
            run_id=run_id,
        )
        _execution(
            workflow,
            execution_id=execution_id,
            request_id=request_id,
            generation_id=context["generation_id"],
            task_id=task_id,
            binding_id=context["binding_id"],
        )
        workflow.repo.capture_task_fence(
            execution_id=execution_id,
            generation_id=context["generation_id"],
            task_id=task_id,
            at=NOW,
        )
        workflow.create_operation(
            operation_id=operation_id,
            execution_id=execution_id,
            task_id=task_id,
            kind="initial",
            phase="held_human",
            persisted_actions=["prepare"],
            created_at=NOW,
        )
        workflow.acquire_actor_lease(
            lease_id=_workflow_next(ids),
            execution_id=execution_id,
            operation_id=operation_id,
            run_id=run_id,
            owner_id="owner-1",
            actor_role="constructor",
            actor_attempt_sequence=1,
            issued_at=wall_now - timedelta(hours=2),
            expires_at=wall_now - timedelta(hours=1),
        )
        head = session.get(models.TaskAuthorityHead, (context["generation_id"], task_id))
        assert head is not None
        activation = session.get(models.ContentActivation, head.current_content_activation_id)
        assert activation is not None
        workflow.open_human_review(
            requirement_id=_workflow_next(ids),
            execution_id=execution_id,
            operation_id=operation_id,
            route="human_review",
            question=question,
            baseline_content_version_id=activation.content_version_id,
            opened_at=NOW,
        )
        session.add(
            wf.HumanReviewRequirement(
                requirement_id=_workflow_next(ids),
                generation_id=context["generation_id"],
                task_id=task_id,
                operation_id=operation_id,
                cycle_id=None,
                route="human_review",
                question="Old resolved question",
                baseline_content_version_id=activation.content_version_id,
                state="decided",
                opened_by_execution_id=execution_id,
                opened_at=NOW - timedelta(hours=2),
                terminal_at=NOW - timedelta(hours=1),
            )
        )
        execution = session.get(wf.CommandExecution, execution_id)
        assert execution is not None
        execution.status = "uncertain"
        execution.terminal_at = NOW + timedelta(minutes=2)
        execution.execution_revision += 1

    with session_scope(factory) as session:
        facts = FrontendAdminQuery(session).capture(
            projection_delay=timedelta(minutes=15),
            max_cards=10,
            max_events=10,
        )
        payload = _service(session).read()

    assert len(facts.human_reviews) == 1
    assert facts.human_reviews[0].task_id == task_id
    assert facts.human_reviews[0].operation_id == operation_id
    assert facts.human_reviews[0].question == question
    assert payload["summary"]["needs_you"] == 1
    assert payload["summary"]["human_review"] == 1
    assert payload["summary"]["recovery"] == 1
    assert payload["summary"]["workflow_queue"] == 0
    assert payload["summary"]["affected_dishes"] == 1
    assert payload["dishes"][0]["bucket"] == "needs_you"
    assert payload["dishes"][0]["diagnostics"]["attention_codes"] == [
        "lease_attention",
        "verification_attention",
        "recovery_required",
    ]
    review_attention = next(
        item for item in payload["dishes"][0]["attention"] if item["code"] == "verification_attention"
    )
    assert review_attention["message"] == question


def test_admin_reads_real_open_operation_as_system_activity(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run_id = _workflow_next(ids)
        request_id = _workflow_next(ids)
        execution_id = _workflow_next(ids)
        operation_id = _workflow_next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        workflow = WorkflowAuthorityService(session, uuid_factory=lambda: _workflow_next(ids))
        _admit(
            workflow,
            request_id=request_id,
            generation_id=context["generation_id"],
            run_id=run_id,
        )
        _execution(
            workflow,
            execution_id=execution_id,
            request_id=request_id,
            generation_id=context["generation_id"],
            task_id=task_id,
            binding_id=context["binding_id"],
        )
        workflow.repo.capture_task_fence(
            execution_id=execution_id,
            generation_id=context["generation_id"],
            task_id=task_id,
            at=NOW,
        )
        workflow.create_operation(
            operation_id=operation_id,
            execution_id=execution_id,
            task_id=task_id,
            kind="initial",
            phase="prepare_required",
            persisted_actions=["prepare"],
            created_at=NOW,
        )

    with session_scope(factory) as session:
        facts = FrontendAdminQuery(session).capture(
            projection_delay=timedelta(minutes=15),
            max_cards=10,
            max_events=10,
        )

    assert len(facts.cards) == 1
    assert facts.cards[0].operation_kind == "initial"
    assert facts.cards[0].operation_phase == "prepare_required"

    with session_scope(factory) as session:
        payload = _service(session).read()

    assert payload["summary"]["needs_you"] == 0
    assert payload["summary"]["workflow_queue"] == 0
    assert payload["summary"]["system_activity"] == 1
    assert payload["summary"]["affected_dishes"] == 1
    assert payload["dishes"][0]["bucket"] == "system_activity"
    assert payload["dishes"][0]["workflow_status"] == {
        "state": "active_operation",
        "operation": "Initial",
        "phase": "Prepare required",
    }
    assert payload["dishes"][0]["attention"] == []
    assert payload["dishes"][0]["diagnostics"]["attention_codes"] == []


def test_admin_reads_real_persisted_system_activity(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
            section_display_name="Operations",
            section_workflow_role="operations",
        )
        imported = _import(session, ids, context, title="Isolated dish")
        task = session.get(models.DishTask, imported.task_id)
        assert task is not None
        task.existence_state = "isolated"

    with session_scope(factory) as session:
        payload = _service(session).read()

    assert payload["summary"]["needs_you"] == 0
    assert payload["summary"]["system_activity"] == 1
    assert payload["summary"]["affected_dishes"] == 1
    assert payload["dishes"][0]["bucket"] == "system_activity"
    assert payload["dishes"][0]["diagnostics"]["attention_codes"] == ["isolated"]
