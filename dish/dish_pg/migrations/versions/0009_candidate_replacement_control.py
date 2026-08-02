"""Permit exact pre-burn candidate replacement on a closed generation control.

Revision ID: 0009_candidate_replacement_control
Revises: 0008_fail_closed_admission_outbox
"""
from __future__ import annotations

from alembic import op

revision = "0009_candidate_replacement_control"
down_revision = "0008_fail_closed_admission_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_mutation_admission_transition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.generation_id <> NEW.generation_id THEN
                RAISE EXCEPTION 'mutation admission generation identity is immutable';
            END IF;
            IF NEW.control_revision <> OLD.control_revision + 1 THEN
                RAISE EXCEPTION 'mutation admission revision must advance exactly once';
            END IF;
            IF OLD.candidate_id <> NEW.candidate_id THEN
                IF OLD.state <> 'closed' OR NEW.state <> 'closed'
                   OR OLD.opened_at IS NOT NULL OR NEW.opened_at IS NOT NULL
                   OR NOT EXISTS (
                        SELECT 1 FROM release_candidates old_candidate
                         WHERE old_candidate.candidate_id = OLD.candidate_id
                           AND old_candidate.generation_id = OLD.generation_id
                           AND old_candidate.status = 'aborted'
                   )
                   OR NOT EXISTS (
                        SELECT 1 FROM release_candidates new_candidate
                         WHERE new_candidate.candidate_id = NEW.candidate_id
                           AND new_candidate.generation_id = NEW.generation_id
                           AND new_candidate.status = 'assembling'
                   )
                   OR EXISTS (
                        SELECT 1 FROM authority_activations activation
                         WHERE activation.generation_id = OLD.generation_id
                           AND activation.outcome = 'activated'
                   ) THEN
                    RAISE EXCEPTION 'mutation admission candidate may rebind only after exact pre-burn abort';
                END IF;
                RETURN NEW;
            END IF;
            IF OLD.state <> 'closed' OR NEW.state <> 'open' THEN
                RAISE EXCEPTION 'mutation admission may open exactly once';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_mutation_admission_transition()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.generation_id <> NEW.generation_id
               OR OLD.candidate_id <> NEW.candidate_id THEN
                RAISE EXCEPTION 'mutation admission identity is immutable';
            END IF;
            IF NEW.control_revision <> OLD.control_revision + 1 THEN
                RAISE EXCEPTION 'mutation admission revision must advance exactly once';
            END IF;
            IF OLD.state <> 'closed' OR NEW.state <> 'open' THEN
                RAISE EXCEPTION 'mutation admission may open exactly once';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
