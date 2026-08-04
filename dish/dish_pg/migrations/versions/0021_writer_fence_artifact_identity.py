"""Bind writer-fence engagement to a verified filesystem artifact.

Revision ID: 0021_writer_fence_artifact_identity
Revises: 0020_first_request_reservation
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0021_writer_fence_artifact_identity"
down_revision = "0020_first_request_reservation"
branch_labels = None
depends_on = None


def _state_check() -> str:
    return (
        "(state = 'prepared' AND engaged_at IS NULL AND verified_at IS NULL AND released_at IS NULL "
        "AND proof_sha256 IS NULL AND artifact_observation_id IS NULL AND artifact_verification_result IS NULL) OR "
        "(state = 'engaged' AND engaged_at IS NOT NULL AND verified_at IS NULL AND released_at IS NULL "
        "AND proof_sha256 IS NULL AND artifact_observation_id IS NOT NULL AND artifact_verification_result = 'matched') OR "
        "(state = 'verified' AND engaged_at IS NOT NULL AND verified_at IS NOT NULL AND released_at IS NULL "
        "AND proof_sha256 IS NOT NULL AND artifact_observation_id IS NOT NULL AND artifact_verification_result = 'matched') OR "
        "(state = 'released' AND engaged_at IS NOT NULL AND released_at IS NOT NULL "
        "AND artifact_observation_id IS NOT NULL AND artifact_verification_result = 'matched')"
    )


def _replace_transition_guard() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_writer_fence_transition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.fence_id <> NEW.fence_id
               OR OLD.candidate_id <> NEW.candidate_id
               OR OLD.target_identity <> NEW.target_identity
               OR OLD.mechanism <> NEW.mechanism
               OR OLD.manifest_sha256 <> NEW.manifest_sha256
               OR OLD.prepared_at <> NEW.prepared_at THEN
                RAISE EXCEPTION 'legacy writer fence identity is immutable';
            END IF;
            IF OLD.state <> 'prepared' AND (
                OLD.artifact_observation_id IS DISTINCT FROM NEW.artifact_observation_id
                OR OLD.artifact_verification_result IS DISTINCT FROM NEW.artifact_verification_result
            ) THEN
                RAISE EXCEPTION 'engaged writer-fence artifact identity is immutable';
            END IF;
            IF NEW.fence_revision <> OLD.fence_revision + 1 THEN
                RAISE EXCEPTION 'writer fence revision must advance exactly once';
            END IF;
            IF (OLD.state = 'prepared' AND NEW.state <> 'engaged')
               OR (OLD.state = 'engaged' AND NEW.state NOT IN ('verified','released'))
               OR (OLD.state = 'verified' AND NEW.state <> 'released')
               OR OLD.state = 'released' THEN
                RAISE EXCEPTION 'illegal writer fence transition';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    invalid = 0
    if not op.get_context().as_sql:
        invalid = int(
            bind.execute(
                sa.text(
                    "SELECT count(*) FROM legacy_writer_fences "
                    "WHERE state <> 'prepared'"
                )
            ).scalar_one()
        )
    if invalid:
        raise RuntimeError(
            "cannot add verified artifact identity: predecessor contains engaged writer fences without artifact evidence"
        )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("legacy_writer_fences") as batch:
            batch.create_unique_constraint("uq_writer_fence_candidate_identity", ["fence_id", "candidate_id"])
    else:
        op.create_unique_constraint(
            "uq_writer_fence_candidate_identity", "legacy_writer_fences", ["fence_id", "candidate_id"]
        )
    op.create_table(
        "writer_fence_artifact_observations",
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("fence_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_generation_identity", sa.String(256), nullable=False),
        sa.Column("canonical_path", sa.String(1024), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("filesystem_device", sa.BigInteger(), nullable=False),
        sa.Column("filesystem_inode", sa.BigInteger(), nullable=False),
        sa.Column("file_type", sa.String(32), nullable=False),
        sa.Column("regular_file", sa.Boolean(), nullable=False),
        sa.Column("verification_result", sa.String(16), nullable=False),
        sa.Column("observation_contract_version", sa.String(64), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evidence_sha256", sa.String(64), nullable=False),
        sa.CheckConstraint("length(trim(artifact_generation_identity)) > 0", name="ck_writer_fence_artifact_observations_generation_identity_nonblank"),
        sa.CheckConstraint("canonical_path LIKE '/%' AND canonical_path NOT LIKE '%/../%'", name="ck_writer_fence_artifact_observations_canonical_path_absolute"),
        sa.CheckConstraint("length(content_sha256) = 64", name="ck_writer_fence_artifact_observations_content_hash_length"),
        sa.CheckConstraint("filesystem_device >= 0 AND filesystem_inode > 0", name="ck_writer_fence_artifact_observations_filesystem_identity_positive"),
        sa.CheckConstraint("file_type IN ('regular')", name="ck_writer_fence_artifact_observations_file_type_allowed"),
        sa.CheckConstraint("regular_file", name="ck_writer_fence_artifact_observations_regular_file_required"),
        sa.CheckConstraint("verification_result IN ('matched','mismatched','unverifiable')", name="ck_writer_fence_artifact_observations_verification_result_allowed"),
        sa.CheckConstraint("length(trim(observation_contract_version)) > 0", name="ck_writer_fence_artifact_observations_contract_version_nonblank"),
        sa.CheckConstraint("recorded_at >= observed_at", name="ck_writer_fence_artifact_observations_recording_after_observation"),
        sa.CheckConstraint("length(evidence_sha256) = 64", name="ck_writer_fence_artifact_observations_evidence_hash_length"),
        sa.ForeignKeyConstraint(["fence_id", "candidate_id"], ["legacy_writer_fences.fence_id", "legacy_writer_fences.candidate_id"], name="fk_writer_fence_artifact_observation_exact_fence", ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("observation_id", name="pk_writer_fence_artifact_observations"),
        sa.UniqueConstraint("fence_id", name="uq_writer_fence_artifact_observations_fence_id"),
        sa.UniqueConstraint("observation_id", "fence_id", "candidate_id", "verification_result", name="uq_writer_fence_artifact_observation_binding"),
        sa.UniqueConstraint("candidate_id", "canonical_path", "filesystem_device", "filesystem_inode", "content_sha256", name="uq_writer_fence_artifact_identity"),
    )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("legacy_writer_fences") as batch:
            batch.add_column(sa.Column("artifact_observation_id", sa.Uuid(), nullable=True))
            batch.add_column(sa.Column("artifact_verification_result", sa.String(16), nullable=True))
            batch.create_unique_constraint("uq_legacy_writer_fences_artifact_observation_id", ["artifact_observation_id"])
            batch.create_foreign_key(
                "fk_writer_fence_exact_artifact_observation", "writer_fence_artifact_observations",
                ["artifact_observation_id", "fence_id", "candidate_id", "artifact_verification_result"],
                ["observation_id", "fence_id", "candidate_id", "verification_result"], ondelete="RESTRICT",
            )
            batch.drop_constraint(op.f("ck_legacy_writer_fences_state_payload_consistent"), type_="check")
            batch.create_check_constraint(op.f("ck_legacy_writer_fences_state_payload_consistent"), _state_check())
    else:
        op.add_column("legacy_writer_fences", sa.Column("artifact_observation_id", sa.Uuid(), nullable=True))
        op.add_column("legacy_writer_fences", sa.Column("artifact_verification_result", sa.String(16), nullable=True))
        op.create_unique_constraint("uq_legacy_writer_fences_artifact_observation_id", "legacy_writer_fences", ["artifact_observation_id"])
        op.create_foreign_key(
            "fk_writer_fence_exact_artifact_observation", "legacy_writer_fences", "writer_fence_artifact_observations",
            ["artifact_observation_id", "fence_id", "candidate_id", "artifact_verification_result"],
            ["observation_id", "fence_id", "candidate_id", "verification_result"], ondelete="RESTRICT",
        )
        op.drop_constraint(op.f("ck_legacy_writer_fences_state_payload_consistent"), "legacy_writer_fences", type_="check")
        op.create_check_constraint(op.f("ck_legacy_writer_fences_state_payload_consistent"), "legacy_writer_fences", _state_check())
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_reject_immutable_writer_fence_artifact_observation()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'immutable writer-fence artifact observation';
            END; $$;
            CREATE TRIGGER writer_fence_artifact_observations_immutable_update
            BEFORE UPDATE ON writer_fence_artifact_observations FOR EACH ROW
            EXECUTE FUNCTION dish_reject_immutable_writer_fence_artifact_observation();
            CREATE TRIGGER writer_fence_artifact_observations_immutable_delete
            BEFORE DELETE ON writer_fence_artifact_observations FOR EACH ROW
            EXECUTE FUNCTION dish_reject_immutable_writer_fence_artifact_observation();
            """
        )
        _replace_transition_guard()


def downgrade() -> None:
    bind = op.get_bind()
    count = int(bind.execute(sa.text("SELECT count(*) FROM writer_fence_artifact_observations")).scalar_one())
    if count:
        raise RuntimeError("refusing lossy downgrade: writer-fence artifact observations exist")
    predecessor_check = ("(state = 'prepared' AND engaged_at IS NULL AND verified_at IS NULL AND released_at IS NULL AND proof_sha256 IS NULL) OR "
         "(state = 'engaged' AND engaged_at IS NOT NULL AND verified_at IS NULL AND released_at IS NULL AND proof_sha256 IS NULL) OR "
         "(state = 'verified' AND engaged_at IS NOT NULL AND verified_at IS NOT NULL AND released_at IS NULL AND proof_sha256 IS NOT NULL) OR "
         "(state = 'released' AND engaged_at IS NOT NULL AND released_at IS NOT NULL)")
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("legacy_writer_fences") as batch:
            batch.drop_constraint(op.f("ck_legacy_writer_fences_state_payload_consistent"), type_="check")
            batch.create_check_constraint("ck_legacy_writer_fences_state_payload_consistent", predecessor_check)
            batch.drop_constraint("fk_writer_fence_exact_artifact_observation", type_="foreignkey")
            batch.drop_constraint("uq_legacy_writer_fences_artifact_observation_id", type_="unique")
            batch.drop_column("artifact_verification_result")
            batch.drop_column("artifact_observation_id")
    else:
        op.drop_constraint(op.f("ck_legacy_writer_fences_state_payload_consistent"), "legacy_writer_fences", type_="check")
        op.create_check_constraint("ck_legacy_writer_fences_state_payload_consistent", "legacy_writer_fences", predecessor_check)
        op.drop_constraint("fk_writer_fence_exact_artifact_observation", "legacy_writer_fences", type_="foreignkey")
        op.drop_constraint("uq_legacy_writer_fences_artifact_observation_id", "legacy_writer_fences", type_="unique")
        op.drop_column("legacy_writer_fences", "artifact_verification_result")
        op.drop_column("legacy_writer_fences", "artifact_observation_id")
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS dish_reject_immutable_writer_fence_artifact_observation() CASCADE")
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_validate_writer_fence_transition()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                IF OLD.fence_id <> NEW.fence_id OR OLD.candidate_id <> NEW.candidate_id
                   OR OLD.target_identity <> NEW.target_identity OR OLD.mechanism <> NEW.mechanism
                   OR OLD.manifest_sha256 <> NEW.manifest_sha256 OR OLD.prepared_at <> NEW.prepared_at THEN
                    RAISE EXCEPTION 'legacy writer fence identity is immutable';
                END IF;
                IF NEW.fence_revision <> OLD.fence_revision + 1 THEN RAISE EXCEPTION 'writer fence revision must advance exactly once'; END IF;
                IF (OLD.state = 'prepared' AND NEW.state <> 'engaged')
                   OR (OLD.state = 'engaged' AND NEW.state NOT IN ('verified','released'))
                   OR (OLD.state = 'verified' AND NEW.state <> 'released') OR OLD.state = 'released' THEN
                    RAISE EXCEPTION 'illegal writer fence transition';
                END IF;
                RETURN NEW;
            END; $$;
            """
        )
    op.drop_table("writer_fence_artifact_observations")
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("legacy_writer_fences") as batch:
            batch.drop_constraint("uq_writer_fence_candidate_identity", type_="unique")
    else:
        op.drop_constraint("uq_writer_fence_candidate_identity", "legacy_writer_fences", type_="unique")
