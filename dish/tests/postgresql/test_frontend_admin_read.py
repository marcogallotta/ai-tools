from __future__ import annotations

import hashlib
from datetime import timedelta

from dish_pg.database import session_scope
from dish_pg.frontend_admin_query import FrontendAdminQuery
from dish_pg.services import CoreAuthorityService, ImportedTaskSpec
from tests.support.postgresql.core import NOW, _bootstrap_registry, _next, core_db


def _import(session, ids, context):
    title = "Admin-visible dish"
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
    assert facts.events == ()
