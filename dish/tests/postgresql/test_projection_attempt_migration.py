from __future__ import annotations

import pytest

from dish_pg.release import ALEMBIC_HEAD
from tests.support.postgresql.projection_attempt_migration import (
    PREDECESSOR_REVISION,
    assert_projection_attempt_backfill,
    assert_projection_attempt_constraints,
    seed_valid_projection_attempt_predecessor,
)

pytestmark = pytest.mark.database_boundary


def test_projection_attempt_migration_backfills_history_and_enforces_new_constraints(
    sqlite_migration_database,
) -> None:
    sqlite_migration_database.initialize(PREDECESSOR_REVISION)
    seed = seed_valid_projection_attempt_predecessor(sqlite_migration_database)
    sqlite_migration_database.upgrade(ALEMBIC_HEAD)
    sqlite_migration_database.assert_revision(ALEMBIC_HEAD)
    assert_projection_attempt_backfill(sqlite_migration_database, seed)
    assert_projection_attempt_constraints(sqlite_migration_database, seed)



def test_fresh_sqlite_bootstrap_reaches_final_head(sqlite_migration_database) -> None:
    sqlite_migration_database.fresh_bootstrap()
    sqlite_migration_database.assert_revision(ALEMBIC_HEAD)
