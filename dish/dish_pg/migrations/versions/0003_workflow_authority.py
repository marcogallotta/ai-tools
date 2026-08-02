"""Stage 3 workflow, replay, recovery, and concurrency authority.

Revision ID: 0003_workflow_authority
Revises: 0002_core_authority_model
"""
from __future__ import annotations

from alembic import op

from dish_pg.migrations.frozen_tables import (
    FROZEN_IMMUTABLE_TABLE_NAMES,
    create_frozen_tables,
    drop_frozen_tables,
)


revision = "0003_workflow_authority"
down_revision = "0002_core_authority_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    create_frozen_tables("0003_workflow_authority")

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_reject_immutable_workflow_authority()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'immutable workflow authority row: %', TG_TABLE_NAME;
            END;
            $$
            """
        )
        for table_name in FROZEN_IMMUTABLE_TABLE_NAMES["0003_workflow_authority"]:
            op.execute(
                f"CREATE TRIGGER {table_name}_immutable_update "
                f"BEFORE UPDATE ON {table_name} FOR EACH ROW "
                "EXECUTE FUNCTION dish_reject_immutable_workflow_authority()"
            )
            op.execute(
                f"CREATE TRIGGER {table_name}_immutable_delete "
                f"BEFORE DELETE ON {table_name} FOR EACH ROW "
                "EXECUTE FUNCTION dish_reject_immutable_workflow_authority()"
            )

        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_validate_run_generation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE generation_status text;
            BEGIN
                SELECT status INTO generation_status
                  FROM authority_generations
                 WHERE generation_id = NEW.generation_id;
                IF generation_status IS DISTINCT FROM 'active' THEN
                    RAISE EXCEPTION 'run requires active authority generation';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            "CREATE TRIGGER service_runs_active_generation "
            "BEFORE INSERT ON service_runs FOR EACH ROW "
            "EXECUTE FUNCTION dish_validate_run_generation()"
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_validate_request_run_generation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE run_generation uuid;
            DECLARE run_status text;
            BEGIN
                SELECT generation_id, status INTO run_generation, run_status
                  FROM service_runs WHERE run_id = NEW.run_id;
                IF run_generation IS DISTINCT FROM NEW.generation_id OR run_status <> 'active' THEN
                    RAISE EXCEPTION 'request run is stale or belongs to another generation';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            "CREATE TRIGGER service_requests_generation_guard "
            "BEFORE INSERT ON service_requests FOR EACH ROW "
            "EXECUTE FUNCTION dish_validate_request_run_generation()"
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS dish_validate_request_run_generation() CASCADE")
        op.execute("DROP FUNCTION IF EXISTS dish_validate_run_generation() CASCADE")
        op.execute("DROP FUNCTION IF EXISTS dish_reject_immutable_workflow_authority() CASCADE")
    drop_frozen_tables("0003_workflow_authority")
