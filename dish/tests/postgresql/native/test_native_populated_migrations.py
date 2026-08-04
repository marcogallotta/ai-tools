"""Native PostgreSQL populated-predecessor certification checks."""
from __future__ import annotations

import pytest

from dish_pg.release import ALEMBIC_HEAD
from tests.support.postgresql.projection_attempt_migration import (
    PREDECESSOR_REVISION,
    TARGET_REVISION,
    assert_projection_attempt_backfill,
    assert_projection_attempt_constraints,
    seed_valid_projection_attempt_predecessor,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def test_native_postgresql_upgrades_populated_projection_attempt_predecessor(
    native_migration_database,
) -> None:
    database = native_migration_database
    database.initialize(PREDECESSOR_REVISION)
    seed = seed_valid_projection_attempt_predecessor(database)
    database.upgrade(TARGET_REVISION)
    database.assert_revision(ALEMBIC_HEAD)
    assert_projection_attempt_backfill(database, seed)
    assert_projection_attempt_constraints(database, seed)
