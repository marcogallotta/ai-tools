"""Reusable 0034 -> 0035 persistence-integrity migration fixtures."""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from dish_pg import candidate_manifest_models  # noqa: F401 -- register metadata
from dish_pg import models
from dish_pg import stage3_models  # noqa: F401 -- register metadata
from dish_pg import stage5_models  # noqa: F401 -- register metadata
from dish_pg import stage6_models  # noqa: F401 -- register metadata
from tests.support.postgresql.migrations import MigrationDatabase

PREDECESSOR_REVISION = "0034_cc5_schema_repair"
TARGET_REVISION = "0035_persistence_constraint_integrity"
NOW = datetime(2026, 8, 9, 14, 0, tzinfo=timezone.utc)

MANIFEST_FAILURE = "historical candidate manifest v2 row"
REVALIDATION_FAILURE = "historical candidate manifest v2 revalidation"
HONEST_FAILURE = "migration honest contract binding"
LEASE_FAILURE = "imported service lease"

_NAMESPACE = uuid.UUID("d5a4ac66-a741-4ae2-9b45-2ac46d245f95")


def _id(label: str) -> uuid.UUID:
    return uuid.uuid5(_NAMESPACE, label)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _table(name: str) -> sa.Table:
    return models.Base.metadata.tables[name]


def _insert(connection: sa.Connection, table_name: str, values: dict[str, object]) -> None:
    connection.execute(_table(table_name).insert().values(**values))


@dataclass(frozen=True)
class ManifestSeed:
    candidate_id: uuid.UUID
    manifest_id: uuid.UUID
    revalidation_id: uuid.UUID | None
    manifest_version: int
    canonical_fingerprint: str


def _seed_manifest_parents(
    connection: sa.Connection,
    *,
    label: str,
    manifest_version: int,
) -> dict[str, uuid.UUID | None]:
    import_run_id = _id(f"{label}:import-run")
    generation_id = _id(f"{label}:generation")
    honest_binding_id = _id(f"{label}:honest-binding")
    registry_version_id = _id(f"{label}:registry")
    registry_activation_id = _id(f"{label}:registry-activation")
    import_batch_id = _id(f"{label}:import-batch")
    shadow_baseline_id = _id(f"{label}:shadow")
    projection_epoch_id = _id(f"{label}:epoch")
    candidate_id = _id(f"{label}:candidate")

    _insert(
        connection,
        "stage_a_import_runs",
        {
            "import_run_id": import_run_id,
            "source_commit": _hash(f"{label}:source-commit"),
            "source_release": f"source-{label}",
            "legacy_generation_id": f"legacy-{label}",
            "baseline_high_water_mark": f"high-water-{label}",
            "source_bundle_sha256": _hash(f"{label}:bundle"),
            "status": "complete",
            "started_at": NOW,
            "completed_at": NOW,
            "provenance": {"fixture": label},
        },
    )
    _insert(
        connection,
        "authority_generations",
        {
            "generation_id": generation_id,
            "predecessor_generation_id": None,
            "creation_reason": "initial_cutover",
            "external_restore_control_id": None,
            "schema_head": PREDECESSOR_REVISION,
            "dish_release": "dish-test",
            "status": "pending",
            "created_at": NOW,
            "retired_at": None,
        },
    )
    _insert(
        connection,
        "honest_contract_bindings",
        {
            "binding_id": honest_binding_id,
            "binding_kind": "release",
            "source_identity": f"release:{label}",
            "dish_release": "dish-test",
            "honest_release": "honest-test",
            "protocol_release": "protocol-test",
            "protocol_sha256": _hash(f"{label}:protocol"),
            "schema_release": "schema-test",
            "schema_sha256": _hash(f"{label}:schema"),
            "migration_id": None,
            "source_schema_version": None,
            "target_schema_version": None,
            "migration_metadata_sha256": None,
            "source_ids": {"fixture": label},
            "provenance": {"fixture": label},
            "resolved_at": NOW,
        },
    )
    _insert(
        connection,
        "section_registry_versions",
        {
            "registry_version_id": registry_version_id,
            "generation_id": generation_id,
            "version_number": 1,
            "import_run_id": import_run_id,
            "contract_binding_id": honest_binding_id,
            "registry_sha256": _hash(f"{label}:registry"),
            "created_at": NOW,
        },
    )
    _insert(
        connection,
        "section_registry_activations",
        {
            "registry_activation_id": registry_activation_id,
            "generation_id": generation_id,
            "registry_version_id": registry_version_id,
            "activation_route": "import",
            "import_run_id": import_run_id,
            "command_execution_id": None,
            "registry_revision": 1,
            "activated_at": NOW,
        },
    )
    _insert(
        connection,
        "active_section_registries",
        {
            "generation_id": generation_id,
            "registry_version_id": registry_version_id,
            "registry_activation_id": registry_activation_id,
            "registry_revision": 1,
            "updated_at": NOW,
        },
    )
    _insert(
        connection,
        "source_import_batches",
        {
            "import_batch_id": import_batch_id,
            "generation_id": generation_id,
            "import_run_id": import_run_id,
            "source_release": f"source-{label}",
            "source_commit": _hash(f"{label}:batch-commit"),
            "source_database_sha256": _hash(f"{label}:database"),
            "source_sidecars": {},
            "ledger_through_commit": _hash(f"{label}:ledger"),
            "expected_entities": 0,
            "imported_entities": 0,
            "status": "complete",
            "started_at": NOW,
            "completed_at": NOW,
        },
    )
    _insert(
        connection,
        "shadow_baselines",
        {
            "shadow_baseline_id": shadow_baseline_id,
            "generation_id": generation_id,
            "source_generation_identity": f"source-generation-{label}",
            "source_commit": _hash(f"{label}:shadow-commit"),
            "baseline_sequence": 1,
            "status": "closed",
            "disqualification_reason": None,
            "created_at": NOW,
            "terminal_at": NOW,
        },
    )
    _insert(
        connection,
        "projection_epochs",
        {
            "projection_epoch_id": projection_epoch_id,
            "generation_id": generation_id,
            "epoch_number": 1,
            "status": "active",
            "activation_reason": f"fixture {label}",
            "external_effects_enabled": False,
            "created_at": NOW,
            "retired_at": None,
        },
    )
    _insert(
        connection,
        "release_candidates",
        {
            "candidate_id": candidate_id,
            "generation_id": generation_id,
            "source_import_batch_id": import_batch_id,
            "shadow_baseline_id": shadow_baseline_id,
            "projection_epoch_id": projection_epoch_id,
            "source_release": f"source-{label}",
            "source_commit": _hash(f"{label}:candidate-commit"),
            "ledger_through_commit": _hash(f"{label}:candidate-ledger"),
            "schema_head": PREDECESSOR_REVISION,
            "dish_release": "dish-test",
            "honest_release": "honest-test",
            "protocol_release": "protocol-test",
            "openapi_release": "openapi-test",
            "routing_release": "routing-test",
            "status": "assembling",
            "candidate_revision": 1,
            "validation_bundle_sha256": None,
            "created_at": NOW,
            "validated_at": None,
            "approved_at": None,
            "terminal_at": None,
        },
    )

    approval_reconciliation_run_id: uuid.UUID | None = None
    if manifest_version == 3:
        approval_reconciliation_run_id = _id(f"{label}:reconciliation")
        _insert(
            connection,
            "projection_reconciliation_runs",
            {
                "reconciliation_run_id": approval_reconciliation_run_id,
                "generation_id": generation_id,
                "projection_epoch_id": projection_epoch_id,
                "corpus_identity": f"corpus:{label}",
                "candidate_id": candidate_id,
                "registry_version_id": registry_version_id,
                "observation_started_at": NOW,
                "observation_completed_at": NOW,
                "external_snapshot_identity": f"snapshot:{label}",
                "external_high_water": f"high-water:{label}",
                "corpus_manifest_sha256": _hash(f"{label}:corpus"),
                "scope_complete": True,
                "adapter_contract_version": "fixture-v1",
                "evidence_recorded_at": NOW,
                "status": "complete",
                "expected_items": 0,
                "processed_items": 0,
                "started_at": NOW,
                "completed_at": NOW,
            },
        )

    return {
        "import_run_id": import_run_id,
        "generation_id": generation_id,
        "honest_binding_id": honest_binding_id,
        "registry_version_id": registry_version_id,
        "import_batch_id": import_batch_id,
        "shadow_baseline_id": shadow_baseline_id,
        "projection_epoch_id": projection_epoch_id,
        "candidate_id": candidate_id,
        "approval_reconciliation_run_id": approval_reconciliation_run_id,
    }


def seed_manifest_case(
    database: MigrationDatabase,
    *,
    label: str,
    manifest_version: int,
    readiness_inventory_sha256: str | None,
    readiness_completion_sha256: str | None,
    add_revalidation: bool = True,
    observed_readiness_inventory_sha256: str | None | object = ...,
    observed_readiness_completion_sha256: str | None | object = ...,
) -> ManifestSeed:
    def seed(connection: sa.Connection) -> ManifestSeed:
        parents = _seed_manifest_parents(
            connection, label=label, manifest_version=manifest_version
        )
        manifest_id = _id(f"{label}:manifest")
        fingerprint = _hash(f"{label}:fingerprint")
        component_hashes = {
            "mapping_membership_sha256": _hash(f"{label}:mapping"),
            "import_completion_sha256": _hash(f"{label}:import-completion"),
            "typed_import_linkage_sha256": _hash(f"{label}:typed-linkage"),
            "reconciliation_evidence_sha256": _hash(f"{label}:reconciliation-evidence"),
        }
        _insert(
            connection,
            "release_candidate_manifests",
            {
                "manifest_id": manifest_id,
                "candidate_id": parents["candidate_id"],
                "manifest_version": manifest_version,
                "canonical_fingerprint": fingerprint,
                "generation_id": parents["generation_id"],
                "source_import_batch_id": parents["import_batch_id"],
                "source_import_run_id": parents["import_run_id"],
                "shadow_baseline_id": parents["shadow_baseline_id"],
                "projection_epoch_id": parents["projection_epoch_id"],
                "registry_version_id": parents["registry_version_id"],
                "honest_binding_id": parents["honest_binding_id"],
                "approval_reconciliation_run_id": parents[
                    "approval_reconciliation_run_id"
                ],
                **component_hashes,
                "readiness_inventory_sha256": readiness_inventory_sha256,
                "readiness_completion_sha256": readiness_completion_sha256,
                "builder_contract_version": f"fixture-manifest-v{manifest_version}",
                "built_at": NOW,
            },
        )

        revalidation_id: uuid.UUID | None = None
        if add_revalidation:
            revalidation_id = _id(f"{label}:revalidation")
            observed_inventory = (
                readiness_inventory_sha256
                if observed_readiness_inventory_sha256 is ...
                else observed_readiness_inventory_sha256
            )
            observed_completion = (
                readiness_completion_sha256
                if observed_readiness_completion_sha256 is ...
                else observed_readiness_completion_sha256
            )
            _insert(
                connection,
                "candidate_manifest_revalidations",
                {
                    "revalidation_id": revalidation_id,
                    "candidate_id": parents["candidate_id"],
                    "manifest_id": manifest_id,
                    "manifest_version": manifest_version,
                    "approved_fingerprint": fingerprint,
                    "observed_fingerprint": fingerprint,
                    "observed_mapping_membership_sha256": component_hashes[
                        "mapping_membership_sha256"
                    ],
                    "observed_import_completion_sha256": component_hashes[
                        "import_completion_sha256"
                    ],
                    "observed_typed_import_linkage_sha256": component_hashes[
                        "typed_import_linkage_sha256"
                    ],
                    "observed_reconciliation_evidence_sha256": component_hashes[
                        "reconciliation_evidence_sha256"
                    ],
                    "observed_readiness_inventory_sha256": observed_inventory,
                    "observed_readiness_completion_sha256": observed_completion,
                    "result": "matched",
                    "revalidated_at": NOW,
                },
            )
        return ManifestSeed(
            candidate_id=parents["candidate_id"],
            manifest_id=manifest_id,
            revalidation_id=revalidation_id,
            manifest_version=manifest_version,
            canonical_fingerprint=fingerprint,
        )

    return database.seed(seed)


def assert_manifest_seed_survives(database: MigrationDatabase, seed: ManifestSeed) -> None:
    def read(connection: sa.Connection) -> None:
        manifests = _table("release_candidate_manifests")
        assert connection.execute(
            sa.select(sa.func.count()).select_from(manifests).where(
                manifests.c.manifest_id == seed.manifest_id
            )
        ).scalar_one() == 1
        if seed.revalidation_id is not None:
            revalidations = _table("candidate_manifest_revalidations")
            assert connection.execute(
                sa.select(sa.func.count()).select_from(revalidations).where(
                    revalidations.c.revalidation_id == seed.revalidation_id
                )
            ).scalar_one() == 1

    database.read(read)


def _honest_values(
    *,
    label: str,
    binding_kind: str,
    protocol_sha256: str,
    schema_sha256: str,
    migration_id: str | None,
    migration_metadata_sha256: str | None,
) -> dict[str, object]:
    migration = binding_kind == "migration"
    return {
        "binding_id": _id(f"honest:{label}:binding"),
        "binding_kind": binding_kind,
        "source_identity": f"source:{label}",
        "dish_release": "dish-test",
        "honest_release": "honest-test",
        "protocol_release": "protocol-test",
        "protocol_sha256": protocol_sha256,
        "schema_release": "schema-test",
        "schema_sha256": schema_sha256,
        "migration_id": migration_id if migration else None,
        "source_schema_version": "v1" if migration else None,
        "target_schema_version": "v2" if migration else None,
        "migration_metadata_sha256": migration_metadata_sha256 if migration else None,
        "source_ids": {"fixture": label},
        "provenance": {"fixture": label},
        "resolved_at": NOW,
    }


def seed_honest_binding(
    database: MigrationDatabase,
    *,
    label: str,
    binding_kind: str,
    protocol_sha256: str,
    schema_sha256: str,
    migration_id: str | None = None,
    migration_metadata_sha256: str | None = None,
) -> uuid.UUID:
    values = _honest_values(
        label=label,
        binding_kind=binding_kind,
        protocol_sha256=protocol_sha256,
        schema_sha256=schema_sha256,
        migration_id=migration_id,
        migration_metadata_sha256=migration_metadata_sha256,
    )
    database.seed(
        lambda connection: connection.execute(
            _table("honest_contract_bindings").insert().values(**values)
        )
    )
    return values["binding_id"]


def seed_duplicate_null_metadata_migration_bindings(database: MigrationDatabase) -> tuple[uuid.UUID, uuid.UUID]:
    first = _honest_values(
        label="duplicate-null-migration-a",
        binding_kind="migration",
        protocol_sha256="6" * 64,
        schema_sha256="7" * 64,
        migration_id="migration-v1-v2",
        migration_metadata_sha256=None,
    )
    second = _honest_values(
        label="duplicate-null-migration-b",
        binding_kind="migration",
        protocol_sha256="6" * 64,
        schema_sha256="7" * 64,
        migration_id="migration-v1-v2",
        migration_metadata_sha256=None,
    )
    database.seed(
        lambda connection: connection.execute(
            _table("honest_contract_bindings").insert(), [first, second]
        )
    )
    return first["binding_id"], second["binding_id"]


def _seed_lease_parents(connection: sa.Connection, *, label: str) -> dict[str, uuid.UUID]:
    import_run_id = _id(f"lease:{label}:import-run")
    generation_id = _id(f"lease:{label}:generation")
    task_id = _id(f"lease:{label}:task")
    _insert(
        connection,
        "stage_a_import_runs",
        {
            "import_run_id": import_run_id,
            "source_commit": _hash(f"lease:{label}:commit"),
            "source_release": f"source-{label}",
            "legacy_generation_id": f"legacy-lease-{label}",
            "baseline_high_water_mark": f"lease-high-water-{label}",
            "source_bundle_sha256": _hash(f"lease:{label}:bundle"),
            "status": "complete",
            "started_at": NOW,
            "completed_at": NOW,
            "provenance": {"fixture": label},
        },
    )
    _insert(
        connection,
        "authority_generations",
        {
            "generation_id": generation_id,
            "predecessor_generation_id": None,
            "creation_reason": "initial_cutover",
            "external_restore_control_id": None,
            "schema_head": PREDECESSOR_REVISION,
            "dish_release": "dish-test",
            "status": "pending",
            "created_at": NOW,
            "retired_at": None,
        },
    )
    _insert(
        connection,
        "dish_tasks",
        {
            "task_id": task_id,
            "existence_state": "ordinary",
            "creation_route": "import",
            "import_run_id": import_run_id,
            "command_execution_id": None,
            "created_at": NOW,
            "retired_at": None,
        },
    )
    return {
        "import_run_id": import_run_id,
        "generation_id": generation_id,
        "task_id": task_id,
    }


def seed_imported_lease(
    database: MigrationDatabase,
    *,
    label: str,
    source_run_id: str | None,
) -> uuid.UUID:
    lease_id = _id(f"imported-lease:{label}:lease")

    def seed(connection: sa.Connection) -> None:
        parents = _seed_lease_parents(connection, label=f"imported-{label}")
        _insert(
            connection,
            "service_leases",
            {
                "lease_id": lease_id,
                "generation_id": parents["generation_id"],
                "task_id": parents["task_id"],
                "operation_id": None,
                "run_id": None,
                "import_run_id": parents["import_run_id"],
                "source_run_id": source_run_id,
                "owner_id": f"imported-owner:{label}",
                "lease_kind": "admin_request",
                "actor_role": None,
                "actor_attempt_sequence": None,
                "verification_cycle_id": None,
                "state": "released",
                "issued_at": NOW,
                "expires_at": NOW + timedelta(minutes=5),
                "lease_revision": 1,
                "terminal_at": NOW + timedelta(minutes=1),
            },
        )

    database.seed(seed)
    return lease_id


def seed_live_lease(database: MigrationDatabase, *, label: str) -> uuid.UUID:
    lease_id = _id(f"live-lease:{label}:lease")

    def seed(connection: sa.Connection) -> None:
        parents = _seed_lease_parents(connection, label=f"live-{label}")
        run_id = _id(f"live-lease:{label}:run")
        owner_id = f"live-owner:{label}"
        connection.execute(
            _table("authority_generations")
            .update()
            .where(
                _table("authority_generations").c.generation_id
                == parents["generation_id"]
            )
            .values(status="active")
        )
        _insert(
            connection,
            "service_runs",
            {
                "run_id": run_id,
                "generation_id": parents["generation_id"],
                "owner_id": owner_id,
                "agent": "service",
                "capability_digest": hashlib.sha256(f"capability:{label}".encode()).digest(),
                "bootstrap_id": None,
                "status": "active",
                "registered_at": NOW,
                "retired_at": None,
            },
        )
        _insert(
            connection,
            "service_leases",
            {
                "lease_id": lease_id,
                "generation_id": parents["generation_id"],
                "task_id": parents["task_id"],
                "operation_id": None,
                "run_id": run_id,
                "import_run_id": None,
                "source_run_id": None,
                "owner_id": owner_id,
                "lease_kind": "admin_request",
                "actor_role": None,
                "actor_attempt_sequence": None,
                "verification_cycle_id": None,
                "state": "active",
                "issued_at": NOW,
                "expires_at": NOW + timedelta(minutes=5),
                "lease_revision": 1,
                "terminal_at": None,
            },
        )

    database.seed(seed)
    return lease_id


def assert_row_exists(database: MigrationDatabase, table_name: str, key: str, value: object) -> None:
    def read(connection: sa.Connection) -> None:
        table = _table(table_name)
        assert connection.execute(
            sa.select(sa.func.count()).select_from(table).where(table.c[key] == value)
        ).scalar_one() == 1

    database.read(read)
