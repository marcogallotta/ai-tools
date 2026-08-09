from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select

from dish_pg import candidate_manifest_models as manifest_models
from dish_pg import stage6_models as release_models
from dish_pg.release import ALEMBIC_HEAD


ROOT = Path(__file__).resolve().parents[2]


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    return config


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
