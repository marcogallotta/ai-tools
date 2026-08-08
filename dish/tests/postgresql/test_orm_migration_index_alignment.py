"""Indexes declared by migrations must remain present in ORM metadata."""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from dish_pg import models

ROOT = Path(__file__).resolve().parents[2]
EXPECTED = {
    "legacy_request_tombstones": {
        "ix_legacy_request_tombstones_import_run": "0023_legacy_request_tombstones.py",
    },
    "projection_reconciliation_runs": {
        "ix_reconciliation_candidate_boundary": "0025_reconciliation_observation_boundary.py",
    },
}


def test_migration_defined_indexes_are_declared_in_orm_metadata() -> None:
    versions = ROOT / "dish_pg" / "migrations" / "versions"
    for table_name, indexes in EXPECTED.items():
        metadata_names = {index.name for index in models.Base.metadata.tables[table_name].indexes}
        assert set(indexes) <= metadata_names
        for index_name, migration_file in indexes.items():
            migration_source = (versions / migration_file).read_text(encoding="utf-8")
            assert index_name in migration_source

def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    return config


@pytest.mark.database_boundary
def test_migrated_schema_contains_the_declared_indexes(tmp_path: Path) -> None:
    database = tmp_path / "orm-index-alignment.sqlite3"
    command.upgrade(_config(database), "head")
    engine = create_engine(f"sqlite+pysqlite:///{database}", future=True)
    try:
        inspector = inspect(engine)
        for table_name, indexes in EXPECTED.items():
            migrated_names = {row["name"] for row in inspector.get_indexes(table_name)}
            assert set(indexes) <= migrated_names
    finally:
        engine.dispose()

