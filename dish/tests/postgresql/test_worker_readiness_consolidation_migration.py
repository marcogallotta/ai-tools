from __future__ import annotations

import io
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from dish_pg import candidate_manifest_models as manifest_models
from dish_pg import stage6_models as release_models
from dish_pg.release import ALEMBIC_HEAD


ROOT = Path(__file__).resolve().parents[2]
PREDECESSOR_REVISION = "0033_frontend_security"
TARGET_REVISION = "0034_cc5_schema_repair"


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    return config


def _offline_postgresql_config(buffer: io.StringIO) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option(
        "sqlalchemy.url",
        "postgresql+psycopg://offline:offline@localhost/offline",
    )
    config.attributes["output_buffer"] = buffer
    return config


def test_offline_cc5_sql_guards_legacy_readiness_before_destructive_ddl() -> None:
    buffer = io.StringIO()
    command.upgrade(
        _offline_postgresql_config(buffer),
        f"{PREDECESSOR_REVISION}:{TARGET_REVISION}",
        sql=True,
    )
    sql = buffer.getvalue()

    guard = "DO $$"
    guard_position = sql.index(guard)
    for table in (
        "projection_worker_readiness",
        "worker_probe_inventories",
        "worker_probe_requirements",
        "worker_probe_evidence",
        "worker_readiness_completions",
    ):
        assert f"EXISTS (SELECT 1 FROM {table})" in sql
    destructive_positions = [
        sql.index(marker)
        for marker in (
            "DROP NOT NULL",
            "DROP CONSTRAINT",
            "DROP FUNCTION",
            "DROP TABLE",
            "DROP INDEX",
        )
        if marker in sql
    ]
    assert destructive_positions
    assert guard_position < min(destructive_positions)


@pytest.mark.database_boundary
def test_online_cc5_upgrade_accepts_empty_legacy_readiness(tmp_path: Path) -> None:
    path = tmp_path / "cc5-empty-predecessor.sqlite3"
    config = _config(path)
    command.upgrade(config, PREDECESSOR_REVISION)
    command.upgrade(config, TARGET_REVISION)

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one() == TARGET_REVISION
        tables = set(inspect(engine).get_table_names())
        assert "worker_probe_inventories" not in tables
        assert "projection_worker_readiness" in tables
    finally:
        engine.dispose()


@pytest.mark.database_boundary
def test_online_cc5_upgrade_refuses_populated_legacy_readiness(tmp_path: Path) -> None:
    path = tmp_path / "cc5-populated-predecessor.sqlite3"
    config = _config(path)
    command.upgrade(config, PREDECESSOR_REVISION)
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO worker_probe_requirements "
                "(requirement_id, inventory_id, probe_kind, ordinal, probe_contract_version) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    "00000000000000000000000000000001",
                    "00000000000000000000000000000002",
                    "claim",
                    0,
                    "projection-worker-probe-v1",
                ),
            )

        with pytest.raises(RuntimeError, match="worker_probe_requirements=1"):
            command.upgrade(config, TARGET_REVISION)

        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one() == PREDECESSOR_REVISION
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM worker_probe_requirements"
            ).scalar_one() == 1
        assert "worker_probe_requirements" in inspect(engine).get_table_names()
    finally:
        engine.dispose()


@pytest.mark.database_boundary
def test_upgrade_through_head_installs_cc5_manifest_and_readiness_schema(tmp_path: Path) -> None:
    path = tmp_path / "cc5-schema-repair.sqlite3"
    command.upgrade(_config(path), "head")
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        inspector = inspect(engine)
        assert inspector.get_table_names()
        manifest_columns = {
            column["name"]
            for column in inspector.get_columns("release_candidate_manifests")
        }
        assert "approval_reconciliation_run_id" in manifest_columns

        readiness_columns = {
            column["name"]
            for column in inspector.get_columns("projection_worker_readiness")
        }
        assert {
            "deployed_artifact_sha256",
            "report_contract_version",
            "claim_probe_result",
            "exact_write_probe_result",
            "restart_probe_result",
            "completed_at",
            "report_sha256",
        }.issubset(readiness_columns)
        assert {"payload", "readiness_sha256", "ready_at", "probe_inventory_id"}.isdisjoint(
            readiness_columns
        )

        tables = set(inspector.get_table_names())
        assert {
            "worker_probe_inventories",
            "worker_probe_requirements",
            "worker_probe_evidence",
            "worker_readiness_completions",
        }.isdisjoint(tables)

        with engine.connect() as connection:
            assert connection.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).scalar_one() == ALEMBIC_HEAD
            # This exact ORM select previously raised UndefinedColumn after upgrade-to-head.
            connection.execute(select(manifest_models.ReleaseCandidateManifest)).all()
            connection.execute(select(release_models.ProjectionWorkerReadiness)).all()
    finally:
        engine.dispose()
