"""dish_pg.migration_status: the fail-closed ExecStartPre schema-head gate.

Exercises the check against a real PostgreSQL target, both matching and
stale, so the gate that's wired into dish-shadow-worker.service's
ExecStartPre is proven to actually block on drift rather than silently pass.

Staleness is simulated by writing an earlier value directly into
alembic_version rather than running a real downgrade: 0035's own downgrade()
deliberately refuses to run (it would reopen known CHECK NULL holes), and
this check only ever reads the version marker, never actual columns, so a
raw marker rewrite is a faithful and much cheaper way to exercise it.
"""
from __future__ import annotations

import pytest
from sqlalchemy import create_engine, text

from dish_pg import migration_status
from dish_pg.migration_status import MigrationStatusError, check_migration_head, main
from tests.support.postgresql.certification import postgresql_dsn
from tests.support.postgresql.core import core_db

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def _rewrite_version_marker(dsn: str, version: str) -> None:
    engine = create_engine(dsn, future=True)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :version"), {"version": version}
            )
    finally:
        engine.dispose()


def test_check_migration_head_passes_when_database_is_at_alembic_head(core_db) -> None:
    dsn = postgresql_dsn()
    head = check_migration_head(dsn)
    assert head == migration_status.ALEMBIC_HEAD


def test_check_migration_head_raises_when_database_is_stale(core_db) -> None:
    dsn = postgresql_dsn()
    _rewrite_version_marker(dsn, "0034_cc5_schema_repair")
    with pytest.raises(MigrationStatusError, match="run `alembic upgrade head`"):
        check_migration_head(dsn)


def test_main_exits_nonzero_and_prints_actionable_message_on_drift(core_db, capsys) -> None:
    dsn = postgresql_dsn()
    _rewrite_version_marker(dsn, "0034_cc5_schema_repair")
    exit_code = main(["--database-url", dsn])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert migration_status.ALEMBIC_HEAD in captured.err
    assert "alembic upgrade head" in captured.err


def test_main_exits_zero_when_up_to_date(core_db) -> None:
    dsn = postgresql_dsn()
    exit_code = main(["--database-url", dsn])
    assert exit_code == 0
