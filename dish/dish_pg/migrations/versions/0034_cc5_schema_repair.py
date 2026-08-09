"""Repair the omitted CC5 worker-readiness / manifest-v3 schema transition.

Revision ID: 0034_cc5_schema_repair
Revises: 0033_frontend_security

CC5 changed the live ORM contract but its declared 0031 migration was never
checked in.  This forward repair deliberately does not rewrite 0032/0033
history.  It preserves historical v2 manifest rows and refuses to discard any
legacy worker-readiness evidence.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0034_cc5_schema_repair"
down_revision = "0033_frontend_security"
branch_labels = None
depends_on = None

_LEGACY_READINESS_TABLES = (
    "projection_worker_readiness",
    "worker_probe_inventories",
    "worker_probe_requirements",
    "worker_probe_evidence",
    "worker_readiness_completions",
)


def _assert_legacy_readiness_empty() -> None:
    if op.get_context().as_sql:
        return
    bind = op.get_bind()
    counts = {
        table: int(
            bind.execute(sa.text(f"SELECT count(*) FROM {table}")).scalar_one()
        )
        for table in _LEGACY_READINESS_TABLES
    }
    populated = {table: count for table, count in counts.items() if count}
    if populated:
        detail = ", ".join(f"{table}={count}" for table, count in populated.items())
        raise RuntimeError(
            "CC5 schema repair refuses destructive worker-readiness consolidation "
            f"while legacy evidence exists ({detail}); export Class-C typed-readiness "
            "evidence and rebuild/reseed the dark-launch target before upgrading"
        )


def _upgrade_manifest_contract() -> None:
    with op.batch_alter_table("release_candidate_manifests") as batch:
        batch.add_column(
            sa.Column("approval_reconciliation_run_id", sa.Uuid(), nullable=True)
        )
        batch.create_foreign_key(
            "fk_rc_manifests_approval_reconciliation_run_id",
            "projection_reconciliation_runs",
            ["approval_reconciliation_run_id"],
            ["reconciliation_run_id"],
            ondelete="RESTRICT",
        )
        batch.alter_column(
            "readiness_inventory_sha256",
            existing_type=sa.String(64),
            nullable=True,
        )
        batch.alter_column(
            "readiness_completion_sha256",
            existing_type=sa.String(64),
            nullable=True,
        )
        batch.drop_constraint(
            "ck_release_candidate_manifests_manifest_version_two",
            type_="check",
        )
        batch.drop_constraint(
            "ck_release_candidate_manifests_component_hash_lengths",
            type_="check",
        )
        batch.create_check_constraint(
            "manifest_version_supported",
            "manifest_version IN (2, 3)",
        )
        batch.create_check_constraint(
            "component_hash_lengths",
            "length(mapping_membership_sha256) = 64 AND "
            "length(import_completion_sha256) = 64 AND "
            "length(typed_import_linkage_sha256) = 64 AND "
            "length(reconciliation_evidence_sha256) = 64 AND "
            "((manifest_version = 2 AND approval_reconciliation_run_id IS NULL "
            "AND length(readiness_inventory_sha256) = 64 "
            "AND length(readiness_completion_sha256) = 64) OR "
            "(manifest_version = 3 AND approval_reconciliation_run_id IS NOT NULL "
            "AND readiness_inventory_sha256 IS NULL "
            "AND readiness_completion_sha256 IS NULL))",
        )

    with op.batch_alter_table("cutover_approval_manifest_bindings") as batch:
        batch.drop_constraint(
            "ck_cutover_approval_manifest_bindings_manifest_version_two",
            type_="check",
        )
        batch.create_check_constraint(
            "version_supported",
            "manifest_version IN (2, 3)",
        )

    with op.batch_alter_table("candidate_manifest_revalidations") as batch:
        batch.alter_column(
            "observed_readiness_inventory_sha256",
            existing_type=sa.String(64),
            nullable=True,
        )
        batch.alter_column(
            "observed_readiness_completion_sha256",
            existing_type=sa.String(64),
            nullable=True,
        )
        batch.drop_constraint(
            "ck_candidate_manifest_revalidations_manifest_version_two",
            type_="check",
        )
        batch.drop_constraint(
            "ck_candidate_manifest_revalidations_component_hash_lengths",
            type_="check",
        )
        batch.create_check_constraint(
            "manifest_version_supported",
            "manifest_version IN (2, 3)",
        )
        batch.create_check_constraint(
            "component_hash_lengths",
            "length(observed_mapping_membership_sha256) = 64 AND "
            "length(observed_import_completion_sha256) = 64 AND "
            "length(observed_typed_import_linkage_sha256) = 64 AND "
            "length(observed_reconciliation_evidence_sha256) = 64 AND "
            "((manifest_version = 2 "
            "AND length(observed_readiness_inventory_sha256) = 64 "
            "AND length(observed_readiness_completion_sha256) = 64) OR "
            "(manifest_version = 3 "
            "AND observed_readiness_inventory_sha256 IS NULL "
            "AND observed_readiness_completion_sha256 IS NULL))",
        )


def _drop_legacy_readiness_schema() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP FUNCTION IF EXISTS dish_validate_worker_probe_inventory() CASCADE; "
            "DROP FUNCTION IF EXISTS dish_validate_worker_probe_evidence() CASCADE; "
            "DROP FUNCTION IF EXISTS dish_validate_worker_readiness_completion() CASCADE; "
            "DROP FUNCTION IF EXISTS dish_reject_typed_readiness_mutation() CASCADE"
        )
    op.drop_table("worker_readiness_completions")
    op.drop_index(
        "ix_worker_probe_evidence_candidate_epoch",
        table_name="worker_probe_evidence",
    )
    op.drop_table("worker_probe_evidence")
    op.drop_table("worker_probe_requirements")
    op.drop_table("projection_worker_readiness")
    op.drop_table("worker_probe_inventories")


def _create_worker_readiness_report() -> None:
    op.create_table(
        "projection_worker_readiness",
        sa.Column("readiness_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("projection_epoch_id", sa.Uuid(), nullable=False),
        sa.Column("reconciliation_run_id", sa.Uuid(), nullable=False),
        sa.Column("worker_identity", sa.String(256), nullable=False),
        sa.Column("worker_release", sa.String(128), nullable=False),
        sa.Column("deployed_artifact_sha256", sa.String(64), nullable=False),
        sa.Column("report_contract_version", sa.String(64), nullable=False),
        sa.Column("claim_probe_result", sa.String(16), nullable=False),
        sa.Column("claim_execution_identity", sa.String(256), nullable=False),
        sa.Column("claim_evidence_identity", sa.String(512), nullable=False),
        sa.Column("exact_write_probe_result", sa.String(16), nullable=False),
        sa.Column("exact_write_execution_identity", sa.String(256), nullable=False),
        sa.Column("exact_write_evidence_identity", sa.String(512), nullable=False),
        sa.Column("restart_probe_result", sa.String(16), nullable=False),
        sa.Column("restart_execution_identity", sa.String(256), nullable=False),
        sa.Column("restart_evidence_identity", sa.String(512), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("report_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "length(trim(worker_identity)) > 0",
            name="ck_projection_worker_readiness_worker_identity_nonblank",
        ),
        sa.CheckConstraint(
            "length(trim(worker_release)) > 0",
            name="ck_projection_worker_readiness_worker_release_nonblank",
        ),
        sa.CheckConstraint(
            "length(deployed_artifact_sha256) = 64",
            name="ck_projection_worker_readiness_deployed_artifact_hash_length",
        ),
        sa.CheckConstraint(
            "report_contract_version = 'projection-worker-readiness-v1'",
            name="ck_projection_worker_readiness_report_contract_version_exact",
        ),
        sa.CheckConstraint(
            "claim_probe_result IN ('pass','fail','error') AND "
            "exact_write_probe_result IN ('pass','fail','error') AND "
            "restart_probe_result IN ('pass','fail','error')",
            name="ck_projection_worker_readiness_probe_results_allowed",
        ),
        sa.CheckConstraint(
            "length(trim(claim_execution_identity)) > 0 AND "
            "length(trim(exact_write_execution_identity)) > 0 AND "
            "length(trim(restart_execution_identity)) > 0",
            name="ck_projection_worker_readiness_execution_identities_nonblank",
        ),
        sa.CheckConstraint(
            "length(trim(claim_evidence_identity)) > 0 AND "
            "length(trim(exact_write_evidence_identity)) > 0 AND "
            "length(trim(restart_evidence_identity)) > 0",
            name="ck_projection_worker_readiness_evidence_identities_nonblank",
        ),
        sa.CheckConstraint(
            "length(report_sha256) = 64",
            name="ck_projection_worker_readiness_report_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["release_candidates.candidate_id"],
            name="fk_projection_worker_readiness_candidate_id_release_candidates",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["projection_epoch_id"],
            ["projection_epochs.projection_epoch_id"],
            name="fk_projection_worker_readiness_projection_epoch_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["reconciliation_run_id"],
            ["projection_reconciliation_runs.reconciliation_run_id"],
            name="fk_projection_worker_readiness_reconciliation_run_id",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "readiness_id", name="pk_projection_worker_readiness"
        ),
        sa.UniqueConstraint(
            "candidate_id", name="uq_projection_worker_readiness_candidate_id"
        ),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER projection_worker_readiness_immutable_update "
            "BEFORE UPDATE ON projection_worker_readiness FOR EACH ROW "
            "EXECUTE FUNCTION dish_reject_immutable_release_evidence()"
        )
        op.execute(
            "CREATE TRIGGER projection_worker_readiness_immutable_delete "
            "BEFORE DELETE ON projection_worker_readiness FOR EACH ROW "
            "EXECUTE FUNCTION dish_reject_immutable_release_evidence()"
        )


def _upgrade_postgresql_candidate_activation_guard() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_release_candidate_transition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            manifest release_candidate_manifests%ROWTYPE;
            latest_revalidation candidate_manifest_revalidations%ROWTYPE;
            approval_bound_at timestamptz;
        BEGIN
            IF OLD.candidate_id <> NEW.candidate_id
               OR OLD.generation_id <> NEW.generation_id
               OR OLD.source_import_batch_id <> NEW.source_import_batch_id
               OR OLD.shadow_baseline_id <> NEW.shadow_baseline_id
               OR OLD.projection_epoch_id <> NEW.projection_epoch_id
               OR OLD.source_release <> NEW.source_release
               OR OLD.source_commit <> NEW.source_commit
               OR OLD.ledger_through_commit <> NEW.ledger_through_commit
               OR OLD.schema_head <> NEW.schema_head
               OR OLD.dish_release <> NEW.dish_release
               OR OLD.honest_release <> NEW.honest_release
               OR OLD.protocol_release <> NEW.protocol_release
               OR OLD.openapi_release <> NEW.openapi_release
               OR OLD.routing_release <> NEW.routing_release
               OR OLD.created_at <> NEW.created_at THEN
                RAISE EXCEPTION 'release candidate identity is immutable';
            END IF;
            IF NEW.candidate_revision <> OLD.candidate_revision + 1 THEN
                RAISE EXCEPTION
                    'release candidate revision must advance exactly once';
            END IF;
            IF (OLD.status = 'assembling'
                    AND NEW.status NOT IN ('validated','aborted'))
               OR (OLD.status = 'validated'
                    AND NEW.status NOT IN ('approved','aborted'))
               OR (OLD.status = 'approved'
                    AND NEW.status NOT IN ('activated','aborted'))
               OR OLD.status IN ('activated','aborted') THEN
                RAISE EXCEPTION 'illegal release candidate transition';
            END IF;
            IF OLD.status = 'validated' AND NEW.status = 'approved'
               AND NOT EXISTS (
                    SELECT 1
                      FROM cutover_approval_manifest_bindings b
                      JOIN cutover_approvals a
                        ON a.approval_id=b.approval_id
                       AND a.candidate_id=b.candidate_id
                     WHERE b.candidate_id=NEW.candidate_id
               ) THEN
                RAISE EXCEPTION
                    'candidate approval lacks exact authority manifest binding';
            END IF;
            IF OLD.status = 'approved' AND NEW.status = 'activated' THEN
                SELECT m.* INTO manifest
                  FROM release_candidate_manifests m
                 WHERE m.candidate_id=NEW.candidate_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'candidate activation lacks authority manifest';
                END IF;
                IF manifest.manifest_version <> 3
                   OR manifest.approval_reconciliation_run_id IS NULL THEN
                    RAISE EXCEPTION
                        'candidate activation requires forward manifest v3';
                END IF;
                SELECT b.bound_at INTO approval_bound_at
                  FROM cutover_approval_manifest_bindings b
                 WHERE b.candidate_id=NEW.candidate_id
                   AND b.manifest_id=manifest.manifest_id
                   AND b.manifest_version=3;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'candidate activation lacks exact approval manifest binding';
                END IF;
                SELECT r.* INTO latest_revalidation
                  FROM candidate_manifest_revalidations r
                 WHERE r.candidate_id=NEW.candidate_id
                   AND r.manifest_id=manifest.manifest_id
                 ORDER BY r.revalidated_at DESC, r.revalidation_id DESC
                 LIMIT 1;
                IF NOT FOUND
                   OR latest_revalidation.result <> 'matched'
                   OR latest_revalidation.observed_fingerprint
                        <> manifest.canonical_fingerprint
                   OR latest_revalidation.revalidated_at < approval_bound_at THEN
                    RAISE EXCEPTION
                        'candidate activation lacks fresh matched manifest revalidation';
                END IF;
                IF NOT EXISTS (
                    SELECT 1
                      FROM source_import_batches b
                      JOIN active_section_registries ar
                        ON ar.generation_id=manifest.generation_id
                      JOIN section_registry_versions rv
                        ON rv.registry_version_id=ar.registry_version_id
                      JOIN projection_epochs pe
                        ON pe.projection_epoch_id=manifest.projection_epoch_id
                      JOIN shadow_baselines sb
                        ON sb.shadow_baseline_id=manifest.shadow_baseline_id
                      JOIN projection_reconciliation_runs rr
                        ON rr.reconciliation_run_id=
                           manifest.approval_reconciliation_run_id
                     WHERE b.import_batch_id=manifest.source_import_batch_id
                       AND b.import_run_id=manifest.source_import_run_id
                       AND b.generation_id=manifest.generation_id
                       AND ar.registry_version_id=manifest.registry_version_id
                       AND rv.generation_id=manifest.generation_id
                       AND rv.import_run_id=manifest.source_import_run_id
                       AND rv.contract_binding_id=manifest.honest_binding_id
                       AND pe.generation_id=manifest.generation_id
                       AND sb.generation_id=manifest.generation_id
                       AND rr.generation_id=manifest.generation_id
                       AND rr.projection_epoch_id=manifest.projection_epoch_id
                       AND rr.candidate_id=manifest.candidate_id
                ) THEN
                    RAISE EXCEPTION
                        'candidate authority changed after approval';
                END IF;
            END IF;
            RETURN NEW;
        END; $$;
        """
    )


def upgrade() -> None:
    _assert_legacy_readiness_empty()
    _upgrade_manifest_contract()
    _drop_legacy_readiness_schema()
    _create_worker_readiness_report()
    _upgrade_postgresql_candidate_activation_guard()


def _sqlite_create_legacy_readiness_schema() -> None:
    """Recreate the empty 0033-era readiness shape for SQLite round-trip tests."""
    op.create_table(
        "projection_worker_readiness",
        sa.Column("readiness_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("projection_epoch_id", sa.Uuid(), nullable=False),
        sa.Column("reconciliation_run_id", sa.Uuid(), nullable=False),
        sa.Column("worker_identity", sa.String(256), nullable=False),
        sa.Column("worker_release", sa.String(128), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("readiness_sha256", sa.String(64), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("probe_inventory_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("readiness_id", name="pk_projection_worker_readiness"),
        sa.UniqueConstraint("candidate_id", name="uq_projection_worker_readiness_candidate_id"),
        sa.ForeignKeyConstraint(["candidate_id"], ["release_candidates.candidate_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["projection_epoch_id"], ["projection_epochs.projection_epoch_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reconciliation_run_id"], ["projection_reconciliation_runs.reconciliation_run_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("length(trim(worker_identity)) > 0", name="ck_projection_worker_readiness_worker_identity_nonblank"),
        sa.CheckConstraint("length(trim(worker_release)) > 0", name="ck_projection_worker_readiness_worker_release_nonblank"),
        sa.CheckConstraint("length(readiness_sha256) = 64", name="ck_projection_worker_readiness_readiness_hash_length"),
    )
    op.create_table(
        "worker_probe_inventories",
        sa.Column("inventory_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("projection_epoch_id", sa.Uuid(), nullable=False),
        sa.Column("inventory_version", sa.BigInteger(), nullable=False),
        sa.Column("required_probe_count", sa.BigInteger(), nullable=False),
        sa.Column("inventory_sha256", sa.String(64), nullable=False),
        sa.Column("inventory_contract_version", sa.String(64), nullable=False),
        sa.Column("sealed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("inventory_id", name="pk_worker_probe_inventories"),
        sa.UniqueConstraint("candidate_id", name="uq_worker_probe_inventories_candidate_id"),
        sa.UniqueConstraint("candidate_id", "projection_epoch_id", name="uq_worker_probe_inventory_candidate_epoch"),
        sa.ForeignKeyConstraint(["candidate_id"], ["release_candidates.candidate_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["projection_epoch_id"], ["projection_epochs.projection_epoch_id"], ondelete="RESTRICT"),
    )
    with op.batch_alter_table("projection_worker_readiness") as batch:
        batch.create_foreign_key(
            "fk_projection_worker_readiness_probe_inventory",
            "worker_probe_inventories",
            ["probe_inventory_id"],
            ["inventory_id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "ix_projection_worker_readiness_probe_inventory",
        "projection_worker_readiness",
        ["probe_inventory_id"],
    )
    op.create_table(
        "worker_probe_requirements",
        sa.Column("requirement_id", sa.Uuid(), nullable=False),
        sa.Column("inventory_id", sa.Uuid(), nullable=False),
        sa.Column("probe_kind", sa.String(64), nullable=False),
        sa.Column("ordinal", sa.BigInteger(), nullable=False),
        sa.Column("probe_contract_version", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("requirement_id", name="pk_worker_probe_requirements"),
        sa.ForeignKeyConstraint(["inventory_id"], ["worker_probe_inventories.inventory_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("inventory_id", "probe_kind", name="uq_worker_probe_requirement_kind"),
        sa.UniqueConstraint("inventory_id", "ordinal", name="uq_worker_probe_requirement_ordinal"),
        sa.UniqueConstraint("requirement_id", "inventory_id", "probe_kind", name="uq_worker_probe_requirement_exact"),
    )
    op.create_table(
        "worker_probe_evidence",
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("readiness_id", sa.Uuid(), nullable=False),
        sa.Column("requirement_id", sa.Uuid(), nullable=False),
        sa.Column("inventory_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("projection_epoch_id", sa.Uuid(), nullable=False),
        sa.Column("probe_kind", sa.String(64), nullable=False),
        sa.Column("execution_identity", sa.String(256), nullable=False),
        sa.Column("worker_identity", sa.String(256), nullable=False),
        sa.Column("deployed_artifact_sha256", sa.String(64), nullable=False),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_artifact_identity", sa.String(512), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("evidence_id", name="pk_worker_probe_evidence"),
        sa.ForeignKeyConstraint(["readiness_id"], ["projection_worker_readiness.readiness_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requirement_id"], ["worker_probe_requirements.requirement_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["inventory_id"], ["worker_probe_inventories.inventory_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["candidate_id"], ["release_candidates.candidate_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["projection_epoch_id"], ["projection_epochs.projection_epoch_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("requirement_id", name="uq_worker_probe_evidence_requirement_id"),
        sa.UniqueConstraint("readiness_id", "probe_kind", name="uq_worker_probe_evidence_readiness_kind"),
    )
    op.create_index(
        "ix_worker_probe_evidence_candidate_epoch",
        "worker_probe_evidence",
        ["candidate_id", "projection_epoch_id"],
    )
    op.create_table(
        "worker_readiness_completions",
        sa.Column("completion_id", sa.Uuid(), nullable=False),
        sa.Column("readiness_id", sa.Uuid(), nullable=False),
        sa.Column("inventory_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("projection_epoch_id", sa.Uuid(), nullable=False),
        sa.Column("completion_state", sa.String(16), nullable=False),
        sa.Column("required_probe_count", sa.BigInteger(), nullable=False),
        sa.Column("passed_probe_count", sa.BigInteger(), nullable=False),
        sa.Column("completion_sha256", sa.String(64), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("completion_id", name="pk_worker_readiness_completions"),
        sa.ForeignKeyConstraint(["readiness_id"], ["projection_worker_readiness.readiness_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["inventory_id"], ["worker_probe_inventories.inventory_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["candidate_id"], ["release_candidates.candidate_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["projection_epoch_id"], ["projection_epochs.projection_epoch_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("readiness_id", name="uq_worker_readiness_completions_readiness_id"),
        sa.UniqueConstraint("inventory_id", name="uq_worker_readiness_completions_inventory_id"),
        sa.UniqueConstraint("candidate_id", name="uq_worker_readiness_completions_candidate_id"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        raise RuntimeError(
            "refusing PostgreSQL downgrade to the known-broken pre-CC5 schema; "
            "restore from a pre-repair backup instead"
        )
    if not op.get_context().as_sql:
        count = int(
            bind.execute(
                sa.text("SELECT count(*) FROM projection_worker_readiness")
            ).scalar_one()
        )
        if count:
            raise RuntimeError(
                "refusing lossy CC5 repair downgrade with worker readiness rows"
            )
        v3 = int(
            bind.execute(
                sa.text(
                    "SELECT "
                    "(SELECT count(*) FROM release_candidate_manifests WHERE manifest_version=3) + "
                    "(SELECT count(*) FROM cutover_approval_manifest_bindings WHERE manifest_version=3) + "
                    "(SELECT count(*) FROM candidate_manifest_revalidations WHERE manifest_version=3)"
                )
            ).scalar_one()
        )
        if v3:
            raise RuntimeError(
                "refusing lossy CC5 repair downgrade with manifest-v3 authority"
            )

    op.drop_table("projection_worker_readiness")
    _sqlite_create_legacy_readiness_schema()

    with op.batch_alter_table("candidate_manifest_revalidations") as batch:
        batch.drop_constraint(
            "ck_candidate_manifest_revalidations_manifest_version_supported",
            type_="check",
        )
        batch.drop_constraint(
            "ck_candidate_manifest_revalidations_component_hash_lengths",
            type_="check",
        )
        batch.alter_column(
            "observed_readiness_inventory_sha256",
            existing_type=sa.String(64),
            nullable=False,
        )
        batch.alter_column(
            "observed_readiness_completion_sha256",
            existing_type=sa.String(64),
            nullable=False,
        )
        batch.create_check_constraint(
            "manifest_version_two",
            "manifest_version = 2",
        )
        batch.create_check_constraint(
            "component_hash_lengths",
            "length(observed_mapping_membership_sha256) = 64 AND "
            "length(observed_import_completion_sha256) = 64 AND "
            "length(observed_typed_import_linkage_sha256) = 64 AND "
            "length(observed_reconciliation_evidence_sha256) = 64 AND "
            "length(observed_readiness_inventory_sha256) = 64 AND "
            "length(observed_readiness_completion_sha256) = 64",
        )

    with op.batch_alter_table("cutover_approval_manifest_bindings") as batch:
        batch.drop_constraint(
            "ck_cutover_approval_manifest_bindings_version_supported",
            type_="check",
        )
        batch.create_check_constraint(
            "manifest_version_two",
            "manifest_version = 2",
        )

    with op.batch_alter_table("release_candidate_manifests") as batch:
        batch.drop_constraint(
            "ck_release_candidate_manifests_manifest_version_supported",
            type_="check",
        )
        batch.drop_constraint(
            "ck_release_candidate_manifests_component_hash_lengths",
            type_="check",
        )
        batch.drop_constraint(
            "fk_rc_manifests_approval_reconciliation_run_id",
            type_="foreignkey",
        )
        batch.alter_column(
            "readiness_inventory_sha256",
            existing_type=sa.String(64),
            nullable=False,
        )
        batch.alter_column(
            "readiness_completion_sha256",
            existing_type=sa.String(64),
            nullable=False,
        )
        batch.drop_column("approval_reconciliation_run_id")
        batch.create_check_constraint(
            "manifest_version_two",
            "manifest_version = 2",
        )
        batch.create_check_constraint(
            "component_hash_lengths",
            "length(mapping_membership_sha256) = 64 AND "
            "length(import_completion_sha256) = 64 AND "
            "length(typed_import_linkage_sha256) = 64 AND "
            "length(reconciliation_evidence_sha256) = 64 AND "
            "length(readiness_inventory_sha256) = 64 AND "
            "length(readiness_completion_sha256) = 64",
        )
