"""Add immutable, version-bound cook log entries."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045_cook_log_entries"
down_revision = "0044_independent_archive"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cook_log_entries",
        sa.Column("log_id", sa.Uuid(), primary_key=True),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("content_version_id", sa.Uuid(), nullable=False),
        sa.Column("dish_version", sa.BigInteger(), nullable=False),
        sa.Column("command_execution_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("dish_version > 0", name=op.f("ck_cook_log_entries_positive_dish_version")),
        sa.CheckConstraint("length(trim(text)) > 0", name=op.f("ck_cook_log_entries_text_nonblank")),
        sa.CheckConstraint("length(text) <= 8000", name=op.f("ck_cook_log_entries_text_length")),
        sa.ForeignKeyConstraint(["generation_id"], ["authority_generations.generation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["dish_tasks.task_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["generation_id", "task_id", "content_version_id"],
            ["task_content_versions.generation_id", "task_content_versions.task_id", "task_content_versions.content_version_id"],
            name="fk_cook_log_exact_content", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id", "task_id", "dish_version"],
            ["dish_mutation_receipts.generation_id", "dish_mutation_receipts.task_id", "dish_mutation_receipts.dish_version"],
            name="fk_cook_log_exact_dish_version", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["command_execution_id", "generation_id", "task_id"],
            ["command_executions.execution_id", "command_executions.generation_id", "command_executions.task_id"],
            name="fk_cook_log_exact_execution", ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("command_execution_id", name=op.f("uq_cook_log_entries_command_execution_id")),
    )
    op.create_index("ix_cook_log_task_time", "cook_log_entries", ["generation_id", "task_id", "recorded_at", "log_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("CREATE TRIGGER cook_log_entries_immutable BEFORE UPDATE OR DELETE ON cook_log_entries FOR EACH ROW EXECUTE FUNCTION dish_reject_immutable_workflow_authority()")
    else:
        op.execute("CREATE TRIGGER cook_log_entries_immutable_update BEFORE UPDATE ON cook_log_entries BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END")
        op.execute("CREATE TRIGGER cook_log_entries_immutable_delete BEFORE DELETE ON cook_log_entries BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END")


def downgrade() -> None:
    op.drop_table("cook_log_entries")
