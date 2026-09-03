"""Add task-scoped run revocations used by terminal archive."""
from __future__ import annotations
import sqlalchemy as sa
from alembic import op
revision = "0046_task_run_revocations"
down_revision = "0045_cook_log_entries"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.create_table(
        "task_run_revocations",
        sa.Column("revocation_id", sa.Uuid(), primary_key=True),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("archive_execution_id", sa.Uuid(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("length(trim(reason)) > 0", name=op.f("ck_task_run_revocations_reason_nonblank")),
        sa.ForeignKeyConstraint(["generation_id"], ["authority_generations.generation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["dish_tasks.task_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["run_id"], ["service_runs.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["archive_execution_id"], ["command_executions.execution_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("generation_id", "task_id", "run_id", name=op.f("uq_task_run_revocations_uq_task_run_revocation")),
    )
    op.create_index("ix_task_run_revocations_task", "task_run_revocations", ["generation_id", "task_id", "run_id"])

def downgrade() -> None:
    op.drop_table("task_run_revocations")
