"""Final Asana closure and approval recertification authority.

Revision ID: 0006_final_asana_closure
Revises: 0005_release_cutover
"""
from __future__ import annotations

from alembic import op

from dish_pg.models import Base
from dish_pg.stage6_models import STAGE7_IMMUTABLE_TABLE_NAMES, STAGE7_TABLE_NAMES

revision = "0006_final_asana_closure"
down_revision = "0005_release_cutover"
branch_labels = None
depends_on = None



def upgrade() -> None:
    bind = op.get_bind()
    for table_name in STAGE7_TABLE_NAMES:
        Base.metadata.tables[table_name].create(bind=bind, checkfirst=False)
    if bind.dialect.name != "postgresql":
        return
    for table_name in STAGE7_IMMUTABLE_TABLE_NAMES:
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
    for table_name in reversed(STAGE7_TABLE_NAMES):
        Base.metadata.tables[table_name].drop(bind=bind, checkfirst=False)
