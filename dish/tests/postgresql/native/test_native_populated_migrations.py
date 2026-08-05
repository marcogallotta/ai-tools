"""Native PostgreSQL populated-predecessor certification checks."""
from __future__ import annotations

import pytest

from dish_pg.release import ALEMBIC_HEAD
from tests.support.postgresql.cutover_authority_migration import (
    PREDECESSOR_REVISION as CUTOVER_PREDECESSOR_REVISION,
    TARGET_REVISION as CUTOVER_TARGET_REVISION,
    seed_candidate_dependency_predecessor,
    seed_unsafe_open_admission_predecessor,
)
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



def test_native_postgresql_upgrades_matching_cutover_candidate_lineage(
    native_migration_database,
) -> None:
    database = native_migration_database
    database.initialize(CUTOVER_PREDECESSOR_REVISION)
    seed_candidate_dependency_predecessor(database, mismatched=False)
    database.upgrade(CUTOVER_TARGET_REVISION)
    database.assert_revision(ALEMBIC_HEAD)


def test_native_postgresql_rejects_mismatched_cutover_candidate_lineage(
    native_migration_database,
) -> None:
    database = native_migration_database
    database.initialize(CUTOVER_PREDECESSOR_REVISION)
    seed_candidate_dependency_predecessor(database, mismatched=True)
    database.expect_upgrade_failure(
        CUTOVER_TARGET_REVISION,
        expected_exception=RuntimeError,
        message_fragment="dependency generations conflict with populated lineage",
    )
    database.assert_revision(CUTOVER_PREDECESSOR_REVISION)

def test_native_postgresql_rejects_unverified_open_admission_predecessor(
    native_migration_database,
) -> None:
    database = native_migration_database
    database.initialize(CUTOVER_PREDECESSOR_REVISION)
    seed = seed_candidate_dependency_predecessor(database, mismatched=False)
    seed_unsafe_open_admission_predecessor(database, seed=seed)
    database.expect_upgrade_failure(
        CUTOVER_TARGET_REVISION,
        expected_exception=RuntimeError,
        message_fragment="open mutation admission lacks verified first-admission authority",
    )
    database.assert_revision(CUTOVER_PREDECESSOR_REVISION)
