"""SQLite database-boundary evidence for the 0035 CHECK-integrity repair."""
from __future__ import annotations

import importlib
import re

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.sql.schema import CheckConstraint

from dish_pg import models
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

pytestmark = pytest.mark.database_boundary


def test_0035_valid_historical_v2_and_current_v3_manifest_state_survives_upgrade(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    database.initialize(PREDECESSOR_REVISION)
    v2 = seed_manifest_case(
        database,
        label="sqlite-valid-v2",
        manifest_version=2,
        readiness_inventory_sha256="1" * 64,
        readiness_completion_sha256="2" * 64,
    )
    v3 = seed_manifest_case(
        database,
        label="sqlite-valid-v3",
        manifest_version=3,
        readiness_inventory_sha256=None,
        readiness_completion_sha256=None,
    )
    database.upgrade(TARGET_REVISION)
    database.assert_revision(TARGET_REVISION)
    assert_manifest_seed_survives(database, v2)
    assert_manifest_seed_survives(database, v3)


def test_0035_rejects_historical_v2_manifest_missing_readiness_hash(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    database.initialize(PREDECESSOR_REVISION)
    seed_manifest_case(
        database,
        label="sqlite-malformed-v2-manifest",
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


def test_0035_rejects_historical_v2_revalidation_missing_readiness_hash(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    database.initialize(PREDECESSOR_REVISION)
    seed_manifest_case(
        database,
        label="sqlite-malformed-v2-revalidation",
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


def test_0035_current_head_manifest_checks_reject_v2_nulls_and_preserve_v2_v3(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    database.initialize(TARGET_REVISION)
    with pytest.raises(IntegrityError):
        seed_manifest_case(
            database,
            label="sqlite-head-malformed-v2",
            manifest_version=2,
            readiness_inventory_sha256=None,
            readiness_completion_sha256="2" * 64,
            add_revalidation=False,
        )
    with pytest.raises(IntegrityError):
        seed_manifest_case(
            database,
            label="sqlite-head-malformed-v2-revalidation",
            manifest_version=2,
            readiness_inventory_sha256="1" * 64,
            readiness_completion_sha256="2" * 64,
            observed_readiness_inventory_sha256=None,
        )
    v2 = seed_manifest_case(
        database,
        label="sqlite-head-valid-v2",
        manifest_version=2,
        readiness_inventory_sha256="1" * 64,
        readiness_completion_sha256="2" * 64,
    )
    v3 = seed_manifest_case(
        database,
        label="sqlite-head-valid-v3",
        manifest_version=3,
        readiness_inventory_sha256=None,
        readiness_completion_sha256=None,
    )
    assert_manifest_seed_survives(database, v2)
    assert_manifest_seed_survives(database, v3)


def test_0035_honest_binding_valid_predecessor_upgrades_and_preserves_nonmigration_nulls(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    database.initialize(PREDECESSOR_REVISION)
    release_id = seed_honest_binding(
        database,
        label="sqlite-valid-release",
        binding_kind="release",
        protocol_sha256="1" * 64,
        schema_sha256="2" * 64,
    )
    task_schema_id = seed_honest_binding(
        database,
        label="sqlite-valid-task-schema",
        binding_kind="task_schema",
        protocol_sha256="3" * 64,
        schema_sha256="4" * 64,
    )
    migration_id = seed_honest_binding(
        database,
        label="sqlite-valid-migration",
        binding_kind="migration",
        protocol_sha256="5" * 64,
        schema_sha256="6" * 64,
        migration_id="migration-v1-v2",
        migration_metadata_sha256="7" * 64,
    )
    database.upgrade(TARGET_REVISION)
    for binding_id in (release_id, task_schema_id, migration_id):
        assert_row_exists(database, "honest_contract_bindings", "binding_id", binding_id)


def test_0035_rejects_predecessor_duplicate_migration_identity_using_null_metadata(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
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


def test_0035_current_head_honest_binding_rejects_null_migration_metadata_and_keeps_null_safe_uniqueness(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    database.initialize(TARGET_REVISION)
    with pytest.raises(IntegrityError):
        seed_honest_binding(
            database,
            label="sqlite-head-null-migration",
            binding_kind="migration",
            protocol_sha256="1" * 64,
            schema_sha256="2" * 64,
            migration_id="migration-v1-v2",
            migration_metadata_sha256=None,
        )
    seed_honest_binding(
        database,
        label="sqlite-head-valid-release",
        binding_kind="release",
        protocol_sha256="3" * 64,
        schema_sha256="4" * 64,
    )
    seed_honest_binding(
        database,
        label="sqlite-head-valid-task-schema",
        binding_kind="task_schema",
        protocol_sha256="5" * 64,
        schema_sha256="6" * 64,
    )
    seed_honest_binding(
        database,
        label="sqlite-head-valid-migration",
        binding_kind="migration",
        protocol_sha256="7" * 64,
        schema_sha256="8" * 64,
        migration_id="migration-v1-v2",
        migration_metadata_sha256="9" * 64,
    )
    with pytest.raises(IntegrityError):
        seed_honest_binding(
            database,
            label="sqlite-head-duplicate-release",
            binding_kind="release",
            protocol_sha256="3" * 64,
            schema_sha256="4" * 64,
        )


def test_0035_valid_imported_lease_upgrades_and_live_lease_semantics_survive(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    database.initialize(PREDECESSOR_REVISION)
    imported = seed_imported_lease(
        database,
        label="sqlite-valid-imported",
        source_run_id="legacy-run-42",
    )
    database.upgrade(TARGET_REVISION)
    assert_row_exists(database, "service_leases", "lease_id", imported)
    live = seed_live_lease(database, label="sqlite-valid-live")
    assert_row_exists(database, "service_leases", "lease_id", live)


def test_0035_rejects_predecessor_imported_lease_without_source_run_id(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    database.initialize(PREDECESSOR_REVISION)
    seed_imported_lease(database, label="sqlite-bad-imported", source_run_id=None)
    database.expect_upgrade_failure(
        TARGET_REVISION,
        expected_exception=RuntimeError,
        message_fragment=LEASE_FAILURE,
    )
    database.assert_revision(PREDECESSOR_REVISION)


def test_0035_current_head_imported_lease_requires_nonblank_source_run_id(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    database.initialize(TARGET_REVISION)
    with pytest.raises(IntegrityError):
        seed_imported_lease(database, label="sqlite-head-null-imported", source_run_id=None)
    with pytest.raises(IntegrityError):
        seed_imported_lease(database, label="sqlite-head-blank-imported", source_run_id="   ")
    lease_id = seed_imported_lease(
        database,
        label="sqlite-head-valid-imported",
        source_run_id="legacy-source-run",
    )
    assert_row_exists(database, "service_leases", "lease_id", lease_id)


def _normalized_check(sql: str) -> str:
    return re.sub(r'[\s()"]+', "", sql).lower()


def test_0035_repaired_check_constraints_match_sqlalchemy_metadata(
    sqlite_migration_database,
) -> None:
    database = sqlite_migration_database
    database.initialize(TARGET_REVISION)
    migration_0035 = importlib.import_module(
        "dish_pg.migrations.versions.0035_persistence_constraint_integrity"
    )
    expected = {
        "release_candidate_manifests": (
            "ck_release_candidate_manifests_component_hash_lengths",
            migration_0035._MANIFEST_COMPONENTS_REPAIRED,
        ),
        "candidate_manifest_revalidations": (
            "ck_candidate_manifest_revalidations_observed_component_hash_lengths",
            migration_0035._REVALIDATION_COMPONENTS_REPAIRED,
        ),
        "honest_contract_bindings": (
            "ck_honest_contract_bindings_migration_fields_match_kind",
            None,
        ),
        "service_leases": ("ck_service_leases_provenance_exact", None),
    }

    def read(connection: sa.Connection) -> None:
        inspector = sa.inspect(connection)
        for table_name, (constraint_name, historical_sql) in expected.items():
            database_checks = {
                row["name"]: row["sqltext"]
                for row in inspector.get_check_constraints(table_name)
            }
            if historical_sql is None:
                orm_constraint = next(
                    constraint
                    for constraint in models.Base.metadata.tables[table_name].constraints
                    if isinstance(constraint, CheckConstraint)
                    and constraint.name == constraint_name
                )
                expected_sql = str(orm_constraint.sqltext)
            else:
                expected_sql = historical_sql
            assert _normalized_check(database_checks[constraint_name]) == _normalized_check(
                expected_sql
            )

    database.read(read)
