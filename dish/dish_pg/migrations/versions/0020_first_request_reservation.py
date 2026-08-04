"""Reserve and atomically consume the exact planned first request.

Revision ID: 0020_first_request_reservation
Revises: 0019_request_run_owner_consistency
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0020_first_request_reservation"
down_revision = "0019_request_run_owner_consistency"
branch_labels = None
depends_on = None


def _create_identity_constraints() -> None:
    bind = op.get_bind()
    specs = (
        ("release_candidates", "uq_release_candidate_generation_identity", ["candidate_id", "generation_id"]),
        ("cutover_runs", "uq_cutover_run_candidate_identity", ["cutover_run_id", "candidate_id"]),
        ("first_admission_plans", "uq_first_admission_plan_exact_request", ["plan_id", "cutover_run_id", "request_id", "command_name"]),
    )
    if bind.dialect.name == "sqlite":
        for table, name, columns in specs:
            with op.batch_alter_table(table) as batch:
                batch.create_unique_constraint(name, columns)
        return
    for table, name, columns in specs:
        op.create_unique_constraint(name, table, columns)


def _create_table() -> None:
    op.create_table(
        "first_request_reservations",
        sa.Column("reservation_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("cutover_run_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("command_name", sa.String(length=64), nullable=False),
        sa.Column("owner_id", sa.String(length=256), nullable=False),
        sa.Column("principal_class", sa.String(length=32), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column("reservation_revision", sa.BigInteger(), nullable=False),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("length(trim(command_name)) > 0", name="ck_first_request_reservations_command_nonblank"),
        sa.CheckConstraint("length(trim(owner_id)) > 0", name="ck_first_request_reservations_owner_nonblank"),
        sa.CheckConstraint(
            "principal_class IN ('agent','admin','verification','service')",
            name="ck_first_request_reservations_principal_class_allowed",
        ),
        sa.CheckConstraint(
            "length(canonical_payload_sha256) = 64",
            name="ck_first_request_reservations_payload_hash_length",
        ),
        sa.CheckConstraint(
            "state IN ('reserved','consumed','cancelled')",
            name="ck_first_request_reservations_state_allowed",
        ),
        sa.CheckConstraint(
            "reservation_revision > 0",
            name="ck_first_request_reservations_positive_revision",
        ),
        sa.CheckConstraint(
            "(state = 'reserved' AND consumed_at IS NULL) OR "
            "(state = 'consumed' AND consumed_at IS NOT NULL) OR "
            "(state = 'cancelled' AND consumed_at IS NULL)",
            name="ck_first_request_reservations_state_time_consistent",
        ),
        sa.ForeignKeyConstraint(
            ["candidate_id", "generation_id"],
            ["release_candidates.candidate_id", "release_candidates.generation_id"],
            name="fk_first_request_reservation_candidate_generation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["cutover_run_id", "candidate_id"],
            ["cutover_runs.cutover_run_id", "cutover_runs.candidate_id"],
            name="fk_first_request_reservation_cutover_candidate",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id", "cutover_run_id", "request_id", "command_name"],
            [
                "first_admission_plans.plan_id",
                "first_admission_plans.cutover_run_id",
                "first_admission_plans.request_id",
                "first_admission_plans.command_name",
            ],
            name="fk_first_request_reservation_exact_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id", "owner_id", "run_id"],
            ["service_runs.generation_id", "service_runs.owner_id", "service_runs.run_id"],
            name="fk_first_request_reservation_exact_run_owner",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("reservation_id", name="pk_first_request_reservations"),
        sa.UniqueConstraint("plan_id", name="uq_first_request_reservations_plan_id"),
        sa.UniqueConstraint("cutover_run_id", name="uq_first_request_reservations_cutover_run_id"),
        sa.UniqueConstraint("candidate_id", name="uq_first_request_reservations_candidate_id"),
        sa.UniqueConstraint("generation_id", name="uq_first_request_reservations_generation_id"),
        sa.UniqueConstraint("request_id", name="uq_first_request_reservations_request_id"),
        sa.UniqueConstraint(
            "generation_id", "candidate_id", name="uq_first_request_reservation_authority"
        ),
    )


def _replace_admission_function() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_require_open_mutation_admission()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            control_candidate UUID;
            control_state VARCHAR(16);
            reservation first_request_reservations%ROWTYPE;
        BEGIN
            SELECT candidate_id, state
              INTO control_candidate, control_state
              FROM mutation_admission_controls
             WHERE generation_id = NEW.generation_id;

            IF NOT FOUND THEN
                RETURN NEW;
            END IF;
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
                   SET state = 'consumed',
                       reservation_revision = reservation_revision + 1,
                       consumed_at = NEW.admitted_at
                 WHERE reservation_id = reservation.reservation_id;
            ELSIF reservation.state <> 'consumed' THEN
                RAISE EXCEPTION 'first PostgreSQL request reservation is not consumable';
            END IF;
            RETURN NEW;
        END;
        $$;
        """
    )


def _create_transition_guard() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_first_request_reservation_transition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.reservation_id <> NEW.reservation_id
               OR OLD.plan_id <> NEW.plan_id
               OR OLD.cutover_run_id <> NEW.cutover_run_id
               OR OLD.candidate_id <> NEW.candidate_id
               OR OLD.generation_id <> NEW.generation_id
               OR OLD.request_id <> NEW.request_id
               OR OLD.command_name <> NEW.command_name
               OR OLD.owner_id <> NEW.owner_id
               OR OLD.principal_class <> NEW.principal_class
               OR OLD.run_id <> NEW.run_id
               OR OLD.canonical_payload_sha256 <> NEW.canonical_payload_sha256
               OR OLD.reserved_at <> NEW.reserved_at THEN
                RAISE EXCEPTION 'first-request reservation identity is immutable';
            END IF;
            IF NEW.reservation_revision <> OLD.reservation_revision + 1 THEN
                RAISE EXCEPTION 'first-request reservation revision must advance exactly once';
            END IF;
            IF OLD.state <> 'reserved' OR NEW.state NOT IN ('consumed','cancelled') THEN
                RAISE EXCEPTION 'illegal first-request reservation transition';
            END IF;
            RETURN NEW;
        END;
        $$;
        CREATE TRIGGER first_request_reservations_transition_guard
        BEFORE UPDATE ON first_request_reservations FOR EACH ROW
        EXECUTE FUNCTION dish_validate_first_request_reservation_transition();
        """
    )


def upgrade() -> None:
    bind = op.get_bind()
    open_controls = 0
    if not op.get_context().as_sql:
        open_controls = int(
            bind.execute(
                sa.text(
                    "SELECT count(*) FROM mutation_admission_controls "
                    "WHERE state = 'open'"
                )
            ).scalar_one()
        )
    if open_controls:
        raise RuntimeError(
            "cannot install exact first-request reservations while mutation admission is open; "
            "close the control and create the exact reservation before reopening"
        )
    _create_identity_constraints()
    _create_table()
    if bind.dialect.name == "postgresql":
        _create_transition_guard()
        _replace_admission_function()


def downgrade() -> None:
    bind = op.get_bind()
    count = int(bind.execute(sa.text("SELECT count(*) FROM first_request_reservations")).scalar_one())
    if count:
        raise RuntimeError(
            "refusing lossy downgrade: first-request reservation authority rows exist"
        )
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS dish_validate_first_request_reservation_transition() CASCADE")
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_require_open_mutation_admission()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM mutation_admission_controls mac
                     WHERE mac.generation_id = NEW.generation_id
                       AND mac.state = 'closed'
                ) THEN
                    RAISE EXCEPTION 'PostgreSQL mutation admission is closed';
                END IF;
                RETURN NEW;
            END;
            $$;
            """
        )
    op.drop_table("first_request_reservations")
    specs = (
        ("first_admission_plans", "uq_first_admission_plan_exact_request"),
        ("cutover_runs", "uq_cutover_run_candidate_identity"),
        ("release_candidates", "uq_release_candidate_generation_identity"),
    )
    if bind.dialect.name == "sqlite":
        for table, name in specs:
            with op.batch_alter_table(table) as batch:
                batch.drop_constraint(name, type_="unique")
    else:
        for table, name in specs:
            op.drop_constraint(name, table, type_="unique")
