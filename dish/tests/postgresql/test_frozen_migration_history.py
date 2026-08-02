from __future__ import annotations

import ast
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from dish_pg.migrations.frozen_tables import (
    FROZEN_CREATE_SQL,
    FROZEN_TABLE_NAMES,
    frozen_revision_digest,
)
from dish_pg.release import ALEMBIC_HEAD

pytestmark = pytest.mark.smoke

ROOT = Path(__file__).resolve().parents[2]
VERSIONS = ROOT / "dish_pg" / "migrations" / "versions"
FROZEN_REVISIONS = (
    "0003_workflow_authority",
    "0004_transition_projection",
    "0005_release_cutover",
    "0006_final_asana_closure",
    "0007_cutover_evidence_gates",
)
EXPECTED_DIGESTS = {
    "0003_workflow_authority": "203ffd428dc53eb90f857437a7d6ae8773ab7f6b388de766cca905b4e8a8ad20",
    "0004_transition_projection": "a5360764d453cbc853796ba12c3fbf6fc0f099be18be3776b9f084ee180cd74c",
    "0005_release_cutover": "3b0b3d65e7a13823bcff9f8042b01046a57dc29cdc46cb561758f0827b694f56",
    "0006_final_asana_closure": "07a301bba48cd8ca47595d669634d004b8367a092647f49c1b0f3d762d802e60",
    "0007_cutover_evidence_gates": "8714a33aa5bc5ef8fa886b995bad6af5c1314074a474fa0eda57a4232228d7f7",
}


def _config(path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite+pysqlite:///{path}")
    return config


def _dish_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module)
        elif isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
    return {name for name in imports if name.startswith("dish_pg")}


def test_frozen_revision_definitions_have_stable_digests() -> None:
    assert tuple(FROZEN_TABLE_NAMES) == FROZEN_REVISIONS
    assert set(FROZEN_CREATE_SQL) == set(FROZEN_REVISIONS)
    assert {
        revision: frozen_revision_digest(revision) for revision in FROZEN_REVISIONS
    } == EXPECTED_DIGESTS


def test_historical_revisions_do_not_import_live_orm_authority() -> None:
    allowed = {"dish_pg.migrations.frozen_tables"}
    for revision in FROZEN_REVISIONS:
        path = VERSIONS / f"{revision}.py"
        assert _dish_imports(path) == allowed
        source = path.read_text()
        assert "Base.metadata" not in source
        assert "STAGE3_TABLE_NAMES" not in source
        assert "STAGE5_TABLE_NAMES" not in source
        assert "STAGE6_TABLE_NAMES" not in source
        assert "STAGE7_TABLE_NAMES" not in source
        assert "STAGE8_TABLE_NAMES" not in source

    frozen_source = (ROOT / "dish_pg" / "migrations" / "frozen_tables.py").read_text()
    assert "dish_pg.models" not in frozen_source
    assert "stage3_models" not in frozen_source
    assert "stage5_models" not in frozen_source
    assert "stage6_models" not in frozen_source


@pytest.mark.database_boundary
def test_empty_sqlite_upgrade_uses_frozen_history_through_head(tmp_path: Path) -> None:
    path = tmp_path / "frozen-history.sqlite3"
    command.upgrade(_config(path), "head")
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        actual = set(inspect(engine).get_table_names())
        expected = {
            table
            for revision in FROZEN_REVISIONS
            for table in FROZEN_TABLE_NAMES[revision]
        }
        assert expected.issubset(actual)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == ALEMBIC_HEAD
    finally:
        engine.dispose()


@pytest.mark.database_boundary
def test_frozen_history_downgrades_to_stage2_and_reupgrades(tmp_path: Path) -> None:
    path = tmp_path / "frozen-history-roundtrip.sqlite3"
    config = _config(path)
    command.upgrade(config, "head")
    command.downgrade(config, "0002_core_authority_model")

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        remaining = set(inspect(engine).get_table_names())
        removed = {
            table
            for revision in FROZEN_REVISIONS
            for table in FROZEN_TABLE_NAMES[revision]
        }
        assert remaining.isdisjoint(removed)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0002_core_authority_model"
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == ALEMBIC_HEAD
    finally:
        engine.dispose()
