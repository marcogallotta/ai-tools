"""Enforce release and rehearsal chronology at the durable boundary.

Revision ID: 0010_release_chronology
Revises: 0009_candidate_replacement_control
"""
from __future__ import annotations

from alembic import op

revision = "0010_release_chronology"
down_revision = "0009_candidate_replacement_control"
branch_labels = None
depends_on = None

_CONSTRAINT = "ck_rehearsal_runs_completion_not_before_start"


def upgrade() -> None:
    with op.batch_alter_table("rehearsal_runs") as batch:
        batch.create_check_constraint(
            op.f(_CONSTRAINT), "completed_at IS NULL OR completed_at >= started_at"
        )


def downgrade() -> None:
    with op.batch_alter_table("rehearsal_runs") as batch:
        batch.drop_constraint(op.f(_CONSTRAINT), type_="check")
