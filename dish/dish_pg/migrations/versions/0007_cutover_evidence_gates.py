"""Typed deployment, worker-readiness, and first-admission evidence.

Revision ID: 0007_cutover_evidence_gates
Revises: 0006_final_asana_closure
"""
from __future__ import annotations

from alembic import op

from dish_pg.migrations.frozen_tables import (
    FROZEN_IMMUTABLE_TABLE_NAMES,
    create_frozen_tables,
    drop_frozen_tables,
)


revision = "0007_cutover_evidence_gates"
down_revision = "0006_final_asana_closure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    create_frozen_tables("0007_cutover_evidence_gates")
    if bind.dialect.name != "postgresql":
        return
    for table_name in FROZEN_IMMUTABLE_TABLE_NAMES["0007_cutover_evidence_gates"]:
        op.execute(
            f"CREATE TRIGGER {table_name}_immutable_update "
            f"BEFORE UPDATE ON {table_name} FOR EACH ROW "
            "EXECUTE FUNCTION dish_reject_immutable_release_evidence()"
        )
        op.execute(
            f"CREATE TRIGGER {table_name}_immutable_delete "
            f"BEFORE DELETE ON {table_name} FOR EACH ROW "
            "EXECUTE FUNCTION dish_reject_immutable_release_evidence()"
        )


def downgrade() -> None:
    bind = op.get_bind()
    drop_frozen_tables("0007_cutover_evidence_gates")
