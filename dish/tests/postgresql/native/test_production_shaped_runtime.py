"""Native PostgreSQL certification for the Section 4 TEST service runtime."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

from sqlalchemy.engine import make_url

import pytest

from dish_pg.database import session_scope
from dish_pg.production_shaped_rehearsal import (
    _register_runtime_run,
    _safe_child_env,
    _service_database_disconnect_scenario,
    _service_process_loss_scenario,
)
from dish_pg.production_shaped_runtime import ServiceRuntimeClient
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.transition import ProjectionService
from tests.support.postgresql.certification import postgresql_dsn
from tests.support.postgresql.core import core_db
from tests.support.postgresql.process_failure import compose_control
from tests.support.postgresql.projection_attempts import native_workflow_db

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]

ROOT = Path(__file__).resolve().parents[3]
RUNTIME_ENTRY_POINT = ROOT / "dish-service"


def _runtime(core_db, tmp_path: Path, *, corpus_sha256: str):
    factory, _ids, context, _task_id = native_workflow_db(core_db)
    with session_scope(factory) as session:
        ProjectionService(session).activate_epoch(
            generation_id=context["generation_id"],
            activation_reason="Section 4 native runtime certification",
            created_at=datetime.now(timezone.utc),
            external_effects_enabled=True,
        )
    engine = factory.kw["bind"]
    owner_id, run_id = _register_runtime_run(
        engine,
        generation_id=context["generation_id"],
        corpus_sha256=corpus_sha256,
    )
    client = ServiceRuntimeClient(
        entry_point=RUNTIME_ENTRY_POINT,
        database_url=postgresql_dsn(),
        expected_database=str(make_url(postgresql_dsn()).database),
        expected_schema_head=ALEMBIC_HEAD,
        expected_release="dish-42619b9",
        generation_id=str(context["generation_id"]),
        owner_id=owner_id,
        run_id=run_id,
        evidence_dir=tmp_path,
        cwd=ROOT,
        env=_safe_child_env(),
        log_path=tmp_path / "service.log",
        python_executable=sys.executable,
    )
    client.start()
    return client, engine


def test_section4_service_process_loss_replays_without_duplicate_effects(
    core_db, tmp_path
) -> None:
    runtime, engine = _runtime(core_db, tmp_path, corpus_sha256="a" * 64)
    try:
        result = _service_process_loss_scenario(
            runtime=runtime,
            engine=engine,
            evidence_dir=tmp_path,
        )
    finally:
        runtime.stop()
    assert result["status"] == "passed"
    assert result["after_replay"]["request_count"] == 1
    assert result["after_replay"]["execution_count"] == 1
    assert result["after_replay"]["task_effect_count"] == 1
    assert result["after_replay"]["projection_effect_count"] == 1


def test_section4_service_database_disconnect_rolls_back_then_recovers_once(
    core_db, tmp_path
) -> None:
    runtime, engine = _runtime(core_db, tmp_path, corpus_sha256="b" * 64)

    class ComposeCluster:
        def stop(self, *, mode: str = "fast") -> None:
            assert mode == "fast"
            compose_control("stop")

        def start(self) -> None:
            compose_control("start")

    try:
        result = _service_database_disconnect_scenario(
            runtime=runtime,
            engine=engine,
            primary=ComposeCluster(),
            evidence_dir=tmp_path,
        )
    finally:
        try:
            compose_control("start")
        finally:
            engine.dispose()
            runtime.stop()
    assert result["status"] == "passed"
    assert result["before_replay"]["request_count"] == 0
    assert result["after_replay"]["request_count"] == 1
    assert result["after_replay"]["execution_count"] == 1
    assert result["after_replay"]["task_effect_count"] == 1
    assert result["after_replay"]["projection_effect_count"] == 1
