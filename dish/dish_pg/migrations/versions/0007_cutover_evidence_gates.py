"""Typed deployment, worker-readiness, and first-admission evidence.

Revision ID: 0007_cutover_evidence_gates
Revises: 0006_final_asana_closure
"""
from __future__ import annotations

from alembic import op

from dish_pg.models import Base
from dish_pg.stage6_models import STAGE8_IMMUTABLE_TABLE_NAMES, STAGE8_TABLE_NAMES

revision = "0007_cutover_evidence_gates"
down_revision = "0006_final_asana_closure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    for table_name in STAGE8_TABLE_NAMES:
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=False)
    if bind.dialect.name != "postgresql":
        return
    for table_name in STAGE8_IMMUTABLE_TABLE_NAMES:
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
    for table_name in reversed(STAGE8_TABLE_NAMES):
        Base.metadata.tables[table_name].drop(bind=bind, checkfirst=False)
