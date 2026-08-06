from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

from dish_pg.release import ALEMBIC_HEAD

pytestmark = pytest.mark.database_boundary

ROOT = Path(__file__).resolve().parents[2]


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    return config


def _guard_sql(path: Path) -> str:
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        with engine.connect() as connection:
            return str(
                connection.execute(
                    text(
                        """
                        SELECT sql
                          FROM sqlite_master
                         WHERE type = 'trigger'
                           AND name = 'service_requests_stage6_admission_guard'
                        """
                    )
                ).scalar_one()
            )
    finally:
        engine.dispose()


def test_validation_failure_admission_upgrade_updates_existing_guard(
    tmp_path: Path,
) -> None:
    path = tmp_path / "validation-admission.sqlite3"
    config = _config(path)

    command.upgrade(config, "0029_cutover_authority_admission_fixes")
    assert "pre_execution_validation_failure" not in _guard_sql(path)

    command.upgrade(config, ALEMBIC_HEAD)
    assert "pre_execution_validation_failure" in _guard_sql(path)

    command.downgrade(config, "0029_cutover_authority_admission_fixes")
    assert "pre_execution_validation_failure" not in _guard_sql(path)
