"""Close PostgreSQL CHECK NULL holes in persisted authority provenance.

Revision ID: 0035_persistence_constraint_integrity
Revises: 0034_cc5_schema_repair
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0035_persistence_constraint_integrity"
down_revision = "0034_cc5_schema_repair"
branch_labels = None
depends_on = None

_MANIFEST_CHECK = "ck_release_candidate_manifests_component_hash_lengths"
_REVALIDATION_CHECK = (
    "ck_candidate_manifest_revalidations_observed_component_hash_lengths"
)
_HONEST_CHECK = "ck_honest_contract_bindings_migration_fields_match_kind"
_LEASE_CHECK = "ck_service_leases_provenance_exact"

_MANIFEST_COMPONENTS_REPAIRED = (
    "length(mapping_membership_sha256) = 64 AND "
    "length(import_completion_sha256) = 64 AND "
    "length(typed_import_linkage_sha256) = 64 AND "
    "length(reconciliation_evidence_sha256) = 64 AND "
    "((manifest_version = 2 AND approval_reconciliation_run_id IS NULL "
    "AND readiness_inventory_sha256 IS NOT NULL "
    "AND length(readiness_inventory_sha256) = 64 "
    "AND readiness_completion_sha256 IS NOT NULL "
    "AND length(readiness_completion_sha256) = 64) OR "
    "(manifest_version = 3 AND approval_reconciliation_run_id IS NOT NULL "
    "AND readiness_inventory_sha256 IS NULL "
    "AND readiness_completion_sha256 IS NULL))"
)
_MANIFEST_COMPONENTS_PREDECESSOR = (
    "length(mapping_membership_sha256) = 64 AND "
    "length(import_completion_sha256) = 64 AND "
    "length(typed_import_linkage_sha256) = 64 AND "
    "length(reconciliation_evidence_sha256) = 64 AND "
    "((manifest_version = 2 AND approval_reconciliation_run_id IS NULL "
    "AND length(readiness_inventory_sha256) = 64 "
    "AND length(readiness_completion_sha256) = 64) OR "
    "(manifest_version = 3 AND approval_reconciliation_run_id IS NOT NULL "
    "AND readiness_inventory_sha256 IS NULL "
    "AND readiness_completion_sha256 IS NULL))"
)
_REVALIDATION_COMPONENTS_REPAIRED = (
    "length(observed_mapping_membership_sha256) = 64 AND "
    "length(observed_import_completion_sha256) = 64 AND "
    "length(observed_typed_import_linkage_sha256) = 64 AND "
    "length(observed_reconciliation_evidence_sha256) = 64 AND "
    "((manifest_version = 2 "
    "AND observed_readiness_inventory_sha256 IS NOT NULL "
    "AND length(observed_readiness_inventory_sha256) = 64 "
    "AND observed_readiness_completion_sha256 IS NOT NULL "
    "AND length(observed_readiness_completion_sha256) = 64) OR "
    "(manifest_version = 3 "
    "AND observed_readiness_inventory_sha256 IS NULL "
    "AND observed_readiness_completion_sha256 IS NULL))"
)
_REVALIDATION_COMPONENTS_PREDECESSOR = (
    "length(observed_mapping_membership_sha256) = 64 AND "
    "length(observed_import_completion_sha256) = 64 AND "
    "length(observed_typed_import_linkage_sha256) = 64 AND "
    "length(observed_reconciliation_evidence_sha256) = 64 AND "
    "((manifest_version = 2 "
    "AND length(observed_readiness_inventory_sha256) = 64 "
    "AND length(observed_readiness_completion_sha256) = 64) OR "
    "(manifest_version = 3 "
    "AND observed_readiness_inventory_sha256 IS NULL "
    "AND observed_readiness_completion_sha256 IS NULL))"
)
_HONEST_REPAIRED = (
    "(binding_kind <> 'migration' AND migration_id IS NULL "
    "AND source_schema_version IS NULL AND target_schema_version IS NULL "
    "AND migration_metadata_sha256 IS NULL) OR "
    "(binding_kind = 'migration' AND migration_id IS NOT NULL "
    "AND source_schema_version IS NOT NULL AND target_schema_version IS NOT NULL "
    "AND migration_metadata_sha256 IS NOT NULL "
    "AND length(migration_metadata_sha256) = 64)"
)
_HONEST_PREDECESSOR = (
    "(binding_kind <> 'migration' AND migration_id IS NULL "
    "AND source_schema_version IS NULL AND target_schema_version IS NULL "
    "AND migration_metadata_sha256 IS NULL) OR "
    "(binding_kind = 'migration' AND migration_id IS NOT NULL "
    "AND source_schema_version IS NOT NULL AND target_schema_version IS NOT NULL "
    "AND length(migration_metadata_sha256) = 64)"
)
_LEASE_REPAIRED = (
    "(import_run_id IS NULL AND run_id IS NOT NULL AND source_run_id IS NULL) OR "
    "(import_run_id IS NOT NULL AND run_id IS NULL "
    "AND source_run_id IS NOT NULL AND length(trim(source_run_id)) > 0)"
)
_LEASE_PREDECESSOR = (
    "(import_run_id IS NULL AND run_id IS NOT NULL AND source_run_id IS NULL) OR "
    "(import_run_id IS NOT NULL AND run_id IS NULL AND length(trim(source_run_id)) > 0)"
)


def _offline() -> bool:
    return bool(op.get_context().as_sql)


def _preflight_candidate_manifest_v2() -> None:
    if _offline():
        return
    bind = op.get_bind()
    manifest = bind.execute(
        sa.text(
            "SELECT manifest_id FROM release_candidate_manifests "
            "WHERE manifest_version = 2 AND ("
            "readiness_inventory_sha256 IS NULL OR "
            "readiness_completion_sha256 IS NULL OR "
            "length(readiness_inventory_sha256) <> 64 OR "
            "length(readiness_completion_sha256) <> 64) LIMIT 1"
        )
    ).scalar_one_or_none()
    if manifest is not None:
        raise RuntimeError(
            "0035 refuses malformed historical candidate manifest v2 row "
            f"{manifest}: readiness hashes must already be non-NULL 64-character values"
        )
    revalidation = bind.execute(
        sa.text(
            "SELECT revalidation_id FROM candidate_manifest_revalidations "
            "WHERE manifest_version = 2 AND ("
            "observed_readiness_inventory_sha256 IS NULL OR "
            "observed_readiness_completion_sha256 IS NULL OR "
            "length(observed_readiness_inventory_sha256) <> 64 OR "
            "length(observed_readiness_completion_sha256) <> 64) LIMIT 1"
        )
    ).scalar_one_or_none()
    if revalidation is not None:
        raise RuntimeError(
            "0035 refuses malformed historical candidate manifest v2 revalidation "
            f"{revalidation}: observed readiness hashes must already be non-NULL "
            "64-character values"
        )


def _preflight_honest_migration_bindings() -> None:
    if _offline():
        return
    binding = op.get_bind().execute(
        sa.text(
            "SELECT binding_id FROM honest_contract_bindings "
            "WHERE binding_kind = 'migration' AND ("
            "migration_metadata_sha256 IS NULL OR "
            "length(migration_metadata_sha256) <> 64) LIMIT 1"
        )
    ).scalar_one_or_none()
    if binding is not None:
        raise RuntimeError(
            "0035 refuses malformed migration honest contract binding "
            f"{binding}: migration metadata hash must already be non-NULL and length 64"
        )


def _preflight_imported_leases() -> None:
    if _offline():
        return
    lease = op.get_bind().execute(
        sa.text(
            "SELECT lease_id FROM service_leases "
            "WHERE import_run_id IS NOT NULL AND ("
            "run_id IS NOT NULL OR source_run_id IS NULL OR "
            "length(trim(source_run_id)) = 0) LIMIT 1"
        )
    ).scalar_one_or_none()
    if lease is not None:
        raise RuntimeError(
            "0035 refuses malformed imported service lease "
            f"{lease}: imported provenance requires a nonblank source_run_id and no live run_id"
        )


def _replace_constraints(*, repaired: bool) -> None:
    manifest_sql = _MANIFEST_COMPONENTS_REPAIRED if repaired else _MANIFEST_COMPONENTS_PREDECESSOR
    revalidation_sql = (
        _REVALIDATION_COMPONENTS_REPAIRED
        if repaired
        else _REVALIDATION_COMPONENTS_PREDECESSOR
    )
    honest_sql = _HONEST_REPAIRED if repaired else _HONEST_PREDECESSOR
    lease_sql = _LEASE_REPAIRED if repaired else _LEASE_PREDECESSOR

    with op.batch_alter_table("release_candidate_manifests") as batch:
        batch.drop_constraint(op.f(_MANIFEST_CHECK), type_="check")
        batch.create_check_constraint("component_hash_lengths", manifest_sql)
    with op.batch_alter_table("candidate_manifest_revalidations") as batch:
        batch.drop_constraint(op.f(_REVALIDATION_CHECK), type_="check")
        batch.create_check_constraint("observed_component_hash_lengths", revalidation_sql)
    with op.batch_alter_table("honest_contract_bindings") as batch:
        batch.drop_constraint(op.f(_HONEST_CHECK), type_="check")
        batch.create_check_constraint("migration_fields_match_kind", honest_sql)
    with op.batch_alter_table("service_leases") as batch:
        batch.drop_constraint(op.f(_LEASE_CHECK), type_="check")
        batch.create_check_constraint("provenance_exact", lease_sql)


def upgrade() -> None:
    # Run every fail-closed historical-data check before changing any constraint.
    _preflight_candidate_manifest_v2()
    _preflight_honest_migration_bindings()
    _preflight_imported_leases()
    _replace_constraints(repaired=True)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        raise RuntimeError(
            "refusing PostgreSQL downgrade to 0034 because it reopens known CHECK NULL holes; "
            "restore from a pre-0035 backup instead"
        )
    _replace_constraints(repaired=False)
