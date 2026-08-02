"""Fail-closed candidate admission and mandatory command outbox authority.

Revision ID: 0008_fail_closed_admission_outbox
Revises: 0007_cutover_evidence_gates
"""
from __future__ import annotations

from alembic import op

revision = "0008_fail_closed_admission_outbox"
down_revision = "0007_cutover_evidence_gates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_require_open_mutation_admission()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM release_candidates rc
                 WHERE rc.generation_id = NEW.generation_id
            ) AND NOT EXISTS (
                SELECT 1 FROM mutation_admission_controls mac
                 WHERE mac.generation_id = NEW.generation_id
                   AND mac.state = 'open'
            ) THEN
                RAISE EXCEPTION 'PostgreSQL mutation admission is closed';
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
        $$
        """
    )
