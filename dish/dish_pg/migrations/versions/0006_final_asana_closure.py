"""Final Asana closure and approval recertification authority.

Revision ID: 0006_final_asana_closure
Revises: 0005_release_cutover
"""
from __future__ import annotations

from alembic import op

from dish_pg.migrations.frozen_tables import (
    FROZEN_IMMUTABLE_TABLE_NAMES,
    create_frozen_tables,
    drop_frozen_tables,
)


revision = "0006_final_asana_closure"
down_revision = "0005_release_cutover"
branch_labels = None
depends_on = None



def upgrade() -> None:
    bind = op.get_bind()
    create_frozen_tables("0006_final_asana_closure")
    if bind.dialect.name != "postgresql":
        return
    for table_name in FROZEN_IMMUTABLE_TABLE_NAMES["0006_final_asana_closure"]:
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
    drop_frozen_tables("0006_final_asana_closure")
