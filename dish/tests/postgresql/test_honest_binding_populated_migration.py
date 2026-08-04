from __future__ import annotations

import pytest

from tests.support.postgresql.honest_binding_migration import (
    TARGET_REVISION,
    assert_conflicting_upgrade_rejected,
    assert_null_safe_identity_enforced,
    install_predecessor,
    seed_conflicting_predecessor,
    seed_valid_predecessor,
)

pytestmark = pytest.mark.database_boundary


def test_sqlite_populated_honest_binding_upgrade_enforces_null_safe_identity(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    install_predecessor(database)
    seed_valid_predecessor(database)
    database.upgrade(TARGET_REVISION)
    database.assert_revision(TARGET_REVISION)
    assert_null_safe_identity_enforced(database)


def test_sqlite_populated_honest_binding_upgrade_rejects_conflicts(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    install_predecessor(database)
    seed_conflicting_predecessor(database)
    assert_conflicting_upgrade_rejected(database)
