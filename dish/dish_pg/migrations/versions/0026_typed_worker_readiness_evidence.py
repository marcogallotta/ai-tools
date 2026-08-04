"""Replace self-attested worker readiness strings with typed probe evidence.

Revision ID: 0026_typed_worker_readiness_evidence
Revises: 0025_reconciliation_observation_boundary
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0026_typed_worker_readiness_evidence"
down_revision = "0025_reconciliation_observation_boundary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "worker_probe_inventories",
        sa.Column("inventory_id",sa.Uuid(),nullable=False),
        sa.Column("candidate_id",sa.Uuid(),nullable=False),
        sa.Column("projection_epoch_id",sa.Uuid(),nullable=False),
        sa.Column("inventory_version",sa.BigInteger(),nullable=False),
        sa.Column("required_probe_count",sa.BigInteger(),nullable=False),
        sa.Column("inventory_sha256",sa.String(64),nullable=False),
        sa.Column("inventory_contract_version",sa.String(64),nullable=False),
        sa.Column("sealed_at",sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint("inventory_version=1",name="ck_worker_probe_inventories_inventory_version_one"),
        sa.CheckConstraint("required_probe_count>0",name="ck_worker_probe_inventories_required_probe_count_positive"),
        sa.CheckConstraint("length(inventory_sha256)=64",name="ck_worker_probe_inventories_inventory_hash_length"),
        sa.CheckConstraint("length(trim(inventory_contract_version))>0",name="ck_worker_probe_inventories_inventory_contract_nonblank"),
        sa.ForeignKeyConstraint(["candidate_id"],["release_candidates.candidate_id"],ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["projection_epoch_id"],["projection_epochs.projection_epoch_id"],ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("inventory_id",name="pk_worker_probe_inventories"),
        sa.UniqueConstraint("candidate_id",name="uq_worker_probe_inventories_candidate_id"),
        sa.UniqueConstraint("candidate_id","projection_epoch_id",name="uq_worker_probe_inventory_candidate_epoch"),
    )
    op.create_table(
        "worker_probe_requirements",
        sa.Column("requirement_id",sa.Uuid(),nullable=False),
        sa.Column("inventory_id",sa.Uuid(),nullable=False),
        sa.Column("probe_kind",sa.String(64),nullable=False),
        sa.Column("ordinal",sa.BigInteger(),nullable=False),
        sa.Column("probe_contract_version",sa.String(64),nullable=False),
        sa.CheckConstraint("length(trim(probe_kind))>0",name="ck_worker_probe_requirements_probe_kind_nonblank"),
        sa.CheckConstraint("ordinal>=0",name="ck_worker_probe_requirements_ordinal_nonnegative"),
        sa.CheckConstraint("length(trim(probe_contract_version))>0",name="ck_worker_probe_requirements_probe_contract_nonblank"),
        sa.ForeignKeyConstraint(["inventory_id"],["worker_probe_inventories.inventory_id"],ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("requirement_id",name="pk_worker_probe_requirements"),
        sa.UniqueConstraint("inventory_id","probe_kind",name="uq_worker_probe_requirement_kind"),
        sa.UniqueConstraint("inventory_id","ordinal",name="uq_worker_probe_requirement_ordinal"),
        sa.UniqueConstraint("requirement_id","inventory_id","probe_kind",name="uq_worker_probe_requirement_exact"),
    )
    bind = op.get_bind()
    probe_inventory_column = sa.Column(
        "probe_inventory_id", sa.Uuid(), nullable=True
    )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("projection_worker_readiness") as batch:
            batch.add_column(probe_inventory_column)
            batch.create_foreign_key(
                "fk_projection_worker_readiness_probe_inventory",
                "worker_probe_inventories",
                ["probe_inventory_id"],
                ["inventory_id"],
                ondelete="RESTRICT",
            )
    else:
        op.add_column("projection_worker_readiness", probe_inventory_column)
        op.create_foreign_key(
            "fk_projection_worker_readiness_probe_inventory",
            "projection_worker_readiness",
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
        "worker_probe_evidence",
        sa.Column("evidence_id",sa.Uuid(),nullable=False),
        sa.Column("readiness_id",sa.Uuid(),nullable=False),
        sa.Column("requirement_id",sa.Uuid(),nullable=False),
        sa.Column("inventory_id",sa.Uuid(),nullable=False),
        sa.Column("candidate_id",sa.Uuid(),nullable=False),
        sa.Column("projection_epoch_id",sa.Uuid(),nullable=False),
        sa.Column("probe_kind",sa.String(64),nullable=False),
        sa.Column("execution_identity",sa.String(256),nullable=False),
        sa.Column("worker_identity",sa.String(256),nullable=False),
        sa.Column("deployed_artifact_sha256",sa.String(64),nullable=False),
        sa.Column("result",sa.String(16),nullable=False),
        sa.Column("observed_at",sa.DateTime(timezone=True),nullable=False),
        sa.Column("evidence_artifact_identity",sa.String(512),nullable=False),
        sa.Column("evidence_sha256",sa.String(64),nullable=False),
        sa.Column("recorded_at",sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint("length(trim(probe_kind))>0",name="ck_worker_probe_evidence_probe_kind_nonblank"),
        sa.CheckConstraint("length(trim(execution_identity))>0",name="ck_worker_probe_evidence_execution_identity_nonblank"),
        sa.CheckConstraint("length(trim(worker_identity))>0",name="ck_worker_probe_evidence_worker_identity_nonblank"),
        sa.CheckConstraint("length(deployed_artifact_sha256)=64",name="ck_worker_probe_evidence_deployed_artifact_hash_length"),
        sa.CheckConstraint("result IN ('pass','fail','error')",name="ck_worker_probe_evidence_result_allowed"),
        sa.CheckConstraint("length(trim(evidence_artifact_identity))>0",name="ck_worker_probe_evidence_evidence_artifact_nonblank"),
        sa.CheckConstraint("length(evidence_sha256)=64",name="ck_worker_probe_evidence_evidence_hash_length"),
        sa.CheckConstraint("recorded_at>=observed_at",name="ck_worker_probe_evidence_recording_after_observation"),
        sa.ForeignKeyConstraint(["readiness_id"],["projection_worker_readiness.readiness_id"],ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requirement_id"],["worker_probe_requirements.requirement_id"],ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["inventory_id"],["worker_probe_inventories.inventory_id"],ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["candidate_id"],["release_candidates.candidate_id"],ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["projection_epoch_id"],["projection_epochs.projection_epoch_id"],ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("evidence_id",name="pk_worker_probe_evidence"),
        sa.UniqueConstraint("requirement_id",name="uq_worker_probe_evidence_requirement_id"),
        sa.UniqueConstraint("readiness_id","probe_kind",name="uq_worker_probe_evidence_readiness_kind"),
    )
    op.create_index("ix_worker_probe_evidence_candidate_epoch","worker_probe_evidence",["candidate_id","projection_epoch_id"])
    op.create_table(
        "worker_readiness_completions",
        sa.Column("completion_id",sa.Uuid(),nullable=False),
        sa.Column("readiness_id",sa.Uuid(),nullable=False),
        sa.Column("inventory_id",sa.Uuid(),nullable=False),
        sa.Column("candidate_id",sa.Uuid(),nullable=False),
        sa.Column("projection_epoch_id",sa.Uuid(),nullable=False),
        sa.Column("completion_state",sa.String(16),nullable=False),
        sa.Column("required_probe_count",sa.BigInteger(),nullable=False),
        sa.Column("passed_probe_count",sa.BigInteger(),nullable=False),
        sa.Column("completion_sha256",sa.String(64),nullable=False),
        sa.Column("completed_at",sa.DateTime(timezone=True),nullable=False),
        sa.CheckConstraint("completion_state='complete'",name="ck_worker_readiness_completions_completion_state_exact"),
        sa.CheckConstraint("required_probe_count>0 AND passed_probe_count=required_probe_count",name="ck_worker_readiness_completions_exact_probe_counts"),
        sa.CheckConstraint("length(completion_sha256)=64",name="ck_worker_readiness_completions_completion_hash_length"),
        sa.ForeignKeyConstraint(["readiness_id"],["projection_worker_readiness.readiness_id"],ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["inventory_id"],["worker_probe_inventories.inventory_id"],ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["candidate_id"],["release_candidates.candidate_id"],ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["projection_epoch_id"],["projection_epochs.projection_epoch_id"],ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("completion_id",name="pk_worker_readiness_completions"),
        sa.UniqueConstraint("readiness_id",name="uq_worker_readiness_completions_readiness_id"),
        sa.UniqueConstraint("inventory_id",name="uq_worker_readiness_completions_inventory_id"),
        sa.UniqueConstraint("candidate_id",name="uq_worker_readiness_completions_candidate_id"),
    )
    if op.get_bind().dialect.name=="postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_validate_worker_probe_inventory()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM release_candidates c WHERE c.candidate_id=NEW.candidate_id AND c.projection_epoch_id=NEW.projection_epoch_id) THEN
                    RAISE EXCEPTION 'probe inventory does not match candidate projection epoch';
                END IF;
                RETURN NEW;
            END; $$;
            CREATE TRIGGER worker_probe_inventories_validate BEFORE INSERT ON worker_probe_inventories FOR EACH ROW EXECUTE FUNCTION dish_validate_worker_probe_inventory();

            CREATE OR REPLACE FUNCTION dish_validate_worker_probe_evidence()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM projection_worker_readiness r
                    JOIN worker_probe_requirements q ON q.requirement_id=NEW.requirement_id
                    WHERE r.readiness_id=NEW.readiness_id
                      AND r.candidate_id=NEW.candidate_id
                      AND r.projection_epoch_id=NEW.projection_epoch_id
                      AND r.probe_inventory_id=NEW.inventory_id
                      AND r.worker_identity=NEW.worker_identity
                      AND q.inventory_id=NEW.inventory_id
                      AND q.probe_kind=NEW.probe_kind
                ) THEN RAISE EXCEPTION 'probe evidence does not match readiness inventory requirement'; END IF;
                RETURN NEW;
            END; $$;
            CREATE TRIGGER worker_probe_evidence_validate BEFORE INSERT ON worker_probe_evidence FOR EACH ROW EXECUTE FUNCTION dish_validate_worker_probe_evidence();

            CREATE OR REPLACE FUNCTION dish_validate_worker_readiness_completion()
            RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE inventory_count bigint; requirement_count bigint; pass_count bigint;
            BEGIN
                SELECT i.required_probe_count INTO inventory_count
                  FROM worker_probe_inventories i
                  JOIN projection_worker_readiness r ON r.probe_inventory_id=i.inventory_id
                 WHERE i.inventory_id=NEW.inventory_id AND i.candidate_id=NEW.candidate_id
                   AND i.projection_epoch_id=NEW.projection_epoch_id
                   AND r.readiness_id=NEW.readiness_id AND r.candidate_id=NEW.candidate_id
                   AND r.projection_epoch_id=NEW.projection_epoch_id;
                IF NOT FOUND THEN RAISE EXCEPTION 'completion does not match typed readiness identity'; END IF;
                SELECT count(*) INTO requirement_count FROM worker_probe_requirements WHERE inventory_id=NEW.inventory_id;
                SELECT count(*) INTO pass_count FROM worker_probe_evidence
                 WHERE inventory_id=NEW.inventory_id AND readiness_id=NEW.readiness_id AND result='pass';
                IF inventory_count<>requirement_count OR NEW.required_probe_count<>requirement_count
                   OR NEW.passed_probe_count<>pass_count OR pass_count<>requirement_count THEN
                    RAISE EXCEPTION 'typed worker probe inventory is not exactly complete';
                END IF;
                RETURN NEW;
            END; $$;
            CREATE TRIGGER worker_readiness_completions_validate BEFORE INSERT ON worker_readiness_completions FOR EACH ROW EXECUTE FUNCTION dish_validate_worker_readiness_completion();

            CREATE OR REPLACE FUNCTION dish_reject_typed_readiness_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'typed worker readiness evidence is immutable'; END; $$;
            """
        )
        for table in ("worker_probe_inventories","worker_probe_requirements","worker_probe_evidence","worker_readiness_completions"):
            op.execute(f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} FOR EACH ROW EXECUTE FUNCTION dish_reject_typed_readiness_mutation()")
            op.execute(f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION dish_reject_typed_readiness_mutation()")


def downgrade() -> None:
    bind=op.get_bind()
    count=int(bind.execute(sa.text("SELECT (SELECT count(*) FROM worker_probe_inventories)+(SELECT count(*) FROM worker_probe_evidence)+(SELECT count(*) FROM worker_readiness_completions)")).scalar_one())
    if count: raise RuntimeError("refusing lossy downgrade: typed worker readiness authority exists")
    if bind.dialect.name=="postgresql": op.execute("DROP FUNCTION IF EXISTS dish_validate_worker_probe_inventory() CASCADE; DROP FUNCTION IF EXISTS dish_validate_worker_probe_evidence() CASCADE; DROP FUNCTION IF EXISTS dish_validate_worker_readiness_completion() CASCADE; DROP FUNCTION IF EXISTS dish_reject_typed_readiness_mutation() CASCADE")
    op.drop_table("worker_readiness_completions")
    op.drop_index("ix_worker_probe_evidence_candidate_epoch",table_name="worker_probe_evidence")
    op.drop_table("worker_probe_evidence")
    op.drop_index(
        "ix_projection_worker_readiness_probe_inventory",
        table_name="projection_worker_readiness",
    )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("projection_worker_readiness") as batch:
            batch.drop_constraint(
                "fk_projection_worker_readiness_probe_inventory",
                type_="foreignkey",
            )
            batch.drop_column("probe_inventory_id")
    else:
        op.drop_constraint(
            "fk_projection_worker_readiness_probe_inventory",
            "projection_worker_readiness",
            type_="foreignkey",
        )
        op.drop_column("projection_worker_readiness", "probe_inventory_id")
    op.drop_table("worker_probe_requirements")
    op.drop_table("worker_probe_inventories")
