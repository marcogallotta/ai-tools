from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from dish_pg import postgres_service as postgres_service_module
from dish_pg.database import session_scope
from dish_pg.frontend_board_query import FrontendBoardQuery
from dish_service.leases import ServicePrincipal
from dish_service.frontend_board import FrontendBoardConfig
from dish_service.frontend_local import PostgresLocalBoardBackend
from tests.support.postgresql.core import _bootstrap_registry, _import_one, _next, core_db
from tests.support.postgresql.runtime_validation import runtime_service

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def test_local_frontend_backend_reads_native_postgresql_with_canonical_dish_uuid(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
        )
        task_id = _next(ids)
        _import_one(session, ids, context, task_id=task_id)

    backend = PostgresLocalBoardBackend(
        factory,
        token_secret=b"native-stage3-local-board-test-secret",
        config=FrontendBoardConfig(projection_delay=timedelta(minutes=15)),
    )
    board = backend.bootstrap()

    assert board["sections"][0]["cards"][0]["title"] == "[ready] Exact imported task"
    assert board["sections"][0]["section_id"].startswith("r1s-")
    assert board["sections"][0]["cards"][0]["task_id"] == str(task_id)


def test_local_frontend_backend_reads_native_postgresql_task_detail_by_canonical_dish_uuid(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
        )
        task_id = _next(ids)
        _import_one(session, ids, context, task_id=task_id)

    backend = PostgresLocalBoardBackend(
        factory,
        token_secret=b"native-stage4-local-detail-test-secret",
        config=FrontendBoardConfig(projection_delay=timedelta(minutes=15)),
    )
    board = backend.bootstrap()
    task_route_id = board["sections"][0]["cards"][0]["task_id"]
    detail = backend.detail(task_route_id=task_route_id)

    assert detail["task_id"] == task_route_id
    assert detail["title"] == "[ready] Exact imported task"
    assert detail["body_presentation"]["state"] == "sanitized_html"
    assert detail["advisory"]["invokable_by_frontend"] is False
    assert detail["task_id"] == str(task_id)


def test_search_refuses_success_when_registry_identity_rolls_between_read_and_response_fence(
    core_db, tmp_path, monkeypatch
) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(
            session,
            ids,
            generation_status="active",
            schema_head="0032_imported_operation_history",
        )
        task_id = _next(ids)
        _import_one(session, ids, context, task_id=task_id)

    class RolloverBoardQuery(FrontendBoardQuery):
        def __init__(self, session):
            super().__init__(session)
            self._context_reads = 0

        def context(self):
            current = super().context()
            self._context_reads += 1
            if self._context_reads == 1:
                return current
            return replace(current, registry_revision=current.registry_revision + 1)

    monkeypatch.setattr(
        postgres_service_module,
        "FrontendBoardQuery",
        RolloverBoardQuery,
    )
    service = runtime_service(factory, tmp_path)
    principal = ServicePrincipal.from_values("cli", str(_next(ids)))
    result = service.execute_agent(
        "search",
        {"query": "imported", "agent": "gpt", "page_size": 1},
        principal=principal,
        request_id=None,
    )

    assert result["ok"] is False
    assert result["code"] == "BACKEND_REJECTED"
    assert result["retryable"] is True
    assert "results" not in result["data"]
    assert "next_cursor" not in result["data"]
    assert result["data"]["captured_generation_id"] == str(context["generation_id"])
    assert result["data"]["captured_registry_version_id"] == str(
        context["registry_version_id"]
    )
