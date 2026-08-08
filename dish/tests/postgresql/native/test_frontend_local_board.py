from __future__ import annotations

from datetime import timedelta

import pytest

from dish_pg.database import session_scope
from dish_service.frontend_board import FrontendBoardConfig
from dish_service.frontend_local import PostgresLocalBoardBackend
from tests.support.postgresql.core import _bootstrap_registry, _import_one, _next, core_db

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def test_local_frontend_backend_reads_native_postgresql_without_raw_ids(core_db) -> None:
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
    assert board["sections"][0]["cards"][0]["task_id"].startswith("r1t-")
    assert str(task_id) not in repr(board)
