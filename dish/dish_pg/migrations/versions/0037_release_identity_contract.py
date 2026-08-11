"""Bind release evidence and irreversible cutover to exact release identity.

Revision ID: 0037_release_identity_contract
Revises: 0036_exact_operation_run_revocations
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0037_release_identity_contract"
down_revision = "0036_exact_operation_run_revocations"
branch_labels = None
depends_on = None


def _sqlite_suspend_triggers_referencing(table_name: str) -> list[str]:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return []
    rows = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='trigger' AND sql IS NOT NULL "
            "AND (tbl_name=:table_name OR instr(sql, :table_name) > 0)"
        ),
        {"table_name": table_name},
    ).all()
    definitions: list[str] = []
    for name, sql in rows:
        escaped = str(name).replace('"', '""')
        op.execute(f'DROP TRIGGER IF EXISTS "{escaped}"')
        definitions.append(str(sql))
    return definitions


def _sqlite_restore_triggers(definitions: list[str]) -> None:
    for definition in definitions:
        op.execute(definition)


def _replace_manifest_constraints(*, forward: bool) -> None:
    supported = "manifest_version IN (2, 3, 4)" if forward else "manifest_version IN (2, 3)"
    forward_versions = "manifest_version IN (3, 4)" if forward else "manifest_version = 3"

    with op.batch_alter_table("release_candidate_manifests") as batch:
        batch.drop_constraint(
            op.f("ck_release_candidate_manifests_manifest_version_supported"),
            type_="check",
        )
        batch.drop_constraint(
            op.f("ck_release_candidate_manifests_component_hash_lengths"),
            type_="check",
        )
        batch.create_check_constraint("manifest_version_supported", supported)
        batch.create_check_constraint(
            "component_hash_lengths",
            "length(mapping_membership_sha256) = 64 AND "
            "length(import_completion_sha256) = 64 AND "
            "length(typed_import_linkage_sha256) = 64 AND "
            "length(reconciliation_evidence_sha256) = 64 AND "
            "((manifest_version = 2 AND approval_reconciliation_run_id IS NULL "
            "AND readiness_inventory_sha256 IS NOT NULL "
            "AND length(readiness_inventory_sha256) = 64 "
            "AND readiness_completion_sha256 IS NOT NULL "
            "AND length(readiness_completion_sha256) = 64) OR "
            f"({forward_versions} AND approval_reconciliation_run_id IS NOT NULL "
            "AND readiness_inventory_sha256 IS NULL "
            "AND readiness_completion_sha256 IS NULL))",
        )

    with op.batch_alter_table("cutover_approval_manifest_bindings") as batch:
        binding_check = op.f(
            "ck_cutover_approval_manifest_bindings_version_supported"
            if forward
            else "ck_cutover_approval_manifest_bindings_manifest_version_supported"
        )
        batch.drop_constraint(binding_check, type_="check")
        batch.create_check_constraint(
            "manifest_version_supported" if forward else "version_supported",
            supported,
        )

    with op.batch_alter_table("candidate_manifest_revalidations") as batch:
        batch.drop_constraint(
            op.f("ck_candidate_manifest_revalidations_manifest_version_supported"),
            type_="check",
        )
        batch.drop_constraint(
            op.f("ck_candidate_manifest_revalidations_observed_component_hash_lengths"),
            type_="check",
        )
        batch.create_check_constraint("manifest_version_supported", supported)
        batch.create_check_constraint(
            "observed_component_hash_lengths",
            "length(observed_mapping_membership_sha256) = 64 AND "
            "length(observed_import_completion_sha256) = 64 AND "
            "length(observed_typed_import_linkage_sha256) = 64 AND "
            "length(observed_reconciliation_evidence_sha256) = 64 AND "
            "((manifest_version = 2 "
            "AND observed_readiness_inventory_sha256 IS NOT NULL "
            "AND length(observed_readiness_inventory_sha256) = 64 "
            "AND observed_readiness_completion_sha256 IS NOT NULL "
            "AND length(observed_readiness_completion_sha256) = 64) OR "
            f"({forward_versions} "
            "AND observed_readiness_inventory_sha256 IS NULL "
            "AND observed_readiness_completion_sha256 IS NULL))",
        )


def _upgrade_release_identity_columns() -> None:
    candidate_triggers = _sqlite_suspend_triggers_referencing("release_candidates")
    with op.batch_alter_table("release_candidates") as batch:
        batch.add_column(sa.Column("identity_contract_version", sa.String(32), nullable=True))
        batch.add_column(sa.Column("source_manifest_sha256", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column("rehearsal_environment_identity", sa.String(128), nullable=True)
        )
        batch.add_column(sa.Column("registry_version_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("honest_binding_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_relcand_registry",
            "section_registry_versions",
            ["registry_version_id"],
            ["registry_version_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_relcand_honest",
            "honest_contract_bindings",
            ["honest_binding_id"],
            ["binding_id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "identity_contract_complete",
            "(identity_contract_version IS NULL AND source_manifest_sha256 IS NULL "
            "AND rehearsal_environment_identity IS NULL AND registry_version_id IS NULL "
            "AND honest_binding_id IS NULL) OR "
            "(identity_contract_version = 'release-identity-v1' "
            "AND source_manifest_sha256 IS NOT NULL AND length(source_manifest_sha256) = 64 "
            "AND rehearsal_environment_identity IS NOT NULL "
            "AND registry_version_id IS NOT NULL AND honest_binding_id IS NOT NULL)",
        )
    _sqlite_restore_triggers(candidate_triggers)

    with op.batch_alter_table("authority_activations") as batch:
        batch.add_column(sa.Column("registry_version_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("honest_binding_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_authact_registry",
            "section_registry_versions",
            ["registry_version_id"],
            ["registry_version_id"],
            ondelete="RESTRICT",
        )
        batch.create_foreign_key(
            "fk_authact_honest",
            "honest_contract_bindings",
            ["honest_binding_id"],
            ["binding_id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "release_contract_identity_pair",
            "(registry_version_id IS NULL AND honest_binding_id IS NULL) OR "
            "(registry_version_id IS NOT NULL AND honest_binding_id IS NOT NULL)",
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
            active_registry_version_id uuid;
            active_honest_binding_id uuid;
            active_schema_head text;
            active_dish_release text;
            active_honest_release text;
            active_protocol_release text;
        BEGIN
            IF OLD.candidate_id <> NEW.candidate_id
               OR OLD.generation_id <> NEW.generation_id
               OR OLD.source_import_batch_id <> NEW.source_import_batch_id
               OR OLD.shadow_baseline_id <> NEW.shadow_baseline_id
               OR OLD.projection_epoch_id <> NEW.projection_epoch_id
               OR OLD.identity_contract_version IS DISTINCT FROM NEW.identity_contract_version
               OR OLD.source_manifest_sha256 IS DISTINCT FROM NEW.source_manifest_sha256
               OR OLD.rehearsal_environment_identity IS DISTINCT FROM NEW.rehearsal_environment_identity
               OR OLD.registry_version_id IS DISTINCT FROM NEW.registry_version_id
               OR OLD.honest_binding_id IS DISTINCT FROM NEW.honest_binding_id
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

            IF (OLD.status = 'assembling' AND NEW.status = 'validated')
               OR (OLD.status = 'validated' AND NEW.status = 'approved') THEN
                -- Release-boundary lock ordering is generation first, then the
                -- mutable active-registry pointer.  The ActiveSectionRegistry
                -- row lock conflicts with both repository and direct-SQL pointer
                -- updates until this candidate transition transaction ends.
                SELECT g.schema_head, g.dish_release
                  INTO active_schema_head, active_dish_release
                  FROM authority_generations g
                 WHERE g.generation_id=NEW.generation_id
                   AND g.status='active'
                   FOR UPDATE;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'candidate release transition requires active generation';
                END IF;

                SELECT ar.registry_version_id,
                       rv.contract_binding_id,
                       hb.honest_release,
                       hb.protocol_release
                  INTO active_registry_version_id,
                       active_honest_binding_id,
                       active_honest_release,
                       active_protocol_release
                  FROM active_section_registries ar
                  JOIN section_registry_versions rv
                    ON rv.registry_version_id=ar.registry_version_id
                  JOIN honest_contract_bindings hb
                    ON hb.binding_id=rv.contract_binding_id
                 WHERE ar.generation_id=NEW.generation_id
                   AND rv.generation_id=NEW.generation_id
                   AND hb.binding_kind='release'
                   AND hb.dish_release=active_dish_release
                   FOR UPDATE OF ar;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'candidate release transition requires exact active registry and Honest binding';
                END IF;

                IF NEW.identity_contract_version IS DISTINCT FROM 'release-identity-v1'
                   OR NEW.registry_version_id IS DISTINCT FROM active_registry_version_id
                   OR NEW.honest_binding_id IS DISTINCT FROM active_honest_binding_id
                   OR NEW.schema_head IS DISTINCT FROM active_schema_head
                   OR NEW.dish_release IS DISTINCT FROM active_dish_release
                   OR NEW.honest_release IS DISTINCT FROM active_honest_release
                   OR NEW.protocol_release IS DISTINCT FROM active_protocol_release THEN
                    RAISE EXCEPTION
                        'candidate release identity does not match exact active authority';
                END IF;
            END IF;

            IF OLD.status = 'validated' AND NEW.status = 'approved' THEN
                SELECT m.* INTO manifest
                  FROM release_candidate_manifests m
                 WHERE m.candidate_id=NEW.candidate_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'candidate approval lacks authority manifest';
                END IF;
                IF manifest.manifest_version <> 4
                   OR manifest.approval_reconciliation_run_id IS NULL THEN
                    RAISE EXCEPTION
                        'candidate approval requires forward manifest v4';
                END IF;
                IF manifest.generation_id IS DISTINCT FROM NEW.generation_id
                   OR manifest.registry_version_id IS DISTINCT FROM NEW.registry_version_id
                   OR manifest.honest_binding_id IS DISTINCT FROM NEW.honest_binding_id
                   OR manifest.registry_version_id IS DISTINCT FROM active_registry_version_id
                   OR manifest.honest_binding_id IS DISTINCT FROM active_honest_binding_id THEN
                    RAISE EXCEPTION
                        'candidate approval manifest release identity mismatch';
                END IF;
                SELECT b.bound_at INTO approval_bound_at
                  FROM cutover_approval_manifest_bindings b
                  JOIN cutover_approvals a
                    ON a.approval_id=b.approval_id
                   AND a.candidate_id=b.candidate_id
                 WHERE b.candidate_id=NEW.candidate_id
                   AND b.manifest_id=manifest.manifest_id
                   AND b.manifest_version=manifest.manifest_version
                   AND b.canonical_fingerprint=manifest.canonical_fingerprint;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'candidate approval lacks exact authority manifest binding';
                END IF;
            END IF;

            IF OLD.status = 'approved' AND NEW.status = 'activated' THEN
                SELECT m.* INTO manifest
                  FROM release_candidate_manifests m
                 WHERE m.candidate_id=NEW.candidate_id;
                IF NOT FOUND THEN
                    RAISE EXCEPTION
                        'candidate activation lacks authority manifest';
                END IF;
                IF manifest.manifest_version <> 4
                   OR manifest.approval_reconciliation_run_id IS NULL THEN
                    RAISE EXCEPTION
                        'candidate activation requires forward manifest v4';
                END IF;
                SELECT b.bound_at INTO approval_bound_at
                  FROM cutover_approval_manifest_bindings b
                 WHERE b.candidate_id=NEW.candidate_id
                   AND b.manifest_id=manifest.manifest_id
                   AND b.manifest_version=4;
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
                IF NEW.identity_contract_version <> 'release-identity-v1'
                   OR NEW.registry_version_id <> manifest.registry_version_id
                   OR NEW.honest_binding_id <> manifest.honest_binding_id
                   OR NOT EXISTS (
                    SELECT 1
                      FROM source_import_batches b
                      JOIN authority_generations g
                        ON g.generation_id=manifest.generation_id
                      JOIN active_section_registries ar
                        ON ar.generation_id=manifest.generation_id
                      JOIN section_registry_versions rv
                        ON rv.registry_version_id=ar.registry_version_id
                      JOIN honest_contract_bindings hb
                        ON hb.binding_id=rv.contract_binding_id
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
                       AND g.status='active'
                       AND ar.registry_version_id=manifest.registry_version_id
                       AND rv.generation_id=manifest.generation_id
                       AND rv.import_run_id=manifest.source_import_run_id
                       AND rv.contract_binding_id=manifest.honest_binding_id
                       AND hb.binding_id=manifest.honest_binding_id
                       AND hb.binding_kind='release'
                       AND hb.dish_release=g.dish_release
                       AND NEW.schema_head=g.schema_head
                       AND NEW.dish_release=g.dish_release
                       AND NEW.honest_release=hb.honest_release
                       AND NEW.protocol_release=hb.protocol_release
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
    _upgrade_release_identity_columns()
    _replace_manifest_constraints(forward=True)
    _upgrade_postgresql_candidate_activation_guard()


def _assert_downgrade_safe() -> None:
    if op.get_context().as_sql:
        return
    bind = op.get_bind()
    checks = (
        ("release_candidates", "identity_contract_version IS NOT NULL"),
        ("authority_activations", "registry_version_id IS NOT NULL OR honest_binding_id IS NOT NULL"),
        ("release_candidate_manifests", "manifest_version = 4"),
        ("cutover_approval_manifest_bindings", "manifest_version = 4"),
        ("candidate_manifest_revalidations", "manifest_version = 4"),
    )
    for table, predicate in checks:
        present = bind.execute(
            sa.text(f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1")
        ).scalar_one_or_none()
        if present is not None:
            raise RuntimeError(
                "0037 downgrade would erase exact release identity evidence; "
                "restore from a pre-0037 backup instead"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        raise RuntimeError(
            "refusing PostgreSQL downgrade to 0036 because it would remove exact release identity binding; "
            "restore from a pre-0037 backup instead"
        )
    _assert_downgrade_safe()
    _replace_manifest_constraints(forward=False)
    with op.batch_alter_table("authority_activations") as batch:
        batch.drop_constraint(
            op.f("ck_authority_activations_release_contract_identity_pair"),
            type_="check",
        )
        batch.drop_constraint(
            "fk_authact_honest", type_="foreignkey"
        )
        batch.drop_constraint(
            "fk_authact_registry", type_="foreignkey"
        )
        batch.drop_column("honest_binding_id")
        batch.drop_column("registry_version_id")
    candidate_triggers = _sqlite_suspend_triggers_referencing("release_candidates")
    with op.batch_alter_table("release_candidates") as batch:
        batch.drop_constraint(
            op.f("ck_release_candidates_identity_contract_complete"), type_="check"
        )
        batch.drop_constraint(
            "fk_relcand_honest", type_="foreignkey"
        )
        batch.drop_constraint(
            "fk_relcand_registry", type_="foreignkey"
        )
        batch.drop_column("honest_binding_id")
        batch.drop_column("registry_version_id")
        batch.drop_column("rehearsal_environment_identity")
        batch.drop_column("source_manifest_sha256")
        batch.drop_column("identity_contract_version")
    _sqlite_restore_triggers(candidate_triggers)
