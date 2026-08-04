"""Protect PostgreSQL request identity from legacy request-ID reuse.

Revision ID: 0023_legacy_request_tombstones
Revises: 0022_candidate_state_manifest
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0023_legacy_request_tombstones"
down_revision = "0022_candidate_state_manifest"
branch_labels = None
depends_on = None


def _create_table() -> None:
    op.create_table(
        "legacy_request_tombstones",
        sa.Column("tombstone_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("source_authority", sa.String(64), nullable=False),
        sa.Column("import_run_id", sa.Uuid(), nullable=False),
        sa.Column("import_batch_id", sa.Uuid(), nullable=True),
        sa.Column("source_identity_sha256", sa.String(64), nullable=False),
        sa.Column("source_metadata", sa.JSON(), nullable=True),
        sa.Column("imported_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(source_authority)) > 0", name="ck_legacy_request_tombstones_source_authority_nonblank"),
        sa.CheckConstraint("length(source_identity_sha256) = 64", name="ck_legacy_request_tombstones_source_identity_hash_length"),
        sa.ForeignKeyConstraint(["import_run_id"], ["stage_a_import_runs.import_run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["import_batch_id"], ["source_import_batches.import_batch_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("tombstone_id", name="pk_legacy_request_tombstones"),
        sa.UniqueConstraint("request_id", name="uq_legacy_request_tombstones_request_id"),
    )
    op.create_index("ix_legacy_request_tombstones_import_run", "legacy_request_tombstones", ["import_run_id"])


def _install_guards() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_legacy_request_tombstone()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM service_requests r WHERE r.request_id=NEW.request_id) THEN
                RAISE EXCEPTION 'cannot tombstone a native PostgreSQL request identity';
            END IF;
            IF NEW.import_batch_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM source_import_batches b
                 WHERE b.import_batch_id=NEW.import_batch_id
                   AND b.import_run_id=NEW.import_run_id
            ) THEN
                RAISE EXCEPTION 'legacy request tombstone import batch/run mismatch';
            END IF;
            RETURN NEW;
        END; $$;
        CREATE TRIGGER legacy_request_tombstones_validate
        BEFORE INSERT ON legacy_request_tombstones FOR EACH ROW
        EXECUTE FUNCTION dish_validate_legacy_request_tombstone();

        CREATE OR REPLACE FUNCTION dish_reject_legacy_request_tombstone_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'legacy request tombstones are immutable';
        END; $$;
        CREATE TRIGGER legacy_request_tombstones_immutable_update
        BEFORE UPDATE ON legacy_request_tombstones FOR EACH ROW
        EXECUTE FUNCTION dish_reject_legacy_request_tombstone_mutation();
        CREATE TRIGGER legacy_request_tombstones_immutable_delete
        BEFORE DELETE ON legacy_request_tombstones FOR EACH ROW
        EXECUTE FUNCTION dish_reject_legacy_request_tombstone_mutation();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_require_open_mutation_admission()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            control_candidate UUID;
            control_state VARCHAR(16);
            reservation first_request_reservations%ROWTYPE;
        BEGIN
            IF EXISTS (
                SELECT 1 FROM legacy_request_tombstones t
                 WHERE t.request_id=NEW.request_id
            ) THEN
                RAISE EXCEPTION 'request identity is reserved by legacy authority';
            END IF;

            SELECT candidate_id, state
              INTO control_candidate, control_state
              FROM mutation_admission_controls
             WHERE generation_id = NEW.generation_id;
            IF NOT FOUND THEN RETURN NEW; END IF;
            IF control_state <> 'open' THEN
                RAISE EXCEPTION 'PostgreSQL mutation admission is closed';
            END IF;

            SELECT * INTO reservation
              FROM first_request_reservations
             WHERE generation_id = NEW.generation_id
               AND candidate_id = control_candidate
             FOR UPDATE;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'open mutation admission lacks an exact first-request reservation';
            END IF;
            IF reservation.request_id IS DISTINCT FROM NEW.request_id
               OR reservation.command_name IS DISTINCT FROM NEW.command_name
               OR reservation.owner_id IS DISTINCT FROM NEW.owner_id
               OR reservation.principal_class IS DISTINCT FROM NEW.principal_class
               OR reservation.run_id IS DISTINCT FROM NEW.run_id
               OR reservation.canonical_payload_sha256 IS DISTINCT FROM NEW.canonical_payload_sha256 THEN
                RAISE EXCEPTION 'first PostgreSQL mutation does not match the reserved request';
            END IF;
            IF reservation.state = 'reserved' THEN
                UPDATE first_request_reservations
                   SET state='consumed', reservation_revision=reservation_revision+1,
                       consumed_at=NEW.admitted_at
                 WHERE reservation_id=reservation.reservation_id;
            ELSIF reservation.state <> 'consumed' THEN
                RAISE EXCEPTION 'first PostgreSQL request reservation is not consumable';
            END IF;
            RETURN NEW;
        END; $$;
        """
    )


def upgrade() -> None:
    _create_table()
    if op.get_bind().dialect.name == "postgresql":
        _install_guards()


def downgrade() -> None:
    bind = op.get_bind()
    if int(bind.execute(sa.text("SELECT count(*) FROM legacy_request_tombstones")).scalar_one()):
        raise RuntimeError("refusing lossy downgrade: legacy request tombstones exist")
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS dish_validate_legacy_request_tombstone() CASCADE")
        op.execute("DROP FUNCTION IF EXISTS dish_reject_legacy_request_tombstone_mutation() CASCADE")
        # Reinstall the exact-reservation admission function from revision 0020.
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_require_open_mutation_admission()
            RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE control_candidate UUID; control_state VARCHAR(16); reservation first_request_reservations%ROWTYPE;
            BEGIN
                SELECT candidate_id,state INTO control_candidate,control_state
                  FROM mutation_admission_controls WHERE generation_id=NEW.generation_id;
                IF NOT FOUND THEN RETURN NEW; END IF;
                IF control_state <> 'open' THEN RAISE EXCEPTION 'PostgreSQL mutation admission is closed'; END IF;
                SELECT * INTO reservation FROM first_request_reservations
                 WHERE generation_id=NEW.generation_id AND candidate_id=control_candidate FOR UPDATE;
                IF NOT FOUND THEN RAISE EXCEPTION 'open mutation admission lacks an exact first-request reservation'; END IF;
                IF reservation.request_id IS DISTINCT FROM NEW.request_id
                   OR reservation.command_name IS DISTINCT FROM NEW.command_name
                   OR reservation.owner_id IS DISTINCT FROM NEW.owner_id
                   OR reservation.principal_class IS DISTINCT FROM NEW.principal_class
                   OR reservation.run_id IS DISTINCT FROM NEW.run_id
                   OR reservation.canonical_payload_sha256 IS DISTINCT FROM NEW.canonical_payload_sha256 THEN
                    RAISE EXCEPTION 'first PostgreSQL mutation does not match the reserved request';
                END IF;
                IF reservation.state='reserved' THEN
                    UPDATE first_request_reservations SET state='consumed',
                      reservation_revision=reservation_revision+1,consumed_at=NEW.admitted_at
                    WHERE reservation_id=reservation.reservation_id;
                ELSIF reservation.state <> 'consumed' THEN
                    RAISE EXCEPTION 'first PostgreSQL request reservation is not consumable';
                END IF;
                RETURN NEW;
            END; $$;
            """
        )
    op.drop_index("ix_legacy_request_tombstones_import_run", table_name="legacy_request_tombstones")
    op.drop_table("legacy_request_tombstones")
