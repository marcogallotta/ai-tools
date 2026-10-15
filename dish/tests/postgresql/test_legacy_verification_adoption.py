from __future__ import annotations

import uuid

from sqlalchemy import func, select

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg.database import session_scope
from dish_tool.content_versions import CONTENT_IDENTITY_SCHEME, content_identity
from tests.support.canonical import TASK
from tests.support.postgresql.command import _call, _port
from tests.support.postgresql.workflow import NOW, _next, _register_run, workflow_db


def _make_stranded_import(
    session,
    ids,
    context,
    *,
    ordinary: bool = True,
    workflow_role: str = "verification_queue",
):
    title, body = TASK.split("\n", 1)
    task_id = _next(ids)
    content_version_id = _next(ids)
    catalog_id = _next(ids)
    membership_event_id = _next(ids)
    session.add(
        models.DishTask(
            task_id=task_id,
            existence_state="ordinary" if ordinary else "isolated",
            creation_route="import",
            import_run_id=context["import_run_id"],
            command_execution_id=None,
            created_at=NOW,
            retired_at=None,
        )
    )
    session.flush()
    session.add(
        models.DishMutationReceipt(
            generation_id=context["generation_id"],
            task_id=task_id,
            dish_version=1,
            source_route="import",
            import_run_id=context["import_run_id"],
            command_execution_id=None,
            content_changed=True,
            placement_changed=True,
            completion_changed=True,
            archive_changed=False,
            occurred_at=NOW,
        )
    )
    session.flush()
    session.add(
        models.SectionCatalogVersion(
            catalog_version_id=catalog_id,
            generation_id=context["generation_id"],
            version_number=1,
            contract_binding_id=context["binding_id"],
            catalog_sha256="a" * 64,
            source_registry_version_id=None,
            transform_sha256=None,
            created_at=NOW,
        )
    )
    session.flush()
    session.add(
        models.SectionCatalogEntry(
            catalog_version_id=catalog_id,
            section_id=context["section_id"],
            ordinal=0,
            display_name="Verification Queue",
            workflow_role=workflow_role,
        )
    )
    session.flush()
    version = models.ContentVersion(
        content_version_id=content_version_id,
        generation_id=context["generation_id"],
        task_id=task_id,
        representation_kind="document",
        title=title,
        body=body,
        identity_scheme=CONTENT_IDENTITY_SCHEME,
        content_identity=content_identity(title, body),
        creator_route="import",
        import_run_id=context["import_run_id"],
        command_execution_id=None,
        predecessor_content_version_id=None,
        contract_binding_id=context["binding_id"],
        created_dish_version=1,
        created_at=NOW,
    )
    session.add(version)
    session.add(
        models.DishState(
            generation_id=context["generation_id"],
            task_id=task_id,
            current_content_version_id=content_version_id,
            section_id=context["section_id"],
            registry_version_id=context["registry_version_id"],
            catalog_version_id=catalog_id,
            completed=False,
            completion_reason="imported",
            archived_at=None,
            dish_version=1,
            placement_version=1,
            completion_version=1,
            updated_at=NOW,
        )
    )
    session.add(
        models.TaskMembershipHead(
            generation_id=context["generation_id"],
            task_id=task_id,
            membership_revision=1,
            updated_at=NOW,
        )
    )
    session.add(
        models.TaskProjectMembershipEvent(
            membership_event_id=membership_event_id,
            generation_id=context["generation_id"],
            task_id=task_id,
            project_id=context["project_id"],
            event_kind="joined",
            membership_revision=1,
            provenance_route="import",
            import_run_id=context["import_run_id"],
            command_execution_id=None,
            occurred_at=NOW,
        )
    )
    session.flush()
    session.add(
        models.CurrentTaskProjectMembership(
            generation_id=context["generation_id"],
            task_id=task_id,
            project_id=context["project_id"],
            latest_event_id=membership_event_id,
            is_member=True,
            membership_revision=1,
            updated_at=NOW,
        )
    )
    session.flush()
    return task_id, version


def _admin_run(session, ids, context):
    run_id = _next(ids)
    _register_run(
        session,
        generation_id=context["generation_id"],
        run_id=run_id,
        owner="Marco",
        agent="marco",
    )
    return run_id


def test_private_admin_adopts_exact_imported_content_and_replays(workflow_db) -> None:
    factory, ids, context, _fixture_task_id = workflow_db
    with session_scope(factory) as session:
        task_id, version = _make_stranded_import(session, ids, context)
        before = (version.title, version.body, version.content_identity)
        run_id = _admin_run(session, ids, context)
        request_id = _next(ids)
        port = _port(session, ids)
        arguments = {
            "task_id": str(task_id),
            "reason": "Adopt the audited imported Research result.",
        }

        adopted = port.execute(
            _call(
                "adopt-legacy-verification",
                run_id=run_id,
                request_id=request_id,
                owner="Marco",
                principal="admin",
                arguments=arguments,
            )
        )
        replayed = port.execute(
            _call(
                "adopt-legacy-verification",
                run_id=run_id,
                request_id=request_id,
                owner="Marco",
                principal="admin",
                arguments=arguments,
            )
        )
        duplicate = port.execute(
            _call(
                "adopt-legacy-verification",
                run_id=run_id,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments=arguments,
            )
        )

        assert adopted.ok and adopted.data["adopted"] is True
        assert replayed.ok and replayed.request_replayed is True
        assert duplicate.code == "LEGACY_VERIFICATION_ADOPTION_INELIGIBLE"
        assert duplicate.data["blockers"]["open_operation_id"] == adopted.data[
            "operation_id"
        ]
        current = session.get(models.ContentVersion, version.content_version_id)
        assert current is not None
        assert (current.title, current.body, current.content_identity) == before
        operation = session.get(
            wf.WorkflowOperation, uuid.UUID(adopted.data["operation_id"])
        )
        cycle = session.get(wf.VerificationCycle, uuid.UUID(adopted.data["cycle_id"]))
        assert (
            operation.kind,
            operation.phase,
            operation.persisted_actions,
        ) == ("verification", "await_verification", ["inspect"])
        assert operation.creation_execution_id is not None
        assert cycle.reviewed_content_version_id == version.content_version_id
        assert cycle.created_by_execution_id == operation.creation_execution_id
        assert session.scalar(
            select(func.count())
            .select_from(wf.OperationActorFact)
            .where(wf.OperationActorFact.operation_id == operation.operation_id)
        ) == 0
        step = session.scalar(
            select(wf.OperationStep).where(
                wf.OperationStep.operation_id == operation.operation_id
            )
        )
        assert step.step_name == "legacy_verification_adoption"
        assert step.evidence["content_identity"] == before[2]


def test_read_routes_stranded_import_to_admin_then_verification(workflow_db) -> None:
    factory, ids, context, _fixture_task_id = workflow_db
    with session_scope(factory) as session:
        task_id, _version = _make_stranded_import(session, ids, context)
        port = _port(session, ids)

        before = port.execute(
            _call(
                "read",
                run_id=_next(ids),
                arguments={"dish_id": str(task_id), "agent": "claude"},
            )
        )
        assert before.ok
        assert before.allowed_actions == ()
        assert "required_start_kind" not in before.data
        assert before.data["required_admin_action"] == "adopt-legacy-verification"
        assert before.data["workflow_recovery"]["private_admin_only"] is True

        admin_run_id = _admin_run(session, ids, context)
        adopted = port.execute(
            _call(
                "adopt-legacy-verification",
                run_id=admin_run_id,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={
                    "task_id": str(task_id),
                    "reason": "Adopt exact imported content for independent review.",
                },
            )
        )
        assert adopted.ok

        after = port.execute(
            _call(
                "read",
                run_id=_next(ids),
                arguments={"dish_id": str(task_id), "agent": "claude"},
            )
        )
        assert after.ok
        assert after.state == "await_verification"
        assert "workflow_recovery" not in after.data


def test_adoption_rejects_nonordinary_imported_task(workflow_db) -> None:
    factory, ids, context, _fixture_task_id = workflow_db
    with session_scope(factory) as session:
        task_id, _version = _make_stranded_import(
            session, ids, context, ordinary=False
        )
        run_id = _admin_run(session, ids, context)
        result = _port(session, ids).execute(
            _call(
                "adopt-legacy-verification",
                run_id=run_id,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={"task_id": str(task_id), "reason": "test rejection"},
            )
        )

        assert result.code == "LEGACY_VERIFICATION_ADOPTION_INELIGIBLE"
        assert result.data["blockers"]["task_provenance"]["existence_state"] == (
            "isolated"
        )
        assert session.scalar(select(func.count()).select_from(wf.WorkflowOperation)) == 0


def test_adoption_never_repairs_wrong_placement(workflow_db) -> None:
    factory, ids, context, _fixture_task_id = workflow_db
    with session_scope(factory) as session:
        task_id, _version = _make_stranded_import(
            session, ids, context, workflow_role="research_queue"
        )
        run_id = _admin_run(session, ids, context)

        result = _port(session, ids).execute(
            _call(
                "adopt-legacy-verification",
                run_id=run_id,
                request_id=_next(ids),
                owner="Marco",
                principal="admin",
                arguments={"task_id": str(task_id), "reason": "test rejection"},
            )
        )

        assert result.code == "LEGACY_VERIFICATION_ADOPTION_INELIGIBLE"
        assert result.data["blockers"]["placement"] == "not_in_verification_queue"
        assert session.scalar(select(func.count()).select_from(wf.WorkflowOperation)) == 0
        state = session.get(models.DishState, (context["generation_id"], task_id))
        entry = session.get(
            models.SectionCatalogEntry,
            (state.catalog_version_id, state.section_id),
        )
        assert entry.workflow_role == "research_queue"


def test_agent_cannot_call_private_adoption(workflow_db) -> None:
    factory, ids, context, _fixture_task_id = workflow_db
    with session_scope(factory) as session:
        task_id, _version = _make_stranded_import(session, ids, context)
        run_id = _next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        result = _port(session, ids).execute(
            _call(
                "adopt-legacy-verification",
                run_id=run_id,
                request_id=_next(ids),
                arguments={"task_id": str(task_id), "reason": "forbidden"},
            )
        )
        assert result.code == "PRINCIPAL_SCOPE_MISMATCH"
