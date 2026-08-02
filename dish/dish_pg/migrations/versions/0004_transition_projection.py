"""Stage 5 import, shadow, and downstream projection authority.

Revision ID: 0004_transition_projection
Revises: 0003_workflow_authority
"""
from __future__ import annotations

from alembic import op

from dish_pg.migrations.frozen_tables import (
    FROZEN_IMMUTABLE_TABLE_NAMES,
    create_frozen_tables,
    drop_frozen_tables,
)


revision = "0004_transition_projection"
down_revision = "0003_workflow_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    create_frozen_tables("0004_transition_projection")

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_reject_immutable_transition_authority()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'immutable transition authority row: %', TG_TABLE_NAME;
            END;
            $$
            """
        )
        for table_name in FROZEN_IMMUTABLE_TABLE_NAMES["0004_transition_projection"]:
            op.execute(
                f"CREATE TRIGGER {table_name}_immutable_update "
                f"BEFORE UPDATE ON {table_name} FOR EACH ROW "
                "EXECUTE FUNCTION dish_reject_immutable_transition_authority()"
            )
            op.execute(
                f"CREATE TRIGGER {table_name}_immutable_delete "
                f"BEFORE DELETE ON {table_name} FOR EACH ROW "
                "EXECUTE FUNCTION dish_reject_immutable_transition_authority()"
            )

        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_validate_projection_epoch_generation()
            RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE epoch_generation uuid;
            DECLARE epoch_status text;
            BEGIN
                SELECT generation_id, status INTO epoch_generation, epoch_status
                  FROM projection_epochs WHERE projection_epoch_id = NEW.projection_epoch_id;
                IF epoch_generation IS DISTINCT FROM NEW.generation_id OR epoch_status <> 'active' THEN
                    RAISE EXCEPTION 'projection event requires active epoch in same generation';
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            "CREATE TRIGGER projection_outbox_epoch_guard "
            "BEFORE INSERT ON projection_outbox_events FOR EACH ROW "
            "EXECUTE FUNCTION dish_validate_projection_epoch_generation()"
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_validate_projection_outbox_authority()
            RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE generation_status text;
            DECLARE execution_generation uuid;
            DECLARE execution_task uuid;
            DECLARE execution_status text;
            BEGIN
                SELECT status INTO generation_status
                  FROM authority_generations WHERE generation_id = NEW.generation_id;
                IF generation_status IS DISTINCT FROM 'active' THEN
                    RAISE EXCEPTION 'projection event requires active authority generation';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM task_authority_heads
                     WHERE generation_id = NEW.generation_id AND task_id = NEW.task_id
                ) THEN
                    RAISE EXCEPTION 'projection event requires task authority in generation';
                END IF;
                IF NEW.source_route = 'command' THEN
                    SELECT generation_id, task_id, status
                      INTO execution_generation, execution_task, execution_status
                      FROM command_executions WHERE execution_id = NEW.command_execution_id;
                    IF execution_generation IS DISTINCT FROM NEW.generation_id
                       OR execution_task IS DISTINCT FROM NEW.task_id
                       OR execution_status NOT IN ('claimed','committed') THEN
                        RAISE EXCEPTION 'projection event command execution mismatch';
                    END IF;
                END IF;
                RETURN NEW;
            END;
            $$
            """
        )
        op.execute(
            "CREATE TRIGGER projection_outbox_authority_guard "
            "BEFORE INSERT ON projection_outbox_events FOR EACH ROW "
            "EXECUTE FUNCTION dish_validate_projection_outbox_authority()"
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_reject_projection_outbox_identity_update()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'projection outbox identity is immutable';
            END;
            $$
            """
        )
        op.execute(
            "CREATE TRIGGER projection_outbox_identity_update "
            "BEFORE UPDATE OF generation_id, projection_epoch_id, source_route, "
            "command_execution_id, task_id, event_type, aggregate_sequence, "
            "idempotency_key, intent_payload, intent_sha256, created_at "
            "ON projection_outbox_events FOR EACH ROW "
            "EXECUTE FUNCTION dish_reject_projection_outbox_identity_update()"
        )

        mapping_specs = (
            ("project_projection_mappings", "project_id", "project_external_aliases"),
            ("section_projection_mappings", "section_id", "section_external_aliases"),
            ("task_projection_mappings", "task_id", "task_external_aliases"),
        )
        for table_name, entity_column, alias_table in mapping_specs:
            function_name = f"dish_validate_{table_name}_identity"
            op.execute(
                f"""
                CREATE OR REPLACE FUNCTION {function_name}()
                RETURNS trigger LANGUAGE plpgsql AS $$
                DECLARE alias_entity uuid;
                DECLARE alias_state text;
                DECLARE epoch_generation uuid;
                DECLARE epoch_status text;
                BEGIN
                    SELECT {entity_column}, state INTO alias_entity, alias_state
                      FROM {alias_table} WHERE alias_id = NEW.alias_id;
                    SELECT generation_id, status INTO epoch_generation, epoch_status
                      FROM projection_epochs
                     WHERE projection_epoch_id = NEW.projection_epoch_id;
                    IF alias_entity IS DISTINCT FROM NEW.{entity_column}
                       OR alias_state IS DISTINCT FROM 'active'
                       OR epoch_generation IS DISTINCT FROM NEW.generation_id
                       OR epoch_status IS DISTINCT FROM 'active' THEN
                        RAISE EXCEPTION 'projection mapping identity mismatch';
                    END IF;
                    RETURN NEW;
                END;
                $$
                """
            )
            op.execute(
                f"CREATE TRIGGER {table_name}_identity_insert "
                f"BEFORE INSERT ON {table_name} FOR EACH ROW "
                f"EXECUTE FUNCTION {function_name}()"
            )
            op.execute(
                f"CREATE TRIGGER {table_name}_identity_update "
                f"BEFORE UPDATE OF generation_id, projection_epoch_id, {entity_column}, alias_id "
                f"ON {table_name} FOR EACH ROW "
                "EXECUTE FUNCTION dish_reject_projection_outbox_identity_update()"
            )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for function_name in (
            "dish_validate_project_projection_mappings_identity",
            "dish_validate_section_projection_mappings_identity",
            "dish_validate_task_projection_mappings_identity",
            "dish_reject_projection_outbox_identity_update",
            "dish_validate_projection_outbox_authority",
            "dish_validate_projection_epoch_generation",
            "dish_reject_immutable_transition_authority",
        ):
            op.execute(f"DROP FUNCTION IF EXISTS {function_name}() CASCADE")
    drop_frozen_tables("0004_transition_projection")
