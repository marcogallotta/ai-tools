"""Freeze approved release-candidate authority in a versioned manifest.

Revision ID: 0022_candidate_state_manifest
Revises: 0021_writer_fence_artifact_identity
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0022_candidate_state_manifest"
down_revision = "0021_writer_fence_artifact_identity"
branch_labels = None
depends_on = None


_COMPONENT_COLUMNS = (
    "mapping_membership_sha256",
    "import_completion_sha256",
    "typed_import_linkage_sha256",
    "reconciliation_evidence_sha256",
    "readiness_inventory_sha256",
    "readiness_completion_sha256",
)


def _create_supporting_constraints() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("cutover_approvals") as batch:
            batch.create_unique_constraint(
                "uq_cutover_approval_candidate_identity",
                ["approval_id", "candidate_id"],
            )
    else:
        op.create_unique_constraint(
            "uq_cutover_approval_candidate_identity",
            "cutover_approvals",
            ["approval_id", "candidate_id"],
        )


def _create_tables() -> None:
    op.create_table(
        "release_candidate_manifests",
        sa.Column("manifest_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_version", sa.BigInteger(), nullable=False),
        sa.Column("canonical_fingerprint", sa.String(64), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("source_import_batch_id", sa.Uuid(), nullable=False),
        sa.Column("source_import_run_id", sa.Uuid(), nullable=False),
        sa.Column("shadow_baseline_id", sa.Uuid(), nullable=False),
        sa.Column("projection_epoch_id", sa.Uuid(), nullable=False),
        sa.Column("registry_version_id", sa.Uuid(), nullable=False),
        sa.Column("honest_binding_id", sa.Uuid(), nullable=False),
        *(
            sa.Column(column_name, sa.String(64), nullable=False)
            for column_name in _COMPONENT_COLUMNS
        ),
        sa.Column("builder_contract_version", sa.String(64), nullable=False),
        sa.Column("built_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "manifest_version = 2",
            name="ck_release_candidate_manifests_manifest_version_two",
        ),
        sa.CheckConstraint(
            "length(canonical_fingerprint) = 64",
            name="ck_release_candidate_manifests_fingerprint_hash_length",
        ),
        sa.CheckConstraint(
            " AND ".join(
                f"length({column_name}) = 64" for column_name in _COMPONENT_COLUMNS
            ),
            name="ck_release_candidate_manifests_component_hash_lengths",
        ),
        sa.CheckConstraint(
            "length(trim(builder_contract_version)) > 0",
            name="ck_release_candidate_manifests_builder_contract_nonblank",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["release_candidates.candidate_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["authority_generations.generation_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_import_batch_id"],
            ["source_import_batches.import_batch_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_import_run_id"],
            ["stage_a_import_runs.import_run_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["shadow_baseline_id"],
            ["shadow_baselines.shadow_baseline_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["projection_epoch_id"],
            ["projection_epochs.projection_epoch_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["registry_version_id"],
            ["section_registry_versions.registry_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["honest_binding_id"],
            ["honest_contract_bindings.binding_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "manifest_id", name="pk_release_candidate_manifests"
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "manifest_version",
            name="uq_candidate_manifest_version",
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "canonical_fingerprint",
            name="uq_candidate_manifest_fingerprint",
        ),
        sa.UniqueConstraint(
            "manifest_id",
            "candidate_id",
            "manifest_version",
            "canonical_fingerprint",
            name="uq_candidate_manifest_exact_identity",
        ),
    )
    op.create_table(
        "cutover_approval_manifest_bindings",
        sa.Column("binding_id", sa.Uuid(), nullable=False),
        sa.Column("approval_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_version", sa.BigInteger(), nullable=False),
        sa.Column("canonical_fingerprint", sa.String(64), nullable=False),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "manifest_version = 2",
            name="ck_cutover_approval_manifest_bindings_manifest_version_two",
        ),
        sa.CheckConstraint(
            "length(canonical_fingerprint) = 64",
            name="ck_cutover_approval_manifest_bindings_fingerprint_hash_length",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id", "candidate_id"],
            ["cutover_approvals.approval_id", "cutover_approvals.candidate_id"],
            name="fk_approval_manifest_binding_exact_approval",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id", "candidate_id", "manifest_version", "canonical_fingerprint"],
            [
                "release_candidate_manifests.manifest_id",
                "release_candidate_manifests.candidate_id",
                "release_candidate_manifests.manifest_version",
                "release_candidate_manifests.canonical_fingerprint",
            ],
            name="fk_approval_manifest_binding_exact_manifest",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "binding_id", name="pk_cutover_approval_manifest_bindings"
        ),
        sa.UniqueConstraint(
            "approval_id", name="uq_cutover_approval_manifest_bindings_approval_id"
        ),
        sa.UniqueConstraint(
            "candidate_id", name="uq_cutover_approval_manifest_bindings_candidate_id"
        ),
        sa.UniqueConstraint(
            "manifest_id", name="uq_cutover_approval_manifest_bindings_manifest_id"
        ),
    )
    op.create_table(
        "candidate_manifest_revalidations",
        sa.Column("revalidation_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_id", sa.Uuid(), nullable=False),
        sa.Column("manifest_version", sa.BigInteger(), nullable=False),
        sa.Column("approved_fingerprint", sa.String(64), nullable=False),
        sa.Column("observed_fingerprint", sa.String(64), nullable=False),
        *(
            sa.Column(f"observed_{column_name}", sa.String(64), nullable=False)
            for column_name in _COMPONENT_COLUMNS
        ),
        sa.Column("result", sa.String(16), nullable=False),
        sa.Column("revalidated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "manifest_version = 2",
            name="ck_candidate_manifest_revalidations_manifest_version_two",
        ),
        sa.CheckConstraint(
            "length(approved_fingerprint) = 64 AND "
            "length(observed_fingerprint) = 64",
            name="ck_candidate_manifest_revalidations_fingerprint_hash_lengths",
        ),
        sa.CheckConstraint(
            " AND ".join(
                f"length(observed_{column_name}) = 64"
                for column_name in _COMPONENT_COLUMNS
            ),
            name="ck_candidate_manifest_revalidations_observed_component_hash_lengths",
        ),
        sa.CheckConstraint(
            "result IN ('matched','stale')",
            name="ck_candidate_manifest_revalidations_result_allowed",
        ),
        sa.CheckConstraint(
            "(result = 'matched' AND approved_fingerprint = observed_fingerprint) OR "
            "(result = 'stale' AND approved_fingerprint <> observed_fingerprint)",
            name="ck_candidate_manifest_revalidations_result_matches_fingerprint",
        ),
        sa.ForeignKeyConstraint(
            ["manifest_id", "candidate_id", "manifest_version", "approved_fingerprint"],
            [
                "release_candidate_manifests.manifest_id",
                "release_candidate_manifests.candidate_id",
                "release_candidate_manifests.manifest_version",
                "release_candidate_manifests.canonical_fingerprint",
            ],
            name="fk_candidate_manifest_revalidation_exact_manifest",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "revalidation_id", name="pk_candidate_manifest_revalidations"
        ),
        sa.UniqueConstraint(
            "candidate_id",
            "observed_fingerprint",
            "revalidated_at",
            name="uq_candidate_revalidation_observation_time",
        ),
    )
    op.create_index(
        "ix_candidate_manifest_revalidations_latest",
        "candidate_manifest_revalidations",
        ["candidate_id", "revalidated_at"],
    )


def _install_postgresql_guards() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_candidate_manifest_identity()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM release_candidates c
                JOIN source_import_batches b
                  ON b.import_batch_id=NEW.source_import_batch_id
                JOIN shadow_baselines sb
                  ON sb.shadow_baseline_id=NEW.shadow_baseline_id
                JOIN projection_epochs pe
                  ON pe.projection_epoch_id=NEW.projection_epoch_id
                JOIN section_registry_versions rv
                  ON rv.registry_version_id=NEW.registry_version_id
                WHERE c.candidate_id=NEW.candidate_id
                  AND c.generation_id=NEW.generation_id
                  AND c.source_import_batch_id=NEW.source_import_batch_id
                  AND c.shadow_baseline_id=NEW.shadow_baseline_id
                  AND c.projection_epoch_id=NEW.projection_epoch_id
                  AND b.import_run_id=NEW.source_import_run_id
                  AND b.generation_id=NEW.generation_id
                  AND sb.generation_id=NEW.generation_id
                  AND pe.generation_id=NEW.generation_id
                  AND rv.generation_id=NEW.generation_id
                  AND rv.import_run_id=NEW.source_import_run_id
                  AND rv.contract_binding_id=NEW.honest_binding_id
            ) THEN
                RAISE EXCEPTION
                    'candidate manifest authority identity is internally inconsistent';
            END IF;
            RETURN NEW;
        END; $$;
        """
    )
    op.execute(
        "CREATE TRIGGER release_candidate_manifests_validate_identity "
        "BEFORE INSERT ON release_candidate_manifests FOR EACH ROW "
        "EXECUTE FUNCTION dish_validate_candidate_manifest_identity()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_candidate_manifest_revalidation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE manifest release_candidate_manifests%ROWTYPE;
        BEGIN
            SELECT m.* INTO manifest
              FROM release_candidate_manifests m
             WHERE m.manifest_id=NEW.manifest_id
               AND m.candidate_id=NEW.candidate_id
               AND m.manifest_version=NEW.manifest_version
               AND m.canonical_fingerprint=NEW.approved_fingerprint;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'candidate manifest revalidation lacks exact manifest';
            END IF;
            IF NEW.result='matched' AND (
                NEW.observed_mapping_membership_sha256
                    <> manifest.mapping_membership_sha256
                OR NEW.observed_import_completion_sha256
                    <> manifest.import_completion_sha256
                OR NEW.observed_typed_import_linkage_sha256
                    <> manifest.typed_import_linkage_sha256
                OR NEW.observed_reconciliation_evidence_sha256
                    <> manifest.reconciliation_evidence_sha256
                OR NEW.observed_readiness_inventory_sha256
                    <> manifest.readiness_inventory_sha256
                OR NEW.observed_readiness_completion_sha256
                    <> manifest.readiness_completion_sha256
            ) THEN
                RAISE EXCEPTION
                    'matched candidate revalidation component digest mismatch';
            END IF;
            RETURN NEW;
        END; $$;
        """
    )
    op.execute(
        "CREATE TRIGGER candidate_manifest_revalidations_validate "
        "BEFORE INSERT ON candidate_manifest_revalidations FOR EACH ROW "
        "EXECUTE FUNCTION dish_validate_candidate_manifest_revalidation()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_reject_immutable_candidate_manifest_evidence()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'immutable candidate manifest evidence: %', TG_TABLE_NAME;
        END; $$;
        """
    )
    for table in (
        "release_candidate_manifests",
        "cutover_approval_manifest_bindings",
        "candidate_manifest_revalidations",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION "
            "dish_reject_immutable_candidate_manifest_evidence()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION "
            "dish_reject_immutable_candidate_manifest_evidence()"
        )
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
                SELECT b.bound_at INTO approval_bound_at
                  FROM cutover_approval_manifest_bindings b
                 WHERE b.candidate_id=NEW.candidate_id
                   AND b.manifest_id=manifest.manifest_id;
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
                     WHERE b.import_batch_id=manifest.source_import_batch_id
                       AND b.import_run_id=manifest.source_import_run_id
                       AND b.generation_id=manifest.generation_id
                       AND ar.registry_version_id=manifest.registry_version_id
                       AND rv.generation_id=manifest.generation_id
                       AND rv.import_run_id=manifest.source_import_run_id
                       AND rv.contract_binding_id=manifest.honest_binding_id
                       AND pe.generation_id=manifest.generation_id
                       AND sb.generation_id=manifest.generation_id
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
    bind = op.get_bind()
    frozen = 0
    if not op.get_context().as_sql:
        frozen = int(
            bind.execute(
                sa.text(
                    "SELECT count(*) FROM release_candidates "
                    "WHERE status IN ('approved','activated')"
                )
            ).scalar_one()
        )
    if frozen:
        raise RuntimeError(
            "cannot install candidate manifest freeze with already-approved "
            "predecessor candidates"
        )
    _create_supporting_constraints()
    _create_tables()
    if bind.dialect.name == "postgresql":
        _install_postgresql_guards()


def downgrade() -> None:
    bind = op.get_bind()
    count = int(
        bind.execute(
            sa.text(
                "SELECT (SELECT count(*) FROM release_candidate_manifests) + "
                "(SELECT count(*) FROM cutover_approval_manifest_bindings) + "
                "(SELECT count(*) FROM candidate_manifest_revalidations)"
            )
        ).scalar_one()
    )
    if count:
        raise RuntimeError(
            "refusing lossy downgrade: candidate manifest authority exists"
        )
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_validate_release_candidate_transition()
            RETURNS trigger LANGUAGE plpgsql AS $$
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
                RETURN NEW;
            END; $$;
            """
        )
        op.execute(
            "DROP FUNCTION IF EXISTS "
            "dish_validate_candidate_manifest_identity() CASCADE"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS "
            "dish_validate_candidate_manifest_revalidation() CASCADE"
        )
        op.execute(
            "DROP FUNCTION IF EXISTS "
            "dish_reject_immutable_candidate_manifest_evidence() CASCADE"
        )
    op.drop_index(
        "ix_candidate_manifest_revalidations_latest",
        table_name="candidate_manifest_revalidations",
    )
    op.drop_table("candidate_manifest_revalidations")
    op.drop_table("cutover_approval_manifest_bindings")
    op.drop_table("release_candidate_manifests")
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("cutover_approvals") as batch:
            batch.drop_constraint(
                "uq_cutover_approval_candidate_identity", type_="unique"
            )
    else:
        op.drop_constraint(
            "uq_cutover_approval_candidate_identity",
            "cutover_approvals",
            type_="unique",
        )
