from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from dish_pg.database import session_scope
from dish_pg.workflow import WorkflowAuthorityService
from tests.support.postgresql.core import _bootstrap_registry, _import_one, core_db
from tests.support.postgresql.workflow import NOW, _admit, _execution, _next, _register_run

ROOT = Path(__file__).resolve().parents[2]


def test_duplicate_task_level_authorization_grant_is_rejected(core_db) -> None:
    factory, ids = core_db
    with session_scope(factory) as session:
        context = _bootstrap_registry(session, ids, generation_status="active")
        task = _import_one(session, ids, context)
        run_id = _next(ids)
        _register_run(
            session,
            generation_id=context["generation_id"],
            run_id=run_id,
            owner="marco",
            agent="marco",
        )
        request_id = _next(ids)
        execution_id = _next(ids)
        service = WorkflowAuthorityService(session)
        _admit(
            service,
            request_id=request_id,
            generation_id=context["generation_id"],
            run_id=run_id,
            command="authorize-governed-change",
            owner="marco",
            principal="admin",
        )
        _execution(
            service,
            execution_id=execution_id,
            request_id=request_id,
            generation_id=context["generation_id"],
            task_id=task.task_id,
            binding_id=context["binding_id"],
            command="authorize-governed-change",
        )
        grant = dict(
            execution_id=execution_id,
            task_id=task.task_id,
            operation_id=None,
            field_name="title",
            before_value="old title",
            after_value="new title",
            reason="approved exact governed change",
            actor="Marco",
            run_id=run_id,
            granted_at=NOW,
        )
        service.grant_marco_authorization(grant_id=_next(ids), **grant)
        with pytest.raises(IntegrityError):
            service.grant_marco_authorization(grant_id=_next(ids), **grant)


@pytest.mark.database_boundary
def test_task_level_grant_partial_index_migrates_and_downgrades(tmp_path: Path) -> None:
    path = tmp_path / "task-grant-identity.sqlite3"
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    command.upgrade(config, "head")

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        indexes = {row["name"] for row in inspect(engine).get_indexes("marco_authorization_grants")}
        assert "uq_marco_grant_task_semantic_identity" in indexes
    finally:
        engine.dispose()

    command.downgrade(config, "0011_rollback_bundle_identity")
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        indexes = {row["name"] for row in inspect(engine).get_indexes("marco_authorization_grants")}
        assert "uq_marco_grant_task_semantic_identity" not in indexes
    finally:
        engine.dispose()
