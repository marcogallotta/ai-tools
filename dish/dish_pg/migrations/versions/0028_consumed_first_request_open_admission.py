"""Allow normal mutation admission after the first reservation is consumed.

Revision ID: 0028_consumed_first_request_open_admission
Revises: 0027_server_default_alignment
"""
from __future__ import annotations

from alembic import op

revision = "0028_consumed_first_request_open_admission"
down_revision = "0027_server_default_alignment"
branch_labels = None
depends_on = None


def _install_admission_function(*, consumed_allows_open_admission: bool) -> None:
    consumed_branch = (
        "RETURN NEW;"
        if consumed_allows_open_admission
        else """
            IF reservation.request_id IS DISTINCT FROM NEW.request_id
               OR reservation.command_name IS DISTINCT FROM NEW.command_name
               OR reservation.owner_id IS DISTINCT FROM NEW.owner_id
               OR reservation.principal_class IS DISTINCT FROM NEW.principal_class
               OR reservation.run_id IS DISTINCT FROM NEW.run_id
               OR reservation.canonical_payload_sha256 IS DISTINCT FROM NEW.canonical_payload_sha256 THEN
                RAISE EXCEPTION 'first PostgreSQL mutation does not match the reserved request';
            END IF;
            RETURN NEW;
        """
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION dish_require_open_mutation_admission()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
            control_candidate UUID;
            control_state VARCHAR(16);
            reservation first_request_reservations%ROWTYPE;
        BEGIN
            IF EXISTS (
                SELECT 1 FROM legacy_request_tombstones t
                 WHERE t.request_id = NEW.request_id
            ) THEN
                RAISE EXCEPTION 'request identity is reserved by legacy authority';
            END IF;

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

            IF reservation.state = 'consumed' THEN
                {consumed_branch}
            ELSIF reservation.state = 'cancelled' THEN
                RAISE EXCEPTION 'first PostgreSQL request reservation is not consumable';
            ELSIF reservation.state <> 'reserved' THEN
                RAISE EXCEPTION 'first PostgreSQL request reservation is not consumable';
            END IF;

            IF reservation.request_id IS DISTINCT FROM NEW.request_id
               OR reservation.command_name IS DISTINCT FROM NEW.command_name
               OR reservation.owner_id IS DISTINCT FROM NEW.owner_id
               OR reservation.principal_class IS DISTINCT FROM NEW.principal_class
               OR reservation.run_id IS DISTINCT FROM NEW.run_id
               OR reservation.canonical_payload_sha256 IS DISTINCT FROM NEW.canonical_payload_sha256 THEN
                RAISE EXCEPTION 'first PostgreSQL mutation does not match the reserved request';
            END IF;

            UPDATE first_request_reservations
               SET state = 'consumed',
                   reservation_revision = reservation_revision + 1,
                   consumed_at = NEW.admitted_at
             WHERE reservation_id = reservation.reservation_id;
            RETURN NEW;
        END;
        $$;
        """
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _install_admission_function(consumed_allows_open_admission=True)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _install_admission_function(consumed_allows_open_admission=False)
