"""Populated migration evidence on PGlite; never native certification."""
from __future__ import annotations

import pytest

from dish_pg.release import ALEMBIC_HEAD
from tests.support.postgresql.projection_attempt_migration import (
    PREDECESSOR_REVISION,
    TARGET_REVISION,
    assert_projection_attempt_backfill,
    assert_projection_attempt_constraints_present,
    seed_valid_projection_attempt_predecessor,
)

pytestmark = pytest.mark.pglite


def test_pglite_upgrades_populated_projection_attempt_predecessor(
    pglite_migration_database,
) -> None:
    database = pglite_migration_database
    database.initialize(PREDECESSOR_REVISION)
    seed = seed_valid_projection_attempt_predecessor(database)
    database.upgrade(ALEMBIC_HEAD)
    database.assert_revision(ALEMBIC_HEAD)
    assert_projection_attempt_backfill(database, seed)
    assert_projection_attempt_constraints_present(database)
