"""Add a durable external-observation boundary to reconciliation evidence.

Revision ID: 0025_reconciliation_observation_boundary
Revises: 0024_typed_import_linkage
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0025_reconciliation_observation_boundary"
down_revision = "0024_typed_import_linkage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = (
        sa.Column("candidate_id", sa.Uuid(), nullable=True),
        sa.Column("registry_version_id", sa.Uuid(), nullable=True),
        sa.Column("observation_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observation_completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_snapshot_identity", sa.String(256), nullable=True),
        sa.Column("external_high_water", sa.String(256), nullable=True),
        sa.Column("corpus_manifest_sha256", sa.String(64), nullable=True),
        sa.Column("scope_complete", sa.Boolean(), nullable=True),
        sa.Column("adapter_contract_version", sa.String(64), nullable=True),
        sa.Column("evidence_recorded_at", sa.DateTime(timezone=True), nullable=True),
    )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("projection_reconciliation_runs") as batch:
            for column in columns:
                batch.add_column(column)
            batch.create_foreign_key(
                "fk_reconciliation_candidate",
                "release_candidates",
                ["candidate_id"],
                ["candidate_id"],
                ondelete="RESTRICT",
            )
            batch.create_foreign_key(
                "fk_reconciliation_registry_version",
                "section_registry_versions",
                ["registry_version_id"],
                ["registry_version_id"],
                ondelete="RESTRICT",
            )
            batch.create_check_constraint(
                "ck_reconciliation_candidate_observation_contract_complete",
                "(candidate_id IS NULL AND registry_version_id IS NULL AND observation_started_at IS NULL AND observation_completed_at IS NULL AND external_snapshot_identity IS NULL AND external_high_water IS NULL AND corpus_manifest_sha256 IS NULL AND scope_complete IS NULL AND adapter_contract_version IS NULL AND evidence_recorded_at IS NULL) OR (candidate_id IS NOT NULL AND registry_version_id IS NOT NULL AND observation_started_at IS NOT NULL AND corpus_manifest_sha256 IS NOT NULL AND length(corpus_manifest_sha256)=64 AND adapter_contract_version IS NOT NULL AND length(trim(adapter_contract_version))>0 AND evidence_recorded_at IS NOT NULL AND scope_complete IS NOT NULL)",
            )
            batch.create_check_constraint(
                "ck_reconciliation_observation_chronology",
                "observation_completed_at IS NULL OR observation_completed_at >= observation_started_at",
            )
            batch.create_check_constraint(
                "ck_reconciliation_complete_observation_boundary",
                "candidate_id IS NULL OR status <> 'complete' OR (scope_complete AND observation_completed_at IS NOT NULL AND evidence_recorded_at >= observation_completed_at)",
            )
    else:
        for column in columns:
            op.add_column("projection_reconciliation_runs", column)
        op.create_foreign_key(
            "fk_reconciliation_candidate",
            "projection_reconciliation_runs",
            "release_candidates",
            ["candidate_id"],
            ["candidate_id"],
            ondelete="RESTRICT",
        )
        op.create_foreign_key(
            "fk_reconciliation_registry_version",
            "projection_reconciliation_runs",
            "section_registry_versions",
            ["registry_version_id"],
            ["registry_version_id"],
            ondelete="RESTRICT",
        )
        op.create_check_constraint(
            "ck_reconciliation_candidate_observation_contract_complete",
            "projection_reconciliation_runs",
            "(candidate_id IS NULL AND registry_version_id IS NULL AND observation_started_at IS NULL AND observation_completed_at IS NULL AND external_snapshot_identity IS NULL AND external_high_water IS NULL AND corpus_manifest_sha256 IS NULL AND scope_complete IS NULL AND adapter_contract_version IS NULL AND evidence_recorded_at IS NULL) OR (candidate_id IS NOT NULL AND registry_version_id IS NOT NULL AND observation_started_at IS NOT NULL AND corpus_manifest_sha256 IS NOT NULL AND length(corpus_manifest_sha256)=64 AND adapter_contract_version IS NOT NULL AND length(trim(adapter_contract_version))>0 AND evidence_recorded_at IS NOT NULL AND scope_complete IS NOT NULL)",
        )
        op.create_check_constraint(
            "ck_reconciliation_observation_chronology",
            "projection_reconciliation_runs",
            "observation_completed_at IS NULL OR observation_completed_at >= observation_started_at",
        )
        op.create_check_constraint(
            "ck_reconciliation_complete_observation_boundary",
            "projection_reconciliation_runs",
            "candidate_id IS NULL OR status <> 'complete' OR (scope_complete AND observation_completed_at IS NOT NULL AND evidence_recorded_at >= observation_completed_at)",
        )
    op.create_index(
        "ix_reconciliation_candidate_boundary",
        "projection_reconciliation_runs",
        ["candidate_id", "projection_epoch_id", "registry_version_id"],
    )
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_validate_reconciliation_observation_boundary()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.candidate_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM release_candidates c
                    JOIN active_section_registries ar ON ar.generation_id=c.generation_id
                    WHERE c.candidate_id=NEW.candidate_id
                      AND c.generation_id=NEW.generation_id
                      AND c.projection_epoch_id=NEW.projection_epoch_id
                      AND ar.registry_version_id=NEW.registry_version_id
                ) THEN
                    RAISE EXCEPTION 'reconciliation boundary does not match candidate authority';
                END IF;
                IF TG_OP='UPDATE' THEN
                    IF OLD.candidate_id IS DISTINCT FROM NEW.candidate_id
                       OR OLD.generation_id IS DISTINCT FROM NEW.generation_id
                       OR OLD.projection_epoch_id IS DISTINCT FROM NEW.projection_epoch_id
                       OR OLD.registry_version_id IS DISTINCT FROM NEW.registry_version_id
                       OR OLD.observation_started_at IS DISTINCT FROM NEW.observation_started_at
                       OR OLD.corpus_manifest_sha256 IS DISTINCT FROM NEW.corpus_manifest_sha256
                       OR OLD.adapter_contract_version IS DISTINCT FROM NEW.adapter_contract_version THEN
                        RAISE EXCEPTION 'reconciliation observation identity is immutable';
                    END IF;
                    IF OLD.observation_completed_at IS NOT NULL AND OLD.observation_completed_at IS DISTINCT FROM NEW.observation_completed_at THEN
                        RAISE EXCEPTION 'reconciliation completion boundary is immutable once set';
                    END IF;
                    IF OLD.external_snapshot_identity IS NOT NULL AND OLD.external_snapshot_identity IS DISTINCT FROM NEW.external_snapshot_identity THEN
                        RAISE EXCEPTION 'external snapshot identity is immutable once set';
                    END IF;
                    IF OLD.external_high_water IS NOT NULL AND OLD.external_high_water IS DISTINCT FROM NEW.external_high_water THEN
                        RAISE EXCEPTION 'external high-water identity is immutable once set';
                    END IF;
                END IF;
                RETURN NEW;
            END; $$;
            CREATE TRIGGER projection_reconciliation_runs_observation_boundary
            BEFORE INSERT OR UPDATE ON projection_reconciliation_runs FOR EACH ROW
            EXECUTE FUNCTION dish_validate_reconciliation_observation_boundary();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    count = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM projection_reconciliation_runs "
                "WHERE candidate_id IS NOT NULL"
            )
        ).scalar_one()
    )
    if count:
        raise RuntimeError(
            "refusing lossy downgrade: candidate-bound reconciliation evidence exists"
        )
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP FUNCTION IF EXISTS "
            "dish_validate_reconciliation_observation_boundary() CASCADE"
        )
    op.drop_index(
        "ix_reconciliation_candidate_boundary",
        table_name="projection_reconciliation_runs",
    )
    check_names = (
        "ck_reconciliation_complete_observation_boundary",
        "ck_reconciliation_observation_chronology",
        "ck_reconciliation_candidate_observation_contract_complete",
    )
    column_names = (
        "evidence_recorded_at",
        "adapter_contract_version",
        "scope_complete",
        "corpus_manifest_sha256",
        "external_high_water",
        "external_snapshot_identity",
        "observation_completed_at",
        "observation_started_at",
        "registry_version_id",
        "candidate_id",
    )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("projection_reconciliation_runs") as batch:
            for name in check_names:
                batch.drop_constraint(name, type_="check")
            batch.drop_constraint(
                "fk_reconciliation_registry_version", type_="foreignkey"
            )
            batch.drop_constraint("fk_reconciliation_candidate", type_="foreignkey")
            for name in column_names:
                batch.drop_column(name)
    else:
        for name in check_names:
            op.drop_constraint(
                name, "projection_reconciliation_runs", type_="check"
            )
        op.drop_constraint(
            "fk_reconciliation_registry_version",
            "projection_reconciliation_runs",
            type_="foreignkey",
        )
        op.drop_constraint(
            "fk_reconciliation_candidate",
            "projection_reconciliation_runs",
            type_="foreignkey",
        )
        for name in column_names:
            op.drop_column("projection_reconciliation_runs", name)

