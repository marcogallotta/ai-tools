from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import event

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.frontend_board_query import FrontendBoardQuery
from dish_pg.services import CoreAuthorityService, ImportedTaskSpec
from dish_pg.transition import ProjectionService
from dish_pg.workflow import WorkflowAuthorityService
from dish_service.frontend_board import (
    BoardCapacityExceeded,
    FrontendBoardConfig,
    FrontendBoardService,
)
from dish_service.frontend_contract import (
    OPERATION_PRESENTATION_LABELS,
    PHASE_PRESENTATION_LABELS,
    WORKFLOW_PRESENTATION_LABEL_MAX_LENGTH,
)
from dish_service.frontend_tokens import route_identity
from tests.support.postgresql.core import (
    NOW,
    _activate_role_only_registry_revision,
    _bootstrap_registry,
    _next,
    core_db,
)
from tests.support.postgresql.workflow import (
    _admit,
    _execution,
    _register_run,
    workflow_db,
)
from tests.support.postgresql.workflow import _next as _workflow_next

SECRET = b"stage-3-board-test-secret-is-at-least-32-bytes"


def _config(
    *,
    first_page_size: int = 50,
    continuation_page_size: int = 50,
    max_sections: int = 100,
):
    return FrontendBoardConfig(
        first_page_size=first_page_size,
        continuation_page_size=continuation_page_size,
        max_sections=max_sections,
        projection_delay=timedelta(minutes=15),
    )


def _service(
    session,
    *,
    first_page_size: int = 50,
    continuation_page_size: int = 50,
    max_sections: int = 100,
):
    return FrontendBoardService(
        FrontendBoardQuery(session),
        environment="test",
        token_secret=SECRET,
        config=_config(
            first_page_size=first_page_size,
            continuation_page_size=continuation_page_size,
            max_sections=max_sections,
        ),
    )


def _task_route(task_id: UUID) -> str:
    return route_identity(
        secret=SECRET, environment="test", kind="task", object_id=task_id
    )


def _import_title(session, ids, context, *, title: str, asana_gid: str, completed: bool = False):
    service = CoreAuthorityService(session, uuid_factory=lambda: _next(ids))
    task_id = _next(ids)
    body = "Canonical body\n---\nStatus: ready\n"
    return service.import_task_document(
        generation_id=context["generation_id"],
        import_run_id=context["import_run_id"],
        contract_binding_id=context["binding_id"],
        spec=ImportedTaskSpec(
            task_id=task_id,
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


def _leave_project(session, ids, context, *, task_id: UUID) -> None:
    membership = session.get(
        models.CurrentTaskProjectMembership,
        (context["generation_id"], task_id, context["project_id"]),
    )
    head = session.get(
        models.TaskMembershipHead,
        (context["generation_id"], task_id),
    )
    assert membership is not None
    assert head is not None
    revision = membership.membership_revision + 1
    occurred_at = NOW + timedelta(seconds=revision)
    event_id = _next(ids)
    session.add(
        models.TaskProjectMembershipEvent(
            membership_event_id=event_id,
            generation_id=context["generation_id"],
            task_id=task_id,
            project_id=context["project_id"],
            event_kind="left",
            membership_revision=revision,
            provenance_route="import",
            import_run_id=context["import_run_id"],
            command_execution_id=None,
            occurred_at=occurred_at,
        )
    )
    session.flush()
    membership.latest_event_id = event_id
    membership.is_member = False
    membership.membership_revision = revision
    membership.updated_at = occurred_at
    head.membership_revision = revision
    head.updated_at = occurred_at


def test_workflow_status_openapi_matches_server_presentation_registry() -> None:
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "frontend" / "openapi" / "frontend.openapi.json").read_text()
    )
    variants = schema["components"]["schemas"]["WorkflowStatus"]["oneOf"]
    active = next(
        item
        for item in variants
        if "active_operation" in item["properties"]["state"]["enum"]
    )
    assert active["required"] == ["state", "operation", "phase"]
    assert active["properties"]["operation"]["enum"] == list(OPERATION_PRESENTATION_LABELS)
    assert active["properties"]["phase"]["enum"] == list(PHASE_PRESENTATION_LABELS)
    assert active["properties"]["operation"]["maxLength"] == WORKFLOW_PRESENTATION_LABEL_MAX_LENGTH
    assert active["properties"]["phase"]["maxLength"] == WORKFLOW_PRESENTATION_LABEL_MAX_LENGTH


def _open_operation(session, ids, context, task_id):
    run_id = _workflow_next(ids)
    request_id = _workflow_next(ids)
    execution_id = _workflow_next(ids)
    operation_id = _workflow_next(ids)
    _register_run(session, generation_id=context["generation_id"], run_id=run_id)
    service = WorkflowAuthorityService(session, uuid_factory=lambda: _workflow_next(ids))
    _admit(
        service,
        request_id=request_id,
        generation_id=context["generation_id"],
        run_id=run_id,
    )
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
    service.create_operation(
        operation_id=operation_id,
        execution_id=execution_id,
        task_id=task_id,
        kind="initial",
        phase="prepare_required",
        persisted_actions=["prepare"],
        created_at=NOW,
    )
    return service, run_id, execution_id, operation_id


def test_board_includes_isolated_and_paginates_without_consuming_retry(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
        )
        isolated = _import_title(session, ids, context, title="Alpha", asana_gid="1001")
        peer = _import_title(session, ids, context, title="alpha", asana_gid="1002")
        later = _import_title(session, ids, context, title="Beta", asana_gid="1003")
        completed = _import_title(
            session, ids, context, title="Completed", asana_gid="1004", completed=True
        )
        retired = _import_title(session, ids, context, title="Retired", asana_gid="1005")

        session.get(models.DishTask, isolated.task_id).existence_state = "isolated"
        retired_row = session.get(models.DishTask, retired.task_id)
        assert retired_row is not None
        retired_row.existence_state = "retired"
        retired_row.retired_at = NOW

    with session_scope(factory) as session:
        service = _service(session, first_page_size=2, continuation_page_size=2)
        board = service.bootstrap()
        assert len(board["sections"]) == 1
        section = board["sections"][0]
        expected_first = [_task_route(task_id) for task_id in sorted((isolated.task_id, peer.task_id))]
        assert [card["task_id"] for card in section["cards"]] == expected_first
        isolated_card = next(card for card in section["cards"] if card["task_id"] == _task_route(isolated.task_id))
        assert isolated_card["attention_codes"][0] == "isolated"
        assert {notice["code"] for notice in board["notices"]} == {"isolated"}
        assert section["next_cursor"] is not None

        first_retry = service.continuation(
            section_route_id=section["section_id"], cursor=section["next_cursor"]
        )
        second_retry = service.continuation(
            section_route_id=section["section_id"], cursor=section["next_cursor"]
        )
        assert first_retry == second_retry
        assert [card["task_id"] for card in first_retry["cards"]] == [_task_route(later.task_id)]
        assert first_retry["next_cursor"] is None

        returned = {
            card["task_id"]
            for card in section["cards"] + first_retry["cards"]
        }
        assert _task_route(completed.task_id) not in returned
        assert _task_route(retired.task_id) not in returned


def test_transition_registry_revision_does_not_rebind_native_board_placement(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
            section_workflow_role="imported-section-1217084794163035",
        )
        imported = _import_title(
            session,
            ids,
            context,
            title="Registry survivor",
            asana_gid="1099",
        )
        revised_registry = _activate_role_only_registry_revision(
            session,
            ids,
            context,
            workflow_role="research_queue",
        )
        state = session.get(
            models.DishState,
            (context["generation_id"], imported.task_id),
        )
        assert state is not None
        assert state.registry_version_id == context["registry_version_id"]
        assert state.catalog_version_id == context["catalog_version_id"]
        assert revised_registry != state.registry_version_id

    with session_scope(factory) as session:
        service = _service(session)
        board = service.bootstrap()
        assert board["sections"][0]["section_label"] == "Research Queue"
        assert [card["task_id"] for card in board["sections"][0]["cards"]] == [
            _task_route(imported.task_id)
        ]
        assert [item["task_id"] for item in service.search("survivor")["results"]] == [
            _task_route(imported.task_id)
        ]



def test_active_title_search_is_global_case_insensitive_and_board_eligible(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
        )
        exact = _import_title(session, ids, context, title="Chicken Curry", asana_gid="1101")
        partial = _import_title(session, ids, context, title="Curry Noodles", asana_gid="1102")
        beyond_page = _import_title(session, ids, context, title="Green Curry", asana_gid="1103")
        completed = _import_title(
            session, ids, context, title="Completed Curry", asana_gid="1104", completed=True
        )
        archived = _import_title(session, ids, context, title="Archived Curry", asana_gid="1105")

        archived_row = session.get(models.DishTask, archived.task_id)
        assert archived_row is not None
        archived_row.existence_state = "retired"
        archived_row.retired_at = NOW

    with session_scope(factory) as session:
        service = _service(session, first_page_size=1)
        exact_result = service.search("Chicken Curry")
        assert [item["task_id"] for item in exact_result["results"]] == [_task_route(exact.task_id)]

        partial_result = service.search("cUrRy")
        assert {item["task_id"] for item in partial_result["results"]} == {
            _task_route(exact.task_id),
            _task_route(partial.task_id),
            _task_route(beyond_page.task_id),
        }
        assert partial_result["truncated"] is False
        assert all(item["section_label"] == "Research Queue" for item in partial_result["results"])

        board = service.bootstrap()
        loaded_ids = {
            card["task_id"]
            for section in board["sections"]
            for card in section["cards"]
        }
        assert _task_route(beyond_page.task_id) not in loaded_ids
        assert _task_route(beyond_page.task_id) in {
            item["task_id"] for item in partial_result["results"]
        }

        assert service.search("does not exist") == {"results": [], "truncated": False}
        assert _task_route(completed.task_id) not in {item["task_id"] for item in partial_result["results"]}
        assert _task_route(archived.task_id) not in {item["task_id"] for item in partial_result["results"]}


def test_active_title_search_follows_inactive_section_eligibility_without_asana(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
        )
        task = _import_title(session, ids, context, title="Section Curry", asana_gid="1110")

    with session_scope(factory) as session:
        service = _service(session)
        assert [item["task_id"] for item in service.search("section curry")["results"]] == [
            _task_route(task.task_id)
        ]

    with session_scope(factory) as session:
        section = session.get(models.Section, context["section_id"])
        assert section is not None
        section.lifecycle = "retired"
        section.retired_at = NOW

    with session_scope(factory) as session:
        # This search path is PostgreSQL-only: no Asana client/credential is constructed.
        assert _service(session).search("section curry") == {"results": [], "truncated": False}

def test_post_burn_projection_history_is_forensic_not_frontend_health(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
        )
        task = _import_title(session, ids, context, title="Historical projection", asana_gid="1050")
        projection = ProjectionService(session, uuid_factory=lambda: _next(ids))
        epoch = projection.activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="frontend historical projection test",
            created_at=NOW - timedelta(hours=2),
            external_effects_enabled=True,
        )
        projection_event_id = _next(ids)
        session.add(
            tx.ProjectionOutboxEvent(
                projection_event_id=projection_event_id,
                generation_id=context["generation_id"],
                projection_epoch_id=epoch.projection_epoch_id,
                source_route="service",
                origin="live",
                command_execution_id=None,
                task_id=task.task_id,
                event_type="update_task_document",
                aggregate_sequence=1,
                idempotency_key="1" * 64,
                intent_payload={"historical": True},
                intent_sha256="2" * 64,
                state="pending",
                claim_owner=None,
                claim_token=None,
                claim_expires_at=None,
                outbox_revision=1,
                created_at=NOW - timedelta(hours=1),
                terminal_at=None,
            )
        )
        session.flush()
        registry = FrontendBoardQuery(session).bootstrap_registry()
        before_burn = FrontendBoardQuery(session).active_cards(
            registry=registry, projection_delay=timedelta(minutes=15), max_cards=10
        )
        assert next(card for card in before_burn if card.task_id == task.task_id).projection_abnormal

        projection.set_external_effects_enabled(
            projection_epoch_id=epoch.projection_epoch_id,
            enabled=False,
            reason="rollback burn",
        )
        after_burn = FrontendBoardQuery(session).active_cards(
            registry=registry, projection_delay=timedelta(minutes=15), max_cards=10
        )
        assert not next(
            card for card in after_burn if card.task_id == task.task_id
        ).projection_abnormal
        assert session.get(tx.ProjectionOutboxEvent, projection_event_id) is not None


def test_import_placeholder_uses_canonical_workflow_role_section_name(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
            section_display_name="Imported section 1216891250619908",
            section_workflow_role="research_queue",
        )

    with session_scope(factory) as session:
        board = _service(session).bootstrap()

    assert board["sections"][0]["section_label"] == "Research Queue"


def test_snapshot_identity_ignores_randomized_cursor_bytes(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
        )
        _import_title(session, ids, context, title="Alpha", asana_gid="1101")
        _import_title(session, ids, context, title="Beta", asana_gid="1102")

    with session_scope(factory) as session:
        service = _service(session, first_page_size=1, continuation_page_size=1)
        first = service.bootstrap()
        second = service.bootstrap()

    assert first["sections"][0]["next_cursor"] != second["sections"][0]["next_cursor"]
    assert first["snapshot_id"] == second["snapshot_id"]


def test_empty_section_is_explicit_and_bootstrap_query_count_is_constant(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
        )

    statements: list[str] = []
    with session_scope(factory) as session:
        engine = session.get_bind()

        def count_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", count_sql)
        try:
            board = _service(session).bootstrap()
        finally:
            event.remove(engine, "before_cursor_execute", count_sql)

    assert len(statements) == 4
    assert len(board["sections"]) == 1
    assert board["sections"][0]["cards"] == []
    assert board["sections"][0]["next_cursor"] is None
    assert board["notices"] == []


def test_section_capacity_is_rejected_before_bootstrap_card_query(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
        )
        second_section_id = _next(ids)
        session.add(
            models.Section(
                section_id=second_section_id,
                logical_name="Second Queue",
                lifecycle="active",
                created_at=NOW,
                retired_at=None,
            )
        )
        session.add(
            models.GovernedSection(
                section_id=second_section_id,
                project_id=context["project_id"],
                logical_name="Second Queue",
                lifecycle="active",
                import_run_id=context["import_run_id"],
                created_at=NOW,
                retired_at=None,
            )
        )
        session.add(
            models.SectionRegistryEntry(
                registry_version_id=context["registry_version_id"],
                section_id=second_section_id,
                ordinal=1,
                display_name="Second Queue",
                workflow_role="second_queue",
            )
        )
        session.add(
            models.SectionCatalogEntry(
                catalog_version_id=context["catalog_version_id"],
                section_id=second_section_id,
                ordinal=1,
                display_name="Second Queue",
                workflow_role="second_queue",
            )
        )

    statements: list[str] = []
    with session_scope(factory) as session:
        engine = session.get_bind()

        def count_sql(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        event.listen(engine, "before_cursor_execute", count_sql)
        try:
            with pytest.raises(BoardCapacityExceeded, match="section capacity"):
                _service(session, max_sections=1).bootstrap()
        finally:
            event.remove(engine, "before_cursor_execute", count_sql)

    assert len(statements) == 3


@pytest.mark.parametrize("later_state", ["active", "released", "recovered"])
def test_historical_expired_actor_lease_is_not_sticky_after_later_attempt(
    workflow_db, later_state: str
) -> None:
    factory, ids, context, task_id = workflow_db
    wall_now = datetime.now(timezone.utc)
    with session_scope(factory) as session:
        service, run_id, execution_id, operation_id = _open_operation(
            session, ids, context, task_id
        )
        old = service.acquire_actor_lease(
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
        old.state = "expired"
        old.terminal_at = wall_now - timedelta(minutes=30)
        old.lease_revision += 1
        later = service.acquire_actor_lease(
            lease_id=_workflow_next(ids),
            execution_id=execution_id,
            operation_id=operation_id,
            run_id=run_id,
            owner_id="owner-1",
            actor_role="constructor",
            actor_attempt_sequence=2,
            issued_at=wall_now,
            expires_at=wall_now + timedelta(hours=1),
        )
        if later_state != "active":
            later.state = later_state
            later.terminal_at = wall_now + timedelta(minutes=1)
            later.lease_revision += 1

    with session_scope(factory) as session:
        card = _service(session).bootstrap()["sections"][0]["cards"][0]
        assert "lease_attention" not in card["attention_codes"]


def test_current_active_actor_lease_past_evaluation_time_has_attention(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    wall_now = datetime.now(timezone.utc)
    with session_scope(factory) as session:
        service, run_id, execution_id, operation_id = _open_operation(
            session, ids, context, task_id
        )
        service.acquire_actor_lease(
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

    with session_scope(factory) as session:
        card = _service(session).bootstrap()["sections"][0]["cards"][0]
        assert "lease_attention" in card["attention_codes"]


def test_current_terminal_expired_actor_lease_without_later_attempt_has_attention(
    workflow_db,
) -> None:
    factory, ids, context, task_id = workflow_db
    wall_now = datetime.now(timezone.utc)
    with session_scope(factory) as session:
        service, run_id, execution_id, operation_id = _open_operation(
            session, ids, context, task_id
        )
        lease = service.acquire_actor_lease(
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
        lease.state = "expired"
        lease.terminal_at = wall_now - timedelta(minutes=30)
        lease.lease_revision += 1

    with session_scope(factory) as session:
        card = _service(session).bootstrap()["sections"][0]["cards"][0]
        assert "lease_attention" in card["attention_codes"]


def test_current_durable_lease_review_and_recovery_facts_map_to_attention(workflow_db) -> None:
    factory, ids, context, task_id = workflow_db
    with session_scope(factory) as session:
        run_id = _workflow_next(ids)
        request_id = _workflow_next(ids)
        execution_id = _workflow_next(ids)
        operation_id = _workflow_next(ids)
        _register_run(session, generation_id=context["generation_id"], run_id=run_id)
        service = WorkflowAuthorityService(session, uuid_factory=lambda: _workflow_next(ids))
        _admit(
            service,
            request_id=request_id,
            generation_id=context["generation_id"],
            run_id=run_id,
        )
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
        service.create_operation(
            operation_id=operation_id,
            execution_id=execution_id,
            task_id=task_id,
            kind="initial",
            phase="prepare_required",
            persisted_actions=["prepare"],
            created_at=NOW,
        )
        service.acquire_actor_lease(
            lease_id=_workflow_next(ids),
            execution_id=execution_id,
            operation_id=operation_id,
            run_id=run_id,
            owner_id="owner-1",
            actor_role="constructor",
            actor_attempt_sequence=1,
            issued_at=NOW,
            expires_at=NOW + timedelta(minutes=1),
        )
        state = session.get(models.DishState, (context["generation_id"], task_id))
        assert state is not None
        service.open_human_review(
            requirement_id=_workflow_next(ids),
            execution_id=execution_id,
            operation_id=operation_id,
            route="human_review",
            question="Review the durable result",
            baseline_content_version_id=state.current_content_version_id,
            opened_at=NOW,
        )
        execution = session.get(wf.CommandExecution, execution_id)
        assert execution is not None
        execution.status = "uncertain"
        execution.terminal_at = NOW + timedelta(minutes=2)
        execution.execution_revision += 1

    with session_scope(factory) as session:
        board = _service(session).bootstrap()
        card = board["sections"][0]["cards"][0]
        assert card["workflow_status"] == {
            "state": "active_operation",
            "operation": "Initial",
            "phase": "Prepare required",
        }
        assert card["attention_codes"] == [
            "lease_attention",
            "verification_attention",
            "recovery_required",
        ]
        assert [notice["code"] for notice in board["notices"]] == card["attention_codes"]
