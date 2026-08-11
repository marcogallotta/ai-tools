"""Bind Stage 6 cutover rehearsals to durable rehearsal identity and teardown.

Revision ID: 0038_cutover_rehearsal_identity
Revises: 0037_release_identity_contract
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0038_cutover_rehearsal_identity"
down_revision = "0037_release_identity_contract"
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

_CANDIDATE_0037 = """CREATE OR REPLACE FUNCTION dish_validate_release_candidate_transition()
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
        END; $$;"""
_CANDIDATE_0038 = """CREATE OR REPLACE FUNCTION dish_validate_release_candidate_transition()
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
            IF OLD.status = 'activated' AND NEW.status = 'aborted' THEN
                IF NOT EXISTS (
                    SELECT 1 FROM cutover_runs cr
                    JOIN rehearsal_runs rr ON rr.rehearsal_id=cr.rehearsal_id
                    WHERE cr.candidate_id=NEW.candidate_id
                      AND cr.state='rehearsal_torn_down'
                      AND cr.terminal_at=NEW.terminal_at
                      AND rr.candidate_id=NEW.candidate_id
                      AND rr.rehearsal_kind='cutover'
                ) OR EXISTS (
                    SELECT 1 FROM authority_activations a
                    WHERE a.generation_id=NEW.generation_id AND a.outcome='activated'
                ) OR NOT EXISTS (
                    SELECT 1 FROM authority_activations a
                    JOIN cutover_runs cr ON cr.rehearsal_id=a.rehearsal_id
                    WHERE cr.candidate_id=NEW.candidate_id
                      AND cr.state='rehearsal_torn_down'
                      AND a.generation_id=NEW.generation_id
                      AND a.outcome='aborted'
                ) THEN
                    RAISE EXCEPTION 'activated candidate may abort only after exact rehearsal teardown';
                END IF;
                RETURN NEW;
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
        END; $$;"""
_CUTOVER_0005 = """CREATE OR REPLACE FUNCTION dish_validate_cutover_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.cutover_run_id <> NEW.cutover_run_id OR OLD.candidate_id <> NEW.candidate_id OR OLD.started_at <> NEW.started_at THEN RAISE EXCEPTION 'cutover run identity is immutable'; END IF;
    IF NEW.state_revision <> OLD.state_revision + 1 THEN RAISE EXCEPTION 'cutover state revision must advance exactly once'; END IF;
    IF (OLD.state = 'prepared' AND NEW.state NOT IN ('fenced','aborted')) OR (OLD.state = 'fenced' AND NEW.state NOT IN ('activated','aborted')) OR (OLD.state = 'activated' AND NEW.state NOT IN ('rollback_burned','aborted')) OR (OLD.state = 'rollback_burned' AND NEW.state <> 'admission_open') OR (OLD.state = 'admission_open' AND NEW.state <> 'first_admission_verified') OR (OLD.state = 'first_admission_verified' AND NEW.state <> 'completed') OR OLD.state IN ('completed','aborted') THEN RAISE EXCEPTION 'illegal cutover state transition'; END IF;
    RETURN NEW;
END; $$;"""
_CUTOVER_0038 = """CREATE OR REPLACE FUNCTION dish_validate_cutover_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.cutover_run_id <> NEW.cutover_run_id OR OLD.candidate_id <> NEW.candidate_id OR OLD.rehearsal_id IS DISTINCT FROM NEW.rehearsal_id OR OLD.started_at <> NEW.started_at THEN RAISE EXCEPTION 'cutover run identity is immutable'; END IF;
    IF NEW.state_revision <> OLD.state_revision + 1 THEN RAISE EXCEPTION 'cutover state revision must advance exactly once'; END IF;
    IF NEW.state = 'rehearsal_torn_down' THEN
        IF OLD.state NOT IN ('rollback_burned','admission_open') OR OLD.rehearsal_id IS NULL OR NEW.terminal_at IS NULL
           OR NOT EXISTS (SELECT 1 FROM rehearsal_runs rr WHERE rr.rehearsal_id=OLD.rehearsal_id AND rr.candidate_id=OLD.candidate_id AND rr.rehearsal_kind='cutover' AND rr.status='running')
           OR EXISTS (SELECT 1 FROM first_request_reservations r WHERE r.cutover_run_id=OLD.cutover_run_id AND r.state <> 'cancelled')
        THEN RAISE EXCEPTION 'rehearsal teardown lacks exact unconsumed rehearsal authority'; END IF;
        RETURN NEW;
    END IF;
    IF (OLD.state = 'prepared' AND NEW.state NOT IN ('fenced','aborted')) OR (OLD.state = 'fenced' AND NEW.state NOT IN ('activated','aborted')) OR (OLD.state = 'activated' AND NEW.state NOT IN ('rollback_burned','aborted')) OR (OLD.state = 'rollback_burned' AND NEW.state <> 'admission_open') OR (OLD.state = 'admission_open' AND NEW.state <> 'first_admission_verified') OR (OLD.state = 'first_admission_verified' AND NEW.state <> 'completed') OR OLD.state IN ('completed','aborted','rehearsal_torn_down') THEN RAISE EXCEPTION 'illegal cutover state transition'; END IF;
    RETURN NEW;
END; $$;"""


def _schema_forward() -> None:
    with op.batch_alter_table("rehearsal_runs") as batch:
        batch.drop_constraint(op.f("ck_rehearsal_runs_kind_allowed"), type_="check")
        batch.create_check_constraint("kind_allowed", "rehearsal_kind IN ('full','activation','restore','fault_injection','cutover')")
        batch.create_unique_constraint("uq_rehearsal_candidate_identity", ["rehearsal_id", "candidate_id"])
    cutover_triggers = _sqlite_suspend_triggers_referencing("cutover_runs")
    with op.batch_alter_table("cutover_runs") as batch:
        batch.add_column(sa.Column("rehearsal_id", sa.Uuid(), nullable=True))
        batch.drop_constraint(op.f("ck_cutover_runs_state_allowed"), type_="check")
        batch.drop_constraint(op.f("ck_cutover_runs_terminal_time_consistent"), type_="check")
        batch.create_check_constraint("state_allowed", "state IN ('prepared','fenced','activated','rollback_burned','admission_open','first_admission_verified','completed','aborted','rehearsal_torn_down')")
        batch.create_check_constraint("terminal_time_consistent", "(state IN ('completed','aborted','rehearsal_torn_down') AND terminal_at IS NOT NULL) OR (state NOT IN ('completed','aborted','rehearsal_torn_down') AND terminal_at IS NULL)")
        batch.create_check_constraint("teardown_requires_rehearsal", "state <> 'rehearsal_torn_down' OR rehearsal_id IS NOT NULL")
        batch.create_unique_constraint("uq_cutover_runs_rehearsal_id", ["rehearsal_id"])
        batch.create_foreign_key("fk_cutover_run_rehearsal_candidate", "rehearsal_runs", ["rehearsal_id", "candidate_id"], ["rehearsal_id", "candidate_id"], ondelete="RESTRICT")
    _sqlite_restore_triggers(cutover_triggers)
    with op.batch_alter_table("authority_activations") as batch:
        batch.add_column(sa.Column("rehearsal_id", sa.Uuid(), nullable=True))
        batch.drop_constraint("uq_activation_generation_outcome", type_="unique")
        batch.drop_constraint(op.f("uq_authority_activations_projection_epoch"), type_="unique")
        batch.create_foreign_key("fk_authact_rehearsal", "rehearsal_runs", ["rehearsal_id"], ["rehearsal_id"], ondelete="RESTRICT")
    op.create_index("uq_authority_activation_live_generation", "authority_activations", ["generation_id"], unique=True, postgresql_where=sa.text("outcome = 'activated'"), sqlite_where=sa.text("outcome = 'activated'"))
    op.create_index("uq_authority_activation_live_projection_epoch", "authority_activations", ["projection_epoch"], unique=True, postgresql_where=sa.text("outcome = 'activated'"), sqlite_where=sa.text("outcome = 'activated'"))
    reservation_triggers = _sqlite_suspend_triggers_referencing("first_request_reservations")
    with op.batch_alter_table("first_request_reservations") as batch:
        batch.drop_constraint(op.f("uq_first_request_reservations_generation_id"), type_="unique")
    _sqlite_restore_triggers(reservation_triggers)
    op.create_index(
        "uq_first_request_reservation_live_generation",
        "first_request_reservations",
        ["generation_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('reserved','consumed')"),
        sqlite_where=sa.text("state IN ('reserved','consumed')"),
    )


def _sqlite_reservation_insert_guard() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute("DROP TRIGGER IF EXISTS first_request_reservations_initial_state_guard")
    op.execute(
        """
        CREATE TRIGGER first_request_reservations_initial_state_guard
        BEFORE INSERT ON first_request_reservations
        WHEN NEW.state <> 'reserved' OR NEW.reservation_revision <> 1
        BEGIN
            SELECT RAISE(ABORT,
                'first-request reservation must initially be reserved at revision 1');
        END
        """
    )


def _sqlite_authority_activation_teardown_guard() -> None:
    if op.get_bind().dialect.name != "sqlite":
        return
    op.execute("DROP TRIGGER IF EXISTS authority_activations_rehearsal_teardown_guard")
    op.execute(
        """
        CREATE TRIGGER authority_activations_rehearsal_teardown_guard
        BEFORE UPDATE ON authority_activations
        BEGIN
            SELECT CASE WHEN
                OLD.activation_id IS NOT NEW.activation_id
                OR OLD.generation_id IS NOT NEW.generation_id
                OR OLD.import_run_id IS NOT NEW.import_run_id
                OR OLD.cutover_approval_id IS NOT NEW.cutover_approval_id
                OR OLD.legacy_bundle_id IS NOT NEW.legacy_bundle_id
                OR OLD.registry_version_id IS NOT NEW.registry_version_id
                OR OLD.honest_binding_id IS NOT NEW.honest_binding_id
                OR OLD.rehearsal_id IS NOT NEW.rehearsal_id
                OR OLD.schema_head IS NOT NEW.schema_head
                OR OLD.dish_release IS NOT NEW.dish_release
                OR OLD.honest_release IS NOT NEW.honest_release
                OR OLD.protocol_release IS NOT NEW.protocol_release
                OR OLD.openapi_release IS NOT NEW.openapi_release
                OR OLD.routing_release IS NOT NEW.routing_release
                OR OLD.projection_epoch IS NOT NEW.projection_epoch
                OR OLD.recorded_at IS NOT NEW.recorded_at
            THEN RAISE(ABORT, 'authority activation identity is immutable') END;
            SELECT CASE WHEN
                OLD.outcome <> 'activated'
                OR NEW.outcome <> 'aborted'
                OR OLD.rehearsal_id IS NULL
                OR NEW.rollback_burned_at IS NOT NULL
                OR NOT EXISTS (
                    SELECT 1
                      FROM cutover_runs cr
                      JOIN rehearsal_runs rr
                        ON rr.rehearsal_id = cr.rehearsal_id
                     WHERE cr.rehearsal_id = OLD.rehearsal_id
                       AND cr.state = 'rehearsal_torn_down'
                       AND rr.status = 'running'
                       AND rr.rehearsal_kind = 'cutover'
                )
            THEN RAISE(ABORT, 'real or unrelated authority activation cannot be unwound') END;
        END
        """
    )


def _pg_forward() -> None:
    if op.get_bind().dialect.name != "postgresql": return
    op.execute(_CANDIDATE_0038)
    op.execute(_CUTOVER_0038)
    op.execute("""
    CREATE OR REPLACE FUNCTION dish_validate_authority_activation_rehearsal_teardown()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF OLD.activation_id <> NEW.activation_id OR OLD.generation_id <> NEW.generation_id OR OLD.import_run_id <> NEW.import_run_id OR OLD.cutover_approval_id <> NEW.cutover_approval_id OR OLD.legacy_bundle_id <> NEW.legacy_bundle_id OR OLD.registry_version_id IS DISTINCT FROM NEW.registry_version_id OR OLD.honest_binding_id IS DISTINCT FROM NEW.honest_binding_id OR OLD.rehearsal_id IS DISTINCT FROM NEW.rehearsal_id OR OLD.schema_head <> NEW.schema_head OR OLD.dish_release <> NEW.dish_release OR OLD.honest_release <> NEW.honest_release OR OLD.protocol_release <> NEW.protocol_release OR OLD.openapi_release <> NEW.openapi_release OR OLD.routing_release <> NEW.routing_release OR OLD.projection_epoch <> NEW.projection_epoch OR OLD.recorded_at <> NEW.recorded_at THEN RAISE EXCEPTION 'authority activation identity is immutable'; END IF;
        IF OLD.outcome <> 'activated' OR NEW.outcome <> 'aborted' OR OLD.rehearsal_id IS NULL OR NEW.rollback_burned_at IS NOT NULL OR NOT EXISTS (SELECT 1 FROM cutover_runs cr JOIN rehearsal_runs rr ON rr.rehearsal_id=cr.rehearsal_id WHERE cr.rehearsal_id=OLD.rehearsal_id AND cr.state='rehearsal_torn_down' AND rr.status='running' AND rr.rehearsal_kind='cutover') THEN RAISE EXCEPTION 'real or unrelated authority activation cannot be unwound'; END IF;
        RETURN NEW;
    END; $$;
    DROP TRIGGER IF EXISTS authority_activations_rehearsal_teardown_guard ON authority_activations;
    CREATE TRIGGER authority_activations_rehearsal_teardown_guard BEFORE UPDATE ON authority_activations FOR EACH ROW EXECUTE FUNCTION dish_validate_authority_activation_rehearsal_teardown();
    """)


def upgrade() -> None:
    _schema_forward()
    _sqlite_reservation_insert_guard()
    _sqlite_authority_activation_teardown_guard()
    _pg_forward()


def _assert_downgrade_safe() -> None:
    if op.get_context().as_sql: return
    bind=op.get_bind()
    for table,predicate in (("rehearsal_runs", "rehearsal_kind='cutover'"),("cutover_runs","rehearsal_id IS NOT NULL OR state='rehearsal_torn_down'"),("authority_activations","rehearsal_id IS NOT NULL")):
        if bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE {predicate} LIMIT 1")).scalar_one_or_none() is not None:
            raise RuntimeError("0038 downgrade would erase cutover rehearsal identity/history; restore from a pre-0038 backup instead")


def downgrade() -> None:
    _assert_downgrade_safe()
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS authority_activations_rehearsal_teardown_guard ON authority_activations")
        op.execute("DROP FUNCTION IF EXISTS dish_validate_authority_activation_rehearsal_teardown()")
        op.execute(_CANDIDATE_0037)
        op.execute(_CUTOVER_0005)
    elif bind.dialect.name == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS authority_activations_rehearsal_teardown_guard")
    op.drop_index("uq_first_request_reservation_live_generation", table_name="first_request_reservations")
    reservation_triggers = _sqlite_suspend_triggers_referencing("first_request_reservations")
    with op.batch_alter_table("first_request_reservations") as batch:
        batch.create_unique_constraint(op.f("uq_first_request_reservations_generation_id"), ["generation_id"])
    _sqlite_restore_triggers(reservation_triggers)
    op.drop_index("uq_authority_activation_live_projection_epoch", table_name="authority_activations")
    op.drop_index("uq_authority_activation_live_generation", table_name="authority_activations")
    with op.batch_alter_table("authority_activations") as batch:
        batch.drop_constraint("fk_authact_rehearsal", type_="foreignkey")
        batch.drop_column("rehearsal_id")
        batch.create_unique_constraint("uq_activation_generation_outcome", ["generation_id", "outcome"])
        batch.create_unique_constraint(op.f("uq_authority_activations_projection_epoch"), ["projection_epoch"])
    cutover_triggers = _sqlite_suspend_triggers_referencing("cutover_runs")
    with op.batch_alter_table("cutover_runs") as batch:
        batch.drop_constraint("fk_cutover_run_rehearsal_candidate", type_="foreignkey")
        batch.drop_constraint("uq_cutover_runs_rehearsal_id", type_="unique")
        batch.drop_constraint(op.f("ck_cutover_runs_teardown_requires_rehearsal"), type_="check")
        batch.drop_constraint(op.f("ck_cutover_runs_state_allowed"), type_="check")
        batch.drop_constraint(op.f("ck_cutover_runs_terminal_time_consistent"), type_="check")
        batch.drop_column("rehearsal_id")
        batch.create_check_constraint("state_allowed", "state IN ('prepared','fenced','activated','rollback_burned','admission_open','first_admission_verified','completed','aborted')")
        batch.create_check_constraint("terminal_time_consistent", "(state IN ('completed','aborted') AND terminal_at IS NOT NULL) OR (state NOT IN ('completed','aborted') AND terminal_at IS NULL)")
    _sqlite_restore_triggers(cutover_triggers)
    with op.batch_alter_table("rehearsal_runs") as batch:
        batch.drop_constraint("uq_rehearsal_candidate_identity", type_="unique")
        batch.drop_constraint(op.f("ck_rehearsal_runs_kind_allowed"), type_="check")
        batch.create_check_constraint("kind_allowed", "rehearsal_kind IN ('full','activation','restore','fault_injection')")
    _sqlite_reservation_insert_guard()
