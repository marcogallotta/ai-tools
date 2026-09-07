"""Bind DishState placement to native Sections before the runtime root switch."""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import context, op

revision = "0051_native_dish_state_placement"
down_revision = "0050_native_catalog_runtime_authority_switch"
branch_labels = None
depends_on = None


def _postgresql_scalar_guard(*, native: bool) -> str:
    unchanged_placement = (
        "NEW.section_id IS DISTINCT FROM OLD.section_id OR "
        "NEW.registry_version_id IS DISTINCT FROM OLD.registry_version_id"
    )
    placement_guard = (
        """
          IF NOT ((NEW.catalog_version_id IS NULL AND EXISTS (
                SELECT 1 FROM section_registry_entries e
                 WHERE e.registry_version_id=NEW.registry_version_id
                   AND (NEW.section_id IS NULL OR e.section_id=NEW.section_id)))
              OR (NEW.catalog_version_id IS NOT NULL AND NEW.section_id IS NOT NULL AND EXISTS (
                SELECT 1 FROM section_catalog_entries e
                 WHERE e.catalog_version_id=NEW.catalog_version_id
                   AND e.section_id=NEW.section_id)))
          THEN RAISE EXCEPTION 'DishState placement is absent from its authority catalog'; END IF;
        """
        if native
        else """
          IF NOT EXISTS (SELECT 1 FROM section_registry_entries e
              WHERE e.registry_version_id=NEW.registry_version_id
                AND (NEW.section_id IS NULL OR e.section_id=NEW.section_id))
          THEN RAISE EXCEPTION 'DishState placement is absent from registry'; END IF;
        """
    )
    if native:
        unchanged_placement += (
            " OR NEW.catalog_version_id IS DISTINCT FROM OLD.catalog_version_id"
        )
    return f"""
        CREATE OR REPLACE FUNCTION dish_validate_scalar_state()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE receipt dish_mutation_receipts%ROWTYPE;
        DECLARE content task_content_versions%ROWTYPE;
        DECLARE generation_reason text;
        BEGIN
          SELECT * INTO receipt FROM dish_mutation_receipts
           WHERE generation_id=NEW.generation_id AND task_id=NEW.task_id
             AND dish_version=NEW.dish_version;
          IF receipt.dish_version IS NULL THEN RAISE EXCEPTION 'DishState receipt missing'; END IF;
          IF TG_OP='UPDATE' THEN
            IF NEW.dish_version <> OLD.dish_version + 1
               OR receipt.content_changed <> (NEW.current_content_version_id IS DISTINCT FROM OLD.current_content_version_id)
               OR receipt.placement_changed <> (NEW.placement_version IS DISTINCT FROM OLD.placement_version)
               OR receipt.completion_changed <> (NEW.completion_version IS DISTINCT FROM OLD.completion_version)
               OR receipt.archive_changed <> (NEW.archived_at IS DISTINCT FROM OLD.archived_at)
               OR (receipt.archive_changed AND receipt.source_route <> 'command_execution')
               OR (NOT receipt.placement_changed AND ({unchanged_placement}))
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
          {placement_guard}
          RETURN NEW;
        END; $$
    """


def _replace_postgresql_guards(*, native: bool) -> None:
    op.execute(_postgresql_scalar_guard(native=native))
    if native:
        predicate = "s.catalog_version_id IS NULL AND s.registry_version_id <> a.registry_version_id"
    else:
        predicate = "s.registry_version_id <> a.registry_version_id"
    op.execute(f"""
        CREATE OR REPLACE FUNCTION dish_validate_active_registry_bindings()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM dish_states s JOIN active_section_registries a USING (generation_id)
             WHERE {predicate}
          ) THEN RAISE EXCEPTION 'DishState registry binding is not active'; END IF;
          RETURN NULL;
        END; $$
    """)


_SQLITE_LEGACY_PLACEMENT = re.compile(
    r"NOT EXISTS\s*\(\s*SELECT 1 FROM section_registry_entries e\b.*?"
    r"AND\s+\(NEW\.section_id IS NULL OR e\.section_id=NEW\.section_id\)\s*\)",
    re.DOTALL,
)
_SQLITE_UNCHANGED_PLACEMENT = re.compile(
    r"NEW\.section_id IS NOT OLD\.section_id OR\s+"
    r"NEW\.registry_version_id\s*<>\s*OLD\.registry_version_id"
)
_SQLITE_NATIVE_PLACEMENT = (
    "NOT ((NEW.catalog_version_id IS NULL AND EXISTS (SELECT 1 FROM section_registry_entries e "
    "WHERE e.registry_version_id=NEW.registry_version_id "
    "AND (NEW.section_id IS NULL OR e.section_id=NEW.section_id))) OR "
    "(NEW.catalog_version_id IS NOT NULL AND NEW.section_id IS NOT NULL AND EXISTS ("
    "SELECT 1 FROM section_catalog_entries e WHERE e.catalog_version_id=NEW.catalog_version_id "
    "AND e.section_id=NEW.section_id)))"
)


def _replace_sqlite_guards(*, native: bool) -> None:
    connection = op.get_bind()
    for name in ("dish_states_validate_insert", "dish_states_validate_update"):
        sql = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)
        ).scalar_one_or_none()
        if sql is None:
            if native:
                raise RuntimeError(f"{name} is required for native placement migration")
            continue
        if native:
            sql, placement_count = _SQLITE_LEGACY_PLACEMENT.subn(
                _SQLITE_NATIVE_PLACEMENT, sql
            )
            if name == "dish_states_validate_update":
                sql, transition_count = _SQLITE_UNCHANGED_PLACEMENT.subn(
                    "NEW.section_id IS NOT OLD.section_id OR "
                    "NEW.registry_version_id<>OLD.registry_version_id OR "
                    "NEW.catalog_version_id IS NOT OLD.catalog_version_id",
                    sql,
                )
            else:
                transition_count = 1
        else:
            if sql.count(_SQLITE_NATIVE_PLACEMENT) != 1:
                placement_count = 0
            else:
                sql = sql.replace(
                    _SQLITE_NATIVE_PLACEMENT,
                    "NOT EXISTS (SELECT 1 FROM section_registry_entries e "
                    "WHERE e.registry_version_id=NEW.registry_version_id "
                    "AND (NEW.section_id IS NULL OR e.section_id=NEW.section_id))",
                )
                placement_count = 1
            if name == "dish_states_validate_update":
                native_transition = (
                    "NEW.section_id IS NOT OLD.section_id OR "
                    "NEW.registry_version_id<>OLD.registry_version_id OR "
                    "NEW.catalog_version_id IS NOT OLD.catalog_version_id"
                )
                transition_count = sql.count(native_transition)
                sql = sql.replace(
                    native_transition,
                    "NEW.section_id IS NOT OLD.section_id OR "
                    "NEW.registry_version_id<>OLD.registry_version_id",
                )
            else:
                transition_count = 1
        if placement_count != 1 or transition_count != 1:
            raise RuntimeError(f"{name} has an unexpected placement guard shape")
        connection.exec_driver_sql(f'DROP TRIGGER "{name}"')
        connection.exec_driver_sql(sql)


def _suspend_sqlite_triggers() -> tuple[str, ...]:
    connection = op.get_bind()
    rows = connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql LIKE '%dish_states%'"
    ).all()
    for name, _sql in rows:
        connection.exec_driver_sql(f'DROP TRIGGER "{name.replace(chr(34), chr(34) * 2)}"')
    return tuple(sql for _name, sql in rows if sql)


def _restore_sqlite_triggers(statements: tuple[str, ...]) -> None:
    for statement in statements:
        op.get_bind().exec_driver_sql(statement)


def _rebind_foreign_key(*, native: bool) -> None:
    old_table = "governed_sections" if native else "sections"
    new_table = "sections" if native else "governed_sections"
    old_name = op.f(f"fk_dish_states_section_id_{old_table}")
    new_name = op.f(f"fk_dish_states_section_id_{new_table}")
    if context.is_offline_mode():
        op.drop_constraint(old_name, "dish_states", type_="foreignkey")
        op.create_foreign_key(
            new_name,
            "dish_states",
            new_table,
            ["section_id"],
            ["section_id"],
            ondelete="RESTRICT",
        )
        return
    if op.get_bind().dialect.name == "sqlite":
        triggers = _suspend_sqlite_triggers()
        try:
            with op.batch_alter_table("dish_states") as batch:
                batch.drop_constraint(old_name, type_="foreignkey")
                batch.create_foreign_key(
                    new_name,
                    new_table,
                    ["section_id"],
                    ["section_id"],
                    ondelete="RESTRICT",
                )
        finally:
            _restore_sqlite_triggers(triggers)
        return
    op.drop_constraint(old_name, "dish_states", type_="foreignkey")
    op.create_foreign_key(
        new_name,
        "dish_states",
        new_table,
        ["section_id"],
        ["section_id"],
        ondelete="RESTRICT",
    )


def _require_no_runtime_root() -> None:
    if context.is_offline_mode():
        return
    if op.get_bind().execute(
        sa.text("SELECT 1 FROM current_native_catalog_runtimes LIMIT 1")
    ).first():
        raise RuntimeError(
            "0051_native_dish_state_placement downgrade refuses established native runtime authority"
        )


def upgrade() -> None:
    _rebind_foreign_key(native=True)
    if context.is_offline_mode() or op.get_bind().dialect.name == "postgresql":
        _replace_postgresql_guards(native=True)
    else:
        _replace_sqlite_guards(native=True)


def downgrade() -> None:
    _require_no_runtime_root()
    if context.is_offline_mode() or op.get_bind().dialect.name == "postgresql":
        _replace_postgresql_guards(native=False)
    else:
        _replace_sqlite_guards(native=False)
    _rebind_foreign_key(native=False)
