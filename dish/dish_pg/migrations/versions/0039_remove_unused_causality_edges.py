"""Retire the unowned causality_edges persistence candidate.

Revision ID: 0039_remove_unused_causality_edges
Revises: 0038_cutover_rehearsal_identity
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0039_remove_unused_causality_edges"
down_revision = "0038_cutover_rehearsal_identity"
branch_labels = None
depends_on = None


def _install_postgresql_immutability() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        "CREATE TRIGGER causality_edges_immutable_update "
        "BEFORE UPDATE ON causality_edges FOR EACH ROW "
        "EXECUTE FUNCTION dish_reject_immutable_workflow_authority()"
    )
    op.execute(
        "CREATE TRIGGER causality_edges_immutable_delete "
        "BEFORE DELETE ON causality_edges FOR EACH ROW "
        "EXECUTE FUNCTION dish_reject_immutable_workflow_authority()"
    )


def _assert_causality_edges_empty() -> None:
    context = op.get_context()
    if context.as_sql:
        if context.dialect.name != "postgresql":
            raise RuntimeError(
                "causality edge retirement refuses offline rendering for non-PostgreSQL "
                "dialects because the forensic preflight cannot be enforced there"
            )
        op.execute(
            sa.text(
                """
                DO $$
                BEGIN
                    IF EXISTS (SELECT 1 FROM causality_edges) THEN
                        RAISE EXCEPTION
                            'refusing to drop non-empty causality_edges; unexpected forensic evidence requires review';
                    END IF;
                END
                $$
                """
            )
        )
        return

    unexpected = op.get_bind().execute(
        sa.text("SELECT 1 FROM causality_edges LIMIT 1")
    ).first()
    if unexpected is not None:
        raise RuntimeError(
            "refusing to drop non-empty causality_edges; unexpected forensic evidence requires review"
        )


def upgrade() -> None:
    _assert_causality_edges_empty()
    op.drop_table("causality_edges")


def downgrade() -> None:
    op.create_table(
        "causality_edges",
        sa.Column("causality_edge_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("command_execution_id", sa.Uuid(), nullable=True),
        sa.Column("cause_type", sa.String(length=64), nullable=False),
        sa.Column("cause_id", sa.String(length=128), nullable=False),
        sa.Column("effect_type", sa.String(length=64), nullable=False),
        sa.Column("effect_id", sa.String(length=128), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["command_execution_id"],
            ["command_executions.execution_id"],
            name=op.f("fk_causality_edges_command_execution_id_command_executions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["authority_generations.generation_id"],
            name=op.f("fk_causality_edges_generation_id_authority_generations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["request_id"],
            ["service_requests.request_id"],
            name=op.f("fk_causality_edges_request_id_service_requests"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("causality_edge_id", name=op.f("pk_causality_edges")),
        sa.UniqueConstraint(
            "generation_id",
            "cause_type",
            "cause_id",
            "effect_type",
            "effect_id",
            name="uq_causality_edge_identity",
        ),
    )
    _install_postgresql_immutability()
