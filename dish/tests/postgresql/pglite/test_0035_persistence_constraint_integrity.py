"""PGlite development evidence for the 0035 CHECK-integrity repair."""
from __future__ import annotations

import pytest

from tests.support.postgresql.persistence_constraint_integrity_migration import (
    HONEST_FAILURE,
    LEASE_FAILURE,
    MANIFEST_FAILURE,
    PREDECESSOR_REVISION,
    TARGET_REVISION,
    assert_manifest_seed_survives,
    assert_row_exists,
    seed_duplicate_null_metadata_migration_bindings,
    seed_honest_binding,
    seed_imported_lease,
    seed_manifest_case,
)

pytestmark = pytest.mark.pglite


def test_pglite_0035_manifest_v2_v3_upgrade_and_null_rejection(
    pglite_migration_database,
) -> None:
    database = pglite_migration_database
    database.initialize(PREDECESSOR_REVISION)
    v2 = seed_manifest_case(
        database,
        label="pglite-valid-v2",
        manifest_version=2,
        readiness_inventory_sha256="1" * 64,
        readiness_completion_sha256="2" * 64,
    )
    v3 = seed_manifest_case(
        database,
        label="pglite-valid-v3",
        manifest_version=3,
        readiness_inventory_sha256=None,
        readiness_completion_sha256=None,
    )
    database.upgrade(TARGET_REVISION)
    assert_manifest_seed_survives(database, v2)
    assert_manifest_seed_survives(database, v3)

    database.initialize(PREDECESSOR_REVISION)
    seed_manifest_case(
        database,
        label="pglite-malformed-v2",
        manifest_version=2,
        readiness_inventory_sha256=None,
        readiness_completion_sha256="2" * 64,
        add_revalidation=False,
    )
    database.expect_upgrade_failure(
        TARGET_REVISION,
        expected_exception=RuntimeError,
        message_fragment=MANIFEST_FAILURE,
    )
    database.assert_revision(PREDECESSOR_REVISION)

    database.initialize(TARGET_REVISION)
    current_v2 = seed_manifest_case(
        database,
        label="pglite-head-valid-v2",
        manifest_version=2,
        readiness_inventory_sha256="1" * 64,
        readiness_completion_sha256="2" * 64,
        add_revalidation=False,
    )
    assert_manifest_seed_survives(database, current_v2)


def test_pglite_0035_honest_migration_null_metadata_fails_closed(
    pglite_migration_database,
) -> None:
    database = pglite_migration_database
    database.initialize(PREDECESSOR_REVISION)
    seed_duplicate_null_metadata_migration_bindings(database)
    database.expect_upgrade_failure(
        TARGET_REVISION,
        expected_exception=RuntimeError,
        message_fragment=HONEST_FAILURE,
    )
    database.assert_revision(PREDECESSOR_REVISION)

    database.initialize(TARGET_REVISION)
    binding_id = seed_honest_binding(
        database,
        label="pglite-head-valid-migration",
        binding_kind="migration",
        protocol_sha256="3" * 64,
        schema_sha256="4" * 64,
        migration_id="migration-v1-v2",
        migration_metadata_sha256="5" * 64,
    )
    assert_row_exists(database, "honest_contract_bindings", "binding_id", binding_id)


def test_pglite_0035_imported_lease_requires_source_run_id(
    pglite_migration_database,
) -> None:
    database = pglite_migration_database
    database.initialize(PREDECESSOR_REVISION)
    seed_imported_lease(database, label="pglite-bad-imported", source_run_id=None)
    database.expect_upgrade_failure(
        TARGET_REVISION,
        expected_exception=RuntimeError,
        message_fragment=LEASE_FAILURE,
    )
    database.assert_revision(PREDECESSOR_REVISION)

    database.initialize(TARGET_REVISION)
    lease_id = seed_imported_lease(
        database,
        label="pglite-head-valid-imported",
        source_run_id="legacy-pglite-run",
    )
    assert_row_exists(database, "service_leases", "lease_id", lease_id)
