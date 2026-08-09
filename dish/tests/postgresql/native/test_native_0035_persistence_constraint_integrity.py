"""Native PostgreSQL certification for the 0035 persistence-integrity repair."""
from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from tests.support.postgresql.persistence_constraint_integrity_migration import (
    HONEST_FAILURE,
    LEASE_FAILURE,
    MANIFEST_FAILURE,
    PREDECESSOR_REVISION,
    REVALIDATION_FAILURE,
    TARGET_REVISION,
    assert_manifest_seed_survives,
    assert_row_exists,
    seed_duplicate_null_metadata_migration_bindings,
    seed_honest_binding,
    seed_imported_lease,
    seed_live_lease,
    seed_manifest_case,
)

pytestmark = [pytest.mark.postgresql, pytest.mark.native_postgresql]


def test_native_0035_candidate_manifest_integrity(native_migration_database) -> None:
    database = native_migration_database
    database.initialize(PREDECESSOR_REVISION)
    v2 = seed_manifest_case(
        database,
        label="native-valid-v2",
        manifest_version=2,
        readiness_inventory_sha256="1" * 64,
        readiness_completion_sha256="2" * 64,
    )
    v3 = seed_manifest_case(
        database,
        label="native-valid-v3",
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
        label="native-malformed-v2-manifest",
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

    database.initialize(PREDECESSOR_REVISION)
    seed_manifest_case(
        database,
        label="native-malformed-v2-revalidation",
        manifest_version=2,
        readiness_inventory_sha256="1" * 64,
        readiness_completion_sha256="2" * 64,
        observed_readiness_inventory_sha256=None,
    )
    database.expect_upgrade_failure(
        TARGET_REVISION,
        expected_exception=RuntimeError,
        message_fragment=REVALIDATION_FAILURE,
    )
    database.assert_revision(PREDECESSOR_REVISION)

    database.initialize(TARGET_REVISION)
    with pytest.raises(IntegrityError):
        seed_manifest_case(
            database,
            label="native-head-malformed-v2-manifest",
            manifest_version=2,
            readiness_inventory_sha256=None,
            readiness_completion_sha256="2" * 64,
            add_revalidation=False,
        )
    with pytest.raises(IntegrityError):
        seed_manifest_case(
            database,
            label="native-head-malformed-v2-revalidation",
            manifest_version=2,
            readiness_inventory_sha256="1" * 64,
            readiness_completion_sha256="2" * 64,
            observed_readiness_inventory_sha256=None,
        )


def test_native_0035_honest_migration_integrity(native_migration_database) -> None:
    database = native_migration_database
    database.initialize(PREDECESSOR_REVISION)
    first, second = seed_duplicate_null_metadata_migration_bindings(database)
    assert_row_exists(database, "honest_contract_bindings", "binding_id", first)
    assert_row_exists(database, "honest_contract_bindings", "binding_id", second)
    database.expect_upgrade_failure(
        TARGET_REVISION,
        expected_exception=RuntimeError,
        message_fragment=HONEST_FAILURE,
    )
    database.assert_revision(PREDECESSOR_REVISION)

    database.initialize(TARGET_REVISION)
    with pytest.raises(IntegrityError):
        seed_honest_binding(
            database,
            label="native-head-null-migration",
            binding_kind="migration",
            protocol_sha256="1" * 64,
            schema_sha256="2" * 64,
            migration_id="migration-v1-v2",
            migration_metadata_sha256=None,
        )
    valid = seed_honest_binding(
        database,
        label="native-head-valid-migration",
        binding_kind="migration",
        protocol_sha256="3" * 64,
        schema_sha256="4" * 64,
        migration_id="migration-v1-v2",
        migration_metadata_sha256="5" * 64,
    )
    assert_row_exists(database, "honest_contract_bindings", "binding_id", valid)
    seed_honest_binding(
        database,
        label="native-head-valid-release",
        binding_kind="release",
        protocol_sha256="6" * 64,
        schema_sha256="7" * 64,
    )
    with pytest.raises(IntegrityError):
        seed_honest_binding(
            database,
            label="native-head-duplicate-release",
            binding_kind="release",
            protocol_sha256="6" * 64,
            schema_sha256="7" * 64,
        )


def test_native_0035_imported_lease_integrity(native_migration_database) -> None:
    database = native_migration_database
    database.initialize(PREDECESSOR_REVISION)
    valid_predecessor = seed_imported_lease(
        database,
        label="native-valid-imported-predecessor",
        source_run_id="legacy-native-run",
    )
    database.upgrade(TARGET_REVISION)
    assert_row_exists(database, "service_leases", "lease_id", valid_predecessor)

    database.initialize(PREDECESSOR_REVISION)
    seed_imported_lease(database, label="native-bad-imported", source_run_id=None)
    database.expect_upgrade_failure(
        TARGET_REVISION,
        expected_exception=RuntimeError,
        message_fragment=LEASE_FAILURE,
    )
    database.assert_revision(PREDECESSOR_REVISION)

    database.initialize(TARGET_REVISION)
    with pytest.raises(IntegrityError):
        seed_imported_lease(database, label="native-head-null-imported", source_run_id=None)
    with pytest.raises(IntegrityError):
        seed_imported_lease(database, label="native-head-blank-imported", source_run_id="   ")
    imported = seed_imported_lease(
        database,
        label="native-head-valid-imported",
        source_run_id="legacy-native-source",
    )
    live = seed_live_lease(database, label="native-head-valid-live")
    assert_row_exists(database, "service_leases", "lease_id", imported)
    assert_row_exists(database, "service_leases", "lease_id", live)
