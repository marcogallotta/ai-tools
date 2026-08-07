"""SQLite compatibility coverage for populated cutover-authority upgrades."""
from __future__ import annotations

from tests.support.postgresql.cutover_authority_migration import (
    PREDECESSOR_REVISION,
    TARGET_REVISION,
    seed_candidate_dependency_predecessor,
    seed_unsafe_open_admission_predecessor,
)


def test_populated_cutover_authority_upgrade_accepts_matching_generation_lineage(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    database.initialize(PREDECESSOR_REVISION)
    seed_candidate_dependency_predecessor(database, mismatched=False)
    database.upgrade(TARGET_REVISION)
    database.assert_revision(TARGET_REVISION)


def test_populated_cutover_authority_upgrade_rejects_mismatched_generation_lineage(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    database.initialize(PREDECESSOR_REVISION)
    seed_candidate_dependency_predecessor(database, mismatched=True)
    database.expect_upgrade_failure(
        TARGET_REVISION,
        expected_exception=RuntimeError,
        message_fragment="dependency generations conflict with populated lineage",
    )
    database.assert_revision(PREDECESSOR_REVISION)

def test_populated_cutover_authority_upgrade_rejects_unverified_open_admission(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    database.initialize(PREDECESSOR_REVISION)
    seed = seed_candidate_dependency_predecessor(database, mismatched=False)
    seed_unsafe_open_admission_predecessor(database, seed=seed)
    database.expect_upgrade_failure(
        TARGET_REVISION,
        expected_exception=RuntimeError,
        message_fragment="open mutation admission lacks verified first-admission authority",
    )
    database.assert_revision(PREDECESSOR_REVISION)
