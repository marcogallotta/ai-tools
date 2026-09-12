"""Add native Section lifecycle catalog rebind bookkeeping."""

from __future__ import annotations

import re
import sqlalchemy as sa
from alembic import context, op

revision = "0053_native_section_lifecycle"
down_revision = "0052_dish_state_section_not_null"
branch_labels = None
depends_on = None

_LIFECYCLE_COMMANDS_SQL = "'create-section','rename-section','retire-section'"


def _postgresql_scalar_guard(*, catalog_rebind: bool) -> str:
    rebind_declarations = (
        "DECLARE catalog_rebind section_catalog_rebind_receipts%ROWTYPE;\n"
        "        DECLARE is_catalog_rebind boolean := false;"
        if catalog_rebind
        else ""
    )
    rebind_probe = (
        f"""
          IF TG_OP='UPDATE'
             AND NEW.generation_id = OLD.generation_id
             AND NEW.task_id = OLD.task_id
             AND NEW.catalog_version_id IS DISTINCT FROM OLD.catalog_version_id
             AND NEW.current_content_version_id IS NOT DISTINCT FROM OLD.current_content_version_id
             AND NEW.section_id IS NOT DISTINCT FROM OLD.section_id
             AND NEW.registry_version_id = OLD.registry_version_id
             AND NEW.completed = OLD.completed
             AND NEW.completion_reason = OLD.completion_reason
             AND NEW.archived_at IS NOT DISTINCT FROM OLD.archived_at
             AND NEW.dish_version = OLD.dish_version
             AND NEW.placement_version = OLD.placement_version
             AND NEW.completion_version = OLD.completion_version
          THEN
            SELECT cr.* INTO catalog_rebind
              FROM section_catalog_rebind_receipts cr
              JOIN command_executions ce
                ON ce.execution_id=cr.command_execution_id
               AND ce.generation_id=cr.generation_id
             WHERE cr.generation_id=NEW.generation_id
               AND cr.task_id=NEW.task_id
               AND cr.predecessor_catalog_version_id=OLD.catalog_version_id
               AND cr.catalog_version_id=NEW.catalog_version_id
               AND cr.section_id=NEW.section_id
               AND ce.status='claimed'
               AND ce.command_name IN ({_LIFECYCLE_COMMANDS_SQL});
            is_catalog_rebind := catalog_rebind.catalog_version_id IS NOT NULL;
          END IF;
        """
        if catalog_rebind
        else ""
    )
    transition_condition = "TG_OP='UPDATE' AND NOT is_catalog_rebind" if catalog_rebind else "TG_OP='UPDATE'"
    return f"""
        CREATE OR REPLACE FUNCTION dish_validate_scalar_state()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE receipt dish_mutation_receipts%ROWTYPE;
        DECLARE content task_content_versions%ROWTYPE;
        DECLARE generation_reason text;
        {rebind_declarations}
        BEGIN
          {rebind_probe}
          SELECT * INTO receipt FROM dish_mutation_receipts
           WHERE generation_id=NEW.generation_id AND task_id=NEW.task_id
             AND dish_version=NEW.dish_version;
          IF receipt.dish_version IS NULL THEN RAISE EXCEPTION 'DishState receipt missing'; END IF;
          IF {transition_condition} THEN
            IF NEW.dish_version <> OLD.dish_version + 1
               OR receipt.content_changed <> (NEW.current_content_version_id IS DISTINCT FROM OLD.current_content_version_id)
               OR receipt.placement_changed <> (NEW.placement_version IS DISTINCT FROM OLD.placement_version)
               OR receipt.completion_changed <> (NEW.completion_version IS DISTINCT FROM OLD.completion_version)
               OR receipt.archive_changed <> (NEW.archived_at IS DISTINCT FROM OLD.archived_at)
               OR (receipt.archive_changed AND receipt.source_route <> 'command_execution')
               OR (NOT receipt.placement_changed AND (NEW.section_id IS DISTINCT FROM OLD.section_id OR NEW.registry_version_id IS DISTINCT FROM OLD.registry_version_id OR NEW.catalog_version_id IS DISTINCT FROM OLD.catalog_version_id))
               OR (receipt.placement_changed AND NEW.placement_version <> NEW.dish_version)
               OR (NOT receipt.completion_changed AND (NEW.completed IS DISTINCT FROM OLD.completed
                   OR NEW.completion_reason IS DISTINCT FROM OLD.completion_reason))
               OR (receipt.completion_changed AND NEW.completion_version <> NEW.dish_version)
            THEN RAISE EXCEPTION 'invalid DishState transition'; END IF;
          END IF;
          SELECT * INTO content FROM task_content_versions
           WHERE generation_id=NEW.generation_id AND task_id=NEW.task_id
             AND content_version_id=NEW.current_content_version_id;
          IF content.content_version_id IS NULL THEN RAISE EXCEPTION 'DishState content missing'; END IF;
          IF TG_OP='INSERT' THEN
            SELECT creation_reason INTO generation_reason FROM authority_generations
             WHERE generation_id=NEW.generation_id;
            IF generation_reason IS DISTINCT FROM 'destructive_restore'
               AND (NEW.dish_version <> 1 OR NEW.placement_version <> 1
                 OR NEW.completion_version <> 1 OR content.created_dish_version <> 1)
            THEN RAISE EXCEPTION 'ordinary initial DishState must use version 1'; END IF;
            IF EXISTS (SELECT 1 FROM dish_mutation_receipts r
              WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id
                AND r.dish_version IN (NEW.dish_version, NEW.placement_version,
                  NEW.completion_version, content.created_dish_version)
                AND (r.content_changed IS DISTINCT FROM (r.dish_version=content.created_dish_version)
                  OR r.placement_changed IS DISTINCT FROM (r.dish_version=NEW.placement_version)
                  OR r.completion_changed IS DISTINCT FROM (r.dish_version=NEW.completion_version)
                  OR r.archive_changed))
            THEN RAISE EXCEPTION 'initial DishState receipt effects are not sparse-current'; END IF;
          END IF;
          IF TG_OP='UPDATE' AND receipt.content_changed
             AND content.created_dish_version <> NEW.dish_version
          THEN RAISE EXCEPTION 'DishState content occurrence is not current'; END IF;
          IF NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r
              WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id
                AND r.dish_version=content.created_dish_version AND r.content_changed
                AND ((r.source_route='import' AND content.creator_route='import'
                      AND r.import_run_id=content.import_run_id)
                  OR (r.source_route='command_execution'
                      AND content.creator_route='command_execution'
                      AND r.command_execution_id=content.command_execution_id)))
          THEN RAISE EXCEPTION 'DishState content receipt mismatch'; END IF;
          IF NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r
              WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id
                AND r.dish_version=NEW.placement_version AND r.placement_changed)
          THEN RAISE EXCEPTION 'DishState placement receipt mismatch'; END IF;
          IF NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r
              WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id
                AND r.dish_version=NEW.completion_version AND r.completion_changed
                AND ((r.source_route='import' AND NEW.completion_reason='imported')
                  OR (r.source_route='command_execution'
                    AND NEW.completion_reason IN ('cooked','archive','reopen_planning'))))
          THEN RAISE EXCEPTION 'DishState completion receipt mismatch'; END IF;
          IF NOT ((NEW.catalog_version_id IS NULL AND EXISTS (
                SELECT 1 FROM section_registry_entries e
                 WHERE e.registry_version_id=NEW.registry_version_id
                   AND (NEW.section_id IS NULL OR e.section_id=NEW.section_id)))
              OR (NEW.catalog_version_id IS NOT NULL AND NEW.section_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM section_catalog_entries e
                 WHERE e.catalog_version_id=NEW.catalog_version_id
                   AND e.section_id=NEW.section_id)))
          THEN RAISE EXCEPTION 'DishState placement is absent from its authority catalog'; END IF;
          RETURN NEW;
        END; $$
    """


def _sqlite_rebind_match() -> str:
    return (
        "(NEW.generation_id=OLD.generation_id AND NEW.task_id=OLD.task_id "
        "AND NEW.catalog_version_id IS NOT OLD.catalog_version_id "
        "AND NEW.current_content_version_id IS OLD.current_content_version_id "
        "AND NEW.section_id IS OLD.section_id "
        "AND NEW.registry_version_id=OLD.registry_version_id "
        "AND NEW.completed=OLD.completed "
        "AND NEW.completion_reason=OLD.completion_reason "
        "AND NEW.archived_at IS OLD.archived_at "
        "AND NEW.dish_version=OLD.dish_version "
        "AND NEW.placement_version=OLD.placement_version "
        "AND NEW.completion_version=OLD.completion_version "
        "AND EXISTS (SELECT 1 FROM section_catalog_rebind_receipts cr "
        "JOIN command_executions ce ON ce.execution_id=cr.command_execution_id "
        "AND ce.generation_id=cr.generation_id "
        "WHERE cr.generation_id=NEW.generation_id AND cr.task_id=NEW.task_id "
        "AND cr.predecessor_catalog_version_id=OLD.catalog_version_id "
        "AND cr.catalog_version_id=NEW.catalog_version_id "
        "AND cr.section_id=NEW.section_id AND ce.status='claimed' "
        f"AND ce.command_name IN ({_LIFECYCLE_COMMANDS_SQL})))"
    )


def _replace_sqlite_update_guard(*, catalog_rebind: bool) -> None:
    connection = op.get_bind()
    name = "dish_states_validate_update"
    sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
    ).scalar_one_or_none()
    if sql is None:
        raise RuntimeError(f"{name} is required for native Section lifecycle migration")
    match = re.search(
        r"(BEFORE\s+UPDATE\s+ON\s+dish_states\s+WHEN\s*)(.*?)(\s*BEGIN\s+SELECT\s+RAISE\(ABORT,\s*'invalid DishState transition'\);\s*END)",
        sql,
        re.DOTALL | re.IGNORECASE,
    )
    if match is None:
        raise RuntimeError(f"{name} has an unexpected scalar guard shape")
    guard = _sqlite_rebind_match()
    wrapper = f"NOT {guard} AND ("
    predicate = match.group(2).strip()
    if catalog_rebind:
        if predicate.startswith(wrapper) and predicate.endswith(")"):
            return
        revised = wrapper + predicate + ")"
    else:
        if not predicate.startswith(wrapper) or not predicate.endswith(")"):
            raise RuntimeError(f"{name} has no lifecycle catalog-rebind wrapper")
        revised = predicate[len(wrapper) : -1].strip()
    sql = sql[: match.start(2)] + revised + sql[match.end(2) :]
    connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
    connection.exec_driver_sql(sql)


def _create_rebind_table() -> None:
    op.create_table(
        "section_catalog_rebind_receipts",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("predecessor_catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("command_execution_id", sa.Uuid(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "catalog_version_id <> predecessor_catalog_version_id",
            name=op.f("ck_section_catalog_rebind_receipts_catalog_changes"),
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["authority_generations.generation_id"],
            name=op.f("fk_section_catalog_rebind_receipts_generation_id_authority_generations"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["dish_tasks.task_id"],
            name=op.f("fk_section_catalog_rebind_receipts_task_id_dish_tasks"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["command_execution_id", "generation_id"],
            ["command_executions.execution_id", "command_executions.generation_id"],
            name="fk_section_catalog_rebind_exact_execution",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["predecessor_catalog_version_id", "section_id"],
            ["section_catalog_entries.catalog_version_id", "section_catalog_entries.section_id"],
            name="fk_section_catalog_rebind_predecessor_entry",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_version_id", "section_id"],
            ["section_catalog_entries.catalog_version_id", "section_catalog_entries.section_id"],
            name="fk_section_catalog_rebind_successor_entry",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "generation_id",
            "task_id",
            "catalog_version_id",
            name=op.f("pk_section_catalog_rebind_receipts"),
        ),
        sa.UniqueConstraint(
            "command_execution_id",
            "task_id",
            name="uq_section_catalog_rebind_execution_task",
        ),
    )


def _require_no_rebind_history() -> None:
    if context.is_offline_mode():
        return
    if op.get_bind().execute(
        sa.text("SELECT 1 FROM section_catalog_rebind_receipts LIMIT 1")
    ).first():
        raise RuntimeError(
            "0053_native_section_lifecycle downgrade refuses durable catalog-rebind history"
        )


def upgrade() -> None:
    _create_rebind_table()
    if context.is_offline_mode() or op.get_bind().dialect.name == "postgresql":
        op.execute(_postgresql_scalar_guard(catalog_rebind=True))
    else:
        _replace_sqlite_update_guard(catalog_rebind=True)


def downgrade() -> None:
    _require_no_rebind_history()
    if context.is_offline_mode() or op.get_bind().dialect.name == "postgresql":
        op.execute(_postgresql_scalar_guard(catalog_rebind=False))
    else:
        _replace_sqlite_update_guard(catalog_rebind=False)
    op.drop_table("section_catalog_rebind_receipts")
