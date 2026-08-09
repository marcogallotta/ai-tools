from __future__ import annotations

import hashlib
from datetime import timedelta
from uuid import UUID

import pytest

from dish_pg import models
from dish_pg.database import session_scope
from dish_pg.frontend_detail_query import FrontendDetailQuery, TaskDetailIneligible
from dish_pg.services import CoreAuthorityService, ImportedTaskSpec
from dish_service.frontend_detail import FrontendDetailConfig, FrontendDetailService
from dish_service.frontend_tokens import route_identity
from tests.support.postgresql.core import NOW, _bootstrap_registry, _next, core_db

SECRET = b"stage-4-detail-query-secret-is-at-least-32-bytes"


def _import(session, ids, context, *, title: str, asana_gid: str, completed: bool = False):
    body = "Canonical body\n---\nStatus: ready\n"
    return CoreAuthorityService(session, uuid_factory=lambda: _next(ids)).import_task_document(
        generation_id=context["generation_id"],
        import_run_id=context["import_run_id"],
        contract_binding_id=context["binding_id"],
        spec=ImportedTaskSpec(
            task_id=_next(ids),
            asana_task_gid=asana_gid,
            title=title,
            body=body,
            identity_scheme="legacy-sha256-v1",
            content_identity=hashlib.sha256((title + "\0" + body).encode()).hexdigest(),
            project_ids=(context["project_id"],),
            section_id=context["section_id"],
            completed=completed,
            observed_at=NOW,
        ),
    )


def _service(session):
    return FrontendDetailService(
        FrontendDetailQuery(session),
        environment="test",
        token_secret=SECRET,
        config=FrontendDetailConfig(projection_delay=timedelta(minutes=15)),
    )


def _route(task_id: UUID) -> str:
    return route_identity(secret=SECRET, environment="test", kind="task", object_id=task_id)


def test_detail_query_reads_current_eligible_content_and_isolated_state(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head="0032_imported_operation_history"
        )
        imported = _import(session, ids, context, title="Exact detail", asana_gid="4001")
        session.get(models.DishTask, imported.task_id).existence_state = "isolated"

    with session_scope(factory) as session:
        service = _service(session)
        facts = service.capture(_route(imported.task_id))
        payload = service.present(facts)

    assert payload["task_id"] == _route(imported.task_id)
    assert payload["title"] == "Exact detail"
    assert payload["section_label"] == "Research Queue"
    assert payload["attention_codes"][0] == "isolated"
    assert str(imported.task_id) not in str(payload)


def test_detail_uses_canonical_workflow_role_name_for_import_placeholder(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
            section_display_name="Imported section 1216891250619908",
            section_workflow_role="research_queue",
        )
        imported = _import(session, ids, context, title="Named section", asana_gid="4003")

    with session_scope(factory) as session:
        payload = _service(session).present(_service(session).capture(_route(imported.task_id)))

    assert payload["section_label"] == "Research Queue"


def test_detail_query_rejects_completed_task_after_route_resolution(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session, ids, generation_status="active", schema_head="0032_imported_operation_history"
        )
        imported = _import(
            session,
            ids,
            context,
            title="Completed",
            asana_gid="4002",
            completed=True,
        )

    with session_scope(factory) as session:
        with pytest.raises(TaskDetailIneligible):
            _service(session).capture(_route(imported.task_id))
