from __future__ import annotations

import ast
import io
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
    "0003_workflow_authority": "a07b3013a001970c3c2e81fcfd252ec5bb984274812b476a5063d2724755fe1b",
    "0004_transition_projection": "a5360764d453cbc853796ba12c3fbf6fc0f099be18be3776b9f084ee180cd74c",
    "0005_release_cutover": "3b0b3d65e7a13823bcff9f8042b01046a57dc29cdc46cb561758f0827b694f56",
    "0006_final_asana_closure": "07a301bba48cd8ca47595d669634d004b8367a092647f49c1b0f3d762d802e60",
    "0007_cutover_evidence_gates": "8714a33aa5bc5ef8fa886b995bad6af5c1314074a474fa0eda57a4232228d7f7",
}


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
        } - {"causality_edges"}
        assert expected.issubset(actual)
        assert "causality_edges" not in actual
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == ALEMBIC_HEAD
            insert_guard = connection.execute(
                text(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND name='dish_states_validate_insert'"
                )
            ).scalar_one()
            assert "OR r.archive_changed" in insert_guard
    finally:
        engine.dispose()


def test_causality_edge_retirement_offline_sql_guards_before_drop() -> None:
    buffer = io.StringIO()
    command.upgrade(
        _offline_postgresql_config(buffer),
        "0038_cutover_rehearsal_identity:0039_remove_unused_causality_edges",
        sql=True,
    )
    sql = buffer.getvalue()

    guard_position = sql.index("DO $$")
    assert "EXISTS (SELECT 1 FROM causality_edges)" in sql
    assert "refusing to drop non-empty causality_edges" in sql
    drop_position = sql.index("DROP TABLE causality_edges")
    assert guard_position < drop_position


def test_independent_archive_offline_postgresql_sql_guards_populated_state() -> None:
    buffer = io.StringIO()
    command.upgrade(
        _offline_postgresql_config(buffer),
        "0043_archived_at:0044_independent_archive",
        sql=True,
    )
    sql = buffer.getvalue()

    guard_position = sql.index("0044_independent_archive upgrade refuses populated archived rows")
    assert "EXISTS (SELECT 1 FROM dish_states WHERE archived_at IS NOT NULL)" in sql
    assert guard_position < sql.index("ADD COLUMN archive_changed")


def test_native_section_authority_offline_postgresql_sql_renders_schema_transition() -> None:
    buffer = io.StringIO()
    command.upgrade(
        _offline_postgresql_config(buffer),
        "0044_independent_archive:0045_native_section_authority",
        sql=True,
    )
    sql = buffer.getvalue()

    assert "CREATE TABLE sections" in sql
    assert "CREATE TABLE native_catalog_runtime_attestations" in sql
    assert "UPDATE dish_states SET catalog_version_id=registry_version_id" in sql
    assert "0045_native_section_authority" in sql


@pytest.mark.database_boundary
def test_causality_edge_retirement_refuses_unexpected_forensic_evidence(
    tmp_path: Path,
) -> None:
    path = tmp_path / "causality-edge-forensic-evidence.sqlite3"
    config = _config(path)
    command.upgrade(config, "0038_cutover_rehearsal_identity")
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    edge_id = "1" * 32
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """INSERT INTO causality_edges(
                           causality_edge_id,generation_id,request_id,command_execution_id,
                           cause_type,cause_id,effect_type,effect_id,recorded_at
                       ) VALUES (
                           :edge_id,:generation_id,:request_id,NULL,
                           'request','unexpected','effect','unexpected',:recorded_at
                       )"""
                ),
                {
                    "edge_id": edge_id,
                    "generation_id": "2" * 32,
                    "request_id": "3" * 32,
                    "recorded_at": "2026-08-12 20:00:00",
                },
            )

        with pytest.raises(
            RuntimeError,
            match="refusing to drop non-empty causality_edges",
        ):
            command.upgrade(config, "head")

        with engine.connect() as connection:
            assert inspect(connection).has_table("causality_edges")
            assert connection.execute(
                text("SELECT causality_edge_id FROM causality_edges")
            ).scalar_one() == edge_id
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == "0038_cutover_rehearsal_identity"
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


def _insert_archived_state(path: Path, *, completed: bool) -> None:
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TRIGGER dish_states_validate_insert")
            connection.execute(
                text(
                    """INSERT INTO dish_states(
                           generation_id,task_id,current_content_version_id,section_id,
                           registry_version_id,completed,completion_reason,dish_version,
                           placement_version,completion_version,updated_at,archived_at
                       ) VALUES (
                           :generation_id,:task_id,:content_id,NULL,:registry_id,
                           :completed,'archive',1,1,1,:at,:at
                       )"""
                ),
                {
                    "generation_id": "1" * 32,
                    "task_id": "2" * 32,
                    "content_id": "3" * 32,
                    "registry_id": "4" * 32,
                    "completed": completed,
                    "at": "2026-08-27 12:00:00",
                },
            )
    finally:
        engine.dispose()


@pytest.mark.database_boundary
def test_independent_archive_upgrade_refuses_coupled_0043_rows(tmp_path: Path) -> None:
    path = tmp_path / "coupled-archive.sqlite3"
    config = _config(path)
    command.upgrade(config, "0043_archived_at")
    _insert_archived_state(path, completed=True)

    with pytest.raises(RuntimeError, match="upgrade refuses 1 populated archived row"):
        command.upgrade(config, "head")

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0043_archived_at"
            assert "archive_changed" not in {
                column["name"] for column in inspect(connection).get_columns("dish_mutation_receipts")
            }
    finally:
        engine.dispose()


@pytest.mark.database_boundary
def test_independent_archive_downgrade_refuses_populated_0044_rows(tmp_path: Path) -> None:
    path = tmp_path / "independent-archive.sqlite3"
    config = _config(path)
    command.upgrade(config, "0044_independent_archive")
    _insert_archived_state(path, completed=False)

    with pytest.raises(RuntimeError, match="downgrade refuses 1 populated archived row"):
        command.downgrade(config, "0043_archived_at")

    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0044_independent_archive"
            assert "archive_changed" in {
                column["name"] for column in inspect(connection).get_columns("dish_mutation_receipts")
            }
    finally:
        engine.dispose()


@pytest.mark.database_boundary
def test_exact_revocation_migration_upgrades_populated_workflow_operations(tmp_path: Path) -> None:
    path = tmp_path / "exact-revocation-populated.sqlite3"
    config = _config(path)
    command.upgrade(config, "0035_persistence_constraint_integrity")
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    operation_id = "1" * 32
    with engine.begin() as connection:
        connection.execute(
            text(
                """INSERT INTO workflow_operations(
                       operation_id,generation_id,task_id,kind,lifecycle,phase,persisted_actions,
                       import_run_id,creation_request_id,creation_execution_id,contract_binding_id,
                       predecessor_operation_id,terminal_outcome,operation_revision,created_at,terminal_at
                   ) VALUES (
                       :operation_id,:generation_id,:task_id,'initial','completed','terminal','[]',
                       NULL,:request_id,:execution_id,:binding_id,NULL,'completed',1,:created_at,:terminal_at
                   )"""
            ),
            {
                "operation_id": operation_id,
                "generation_id": "2" * 32,
                "task_id": "3" * 32,
                "request_id": "4" * 32,
                "execution_id": "5" * 32,
                "binding_id": "6" * 32,
                "created_at": "2026-08-01 20:00:00",
                "terminal_at": "2026-08-01 20:01:00",
            },
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT operation_id FROM workflow_operations")
            ).scalar_one() == operation_id
            assert "operation_run_revocations" in inspect(engine).get_table_names()
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == ALEMBIC_HEAD
    finally:
        engine.dispose()
