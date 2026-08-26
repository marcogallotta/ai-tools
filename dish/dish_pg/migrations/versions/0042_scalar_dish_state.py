"""Replace split scalar task authority with typed DishState.

Revision ID: 0042_scalar_dish_state
Revises: 0041_test_generation_rollover
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

from dish_pg.migrations.frozen_tables import FROZEN_CREATE_SQL


revision = "0042_scalar_dish_state"
down_revision = "0041_test_generation_rollover"
branch_labels = None
depends_on = None


def _require_empty_authority() -> None:
    if context.is_offline_mode():
        return
    count = int(op.get_bind().exec_driver_sql("SELECT count(*) FROM authority_generations").scalar_one())
    if count:
        raise RuntimeError(
            "0042_scalar_dish_state requires an empty authority_generations table; "
            "use generation rollover/recovery rather than an in-place authority rewrite"
        )


def _create_receipts() -> None:
    op.create_table(
        "dish_mutation_receipts",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("dish_version", sa.BigInteger(), nullable=False),
        sa.Column("source_route", sa.String(24), nullable=False),
        sa.Column("import_run_id", sa.Uuid(), nullable=True),
        sa.Column("command_execution_id", sa.Uuid(), nullable=True),
        sa.Column("content_changed", sa.Boolean(), nullable=False),
        sa.Column("placement_changed", sa.Boolean(), nullable=False),
        sa.Column("completion_changed", sa.Boolean(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("generation_id", "task_id", "dish_version"),
        sa.ForeignKeyConstraint(["generation_id"], ["authority_generations.generation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["dish_tasks.task_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["import_run_id"], ["stage_a_import_runs.import_run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["command_execution_id", "generation_id", "task_id"],
            ["command_executions.execution_id", "command_executions.generation_id", "command_executions.task_id"],
            name="fk_dish_mutation_receipt_exact_execution",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("dish_version > 0", name="positive_dish_version"),
        sa.CheckConstraint("source_route IN ('import','command_execution')", name="source_route_allowed"),
        sa.CheckConstraint(
            "(source_route = 'import' AND import_run_id IS NOT NULL AND command_execution_id IS NULL) OR "
            "(source_route = 'command_execution' AND import_run_id IS NULL AND command_execution_id IS NOT NULL)",
            name="exact_source",
        ),
        sa.CheckConstraint(
            "content_changed OR placement_changed OR completion_changed",
            name="at_least_one_effect",
        ),
    )
    op.create_index(
        "uq_dish_mutation_receipt_execution",
        "dish_mutation_receipts",
        ["command_execution_id"],
        unique=True,
        postgresql_where=sa.text("command_execution_id IS NOT NULL"),
        sqlite_where=sa.text("command_execution_id IS NOT NULL"),
    )


def _create_scalar_heads() -> None:
    op.create_table(
        "dish_states",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("current_content_version_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=True),
        sa.Column("registry_version_id", sa.Uuid(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("completion_reason", sa.String(32), nullable=False),
        sa.Column("dish_version", sa.BigInteger(), nullable=False),
        sa.Column("placement_version", sa.BigInteger(), nullable=False),
        sa.Column("completion_version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("generation_id", "task_id"),
        sa.ForeignKeyConstraint(["generation_id"], ["authority_generations.generation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["dish_tasks.task_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["section_id"], ["governed_sections.section_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["registry_version_id"], ["section_registry_versions.registry_version_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["generation_id", "task_id", "current_content_version_id"],
            ["task_content_versions.generation_id", "task_content_versions.task_id", "task_content_versions.content_version_id"],
            name="fk_dish_state_exact_content",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id", "task_id", "dish_version"],
            ["dish_mutation_receipts.generation_id", "dish_mutation_receipts.task_id", "dish_mutation_receipts.dish_version"],
            name="fk_dish_state_current_receipt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id", "task_id", "placement_version"],
            ["dish_mutation_receipts.generation_id", "dish_mutation_receipts.task_id", "dish_mutation_receipts.dish_version"],
            name="fk_dish_state_placement_receipt",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id", "task_id", "completion_version"],
            ["dish_mutation_receipts.generation_id", "dish_mutation_receipts.task_id", "dish_mutation_receipts.dish_version"],
            name="fk_dish_state_completion_receipt",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("dish_version > 0", name="positive_dish_version"),
        sa.CheckConstraint("placement_version > 0", name="positive_placement_version"),
        sa.CheckConstraint("completion_version > 0", name="positive_completion_version"),
        sa.CheckConstraint("placement_version <= dish_version", name="placement_not_future"),
        sa.CheckConstraint("completion_version <= dish_version", name="completion_not_future"),
        sa.CheckConstraint(
            "completion_reason IN ('imported','cooked','archive','reopen_planning')",
            name="completion_reason_allowed",
        ),
    )
    op.create_index("ix_dish_states_section", "dish_states", ["generation_id", "section_id", "task_id"])
    op.create_index("ix_dish_states_board", "dish_states", ["generation_id", "completed", "section_id", "task_id"])
    op.create_index("ix_dish_states_registry", "dish_states", ["generation_id", "registry_version_id", "task_id"])
    op.create_table(
        "task_membership_heads",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("membership_revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("generation_id", "task_id"),
        sa.ForeignKeyConstraint(["generation_id"], ["authority_generations.generation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["dish_tasks.task_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("membership_revision >= 0", name="nonnegative_membership_revision"),
    )


def _create_downstream_tables() -> None:
    op.create_table(
        "task_execution_fences",
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("expected_dish_version", sa.BigInteger(), nullable=False),
        sa.Column("expected_membership_revision", sa.BigInteger(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("execution_id"),
        sa.ForeignKeyConstraint(["execution_id"], ["command_executions.execution_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["generation_id", "task_id"], ["dish_states.generation_id", "dish_states.task_id"],
            name="fk_task_execution_fence_dish_state", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["generation_id", "task_id"], ["task_membership_heads.generation_id", "task_membership_heads.task_id"],
            name="fk_task_execution_fence_membership_head", ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "expected_dish_version > 0 AND expected_membership_revision >= 0",
            name="valid_versions",
        ),
    )
    op.create_table(
        "verification_inspection_occurrences",
        sa.Column("inspection_id", sa.Uuid(), nullable=False),
        sa.Column("cycle_id", sa.Uuid(), nullable=False),
        sa.Column("operation_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("reviewed_content_version_id", sa.Uuid(), nullable=False),
        sa.Column("verifier_actor_fact_id", sa.Uuid(), nullable=False),
        sa.Column("verifier_run_id", sa.Uuid(), nullable=False),
        sa.Column("attestation", sa.Text(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("registry_version_id", sa.Uuid(), nullable=False),
        sa.Column("placement_version", sa.BigInteger(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("command_execution_id", sa.Uuid(), nullable=False),
        sa.Column("inspected_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("inspection_id"),
        sa.ForeignKeyConstraint(["cycle_id"], ["verification_cycles.cycle_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["operation_id"], ["workflow_operations.operation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["generation_id"], ["authority_generations.generation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["dish_tasks.task_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reviewed_content_version_id"], ["task_content_versions.content_version_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["verifier_actor_fact_id"], ["operation_actor_facts.actor_fact_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["verifier_run_id"], ["service_runs.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["section_id"], ["governed_sections.section_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["registry_version_id"], ["section_registry_versions.registry_version_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["request_id"], ["service_requests.request_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["command_execution_id"], ["command_executions.execution_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["generation_id", "task_id", "placement_version"],
            ["dish_mutation_receipts.generation_id", "dish_mutation_receipts.task_id", "dish_mutation_receipts.dish_version"],
            name="fk_verification_inspection_placement_receipt", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("length(trim(attestation)) > 0", name="attestation_nonblank"),
        sa.CheckConstraint("placement_version > 0", name="positive_placement_version"),
        sa.UniqueConstraint("request_id"),
        sa.UniqueConstraint(
            "cycle_id", "reviewed_content_version_id", "verifier_actor_fact_id", "placement_version",
            name="uq_verification_inspection_identity",
        ),
    )
    op.create_table(
        "abandonment_attempts",
        sa.Column("abandonment_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("source_operation_id", sa.Uuid(), nullable=False),
        sa.Column("source_lease_id", sa.Uuid(), nullable=False),
        sa.Column("source_actor_attempt_sequence", sa.BigInteger(), nullable=False),
        sa.Column("source_cycle_id", sa.Uuid(), nullable=True),
        sa.Column("source_owner_id", sa.String(256), nullable=False),
        sa.Column("source_run_id", sa.Uuid(), nullable=False),
        sa.Column("baseline_content_version_id", sa.Uuid(), nullable=False),
        sa.Column("baseline_placement_version", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("command_execution_id", sa.Uuid(), nullable=False),
        sa.Column("successor_operation_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("abandonment_id"),
        sa.ForeignKeyConstraint(["generation_id"], ["authority_generations.generation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["dish_tasks.task_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_operation_id"], ["workflow_operations.operation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_lease_id"], ["service_leases.lease_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_cycle_id"], ["verification_cycles.cycle_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_run_id"], ["service_runs.run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["baseline_content_version_id"], ["task_content_versions.content_version_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["request_id"], ["service_requests.request_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["command_execution_id"], ["command_executions.execution_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["successor_operation_id"], ["workflow_operations.operation_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["generation_id", "task_id", "baseline_placement_version"],
            ["dish_mutation_receipts.generation_id", "dish_mutation_receipts.task_id", "dish_mutation_receipts.dish_version"],
            name="fk_abandonment_baseline_placement_receipt", ondelete="RESTRICT",
        ),
        sa.CheckConstraint("source_actor_attempt_sequence > 0", name="positive_attempt_sequence"),
        sa.CheckConstraint("baseline_placement_version > 0", name="positive_baseline_placement_version"),
        sa.CheckConstraint(
            "state IN ('preparing','published','blocked','reconciling','completed','cancelled')",
            name="state_allowed",
        ),
        sa.CheckConstraint(
            "(state IN ('preparing','blocked','reconciling') AND successor_operation_id IS NULL AND terminal_at IS NULL) OR "
            "(state = 'published' AND successor_operation_id IS NOT NULL AND terminal_at IS NULL) OR "
            "(state = 'completed' AND successor_operation_id IS NOT NULL AND terminal_at IS NOT NULL) OR "
            "(state = 'cancelled' AND terminal_at IS NOT NULL)",
            name="state_payload_consistent",
        ),
    )
    op.create_index(
        "uq_abandonment_one_active_per_task", "abandonment_attempts", ["generation_id", "task_id"],
        unique=True,
        postgresql_where=sa.text("state IN ('preparing','published','blocked','reconciling')"),
        sqlite_where=sa.text("state IN ('preparing','published','blocked','reconciling')"),
    )


def _install_postgresql_guards() -> None:
    op.execute("DROP FUNCTION IF EXISTS dish_validate_task_head_pointer() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS dish_validate_current_placement() CASCADE")
    op.execute("DROP FUNCTION IF EXISTS dish_validate_current_completion() CASCADE")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_reject_scalar_authority_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
          RAISE EXCEPTION 'immutable scalar authority row: %', TG_TABLE_NAME;
        END; $$
        """
    )
    # task_content_versions keeps the immutability triggers installed by 0002 on
    # PostgreSQL. The other three tables are new or were dropped/recreated here.
    for table in (
        "dish_mutation_receipts",
        "task_execution_fences",
        "verification_inspection_occurrences",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION dish_reject_scalar_authority_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION dish_reject_scalar_authority_mutation()"
        )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_reject_scalar_identity_change()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
          IF TG_OP = 'DELETE' OR NEW.generation_id IS DISTINCT FROM OLD.generation_id
             OR NEW.task_id IS DISTINCT FROM OLD.task_id THEN
            RAISE EXCEPTION 'scalar authority identity is immutable';
          END IF;
          RETURN NEW;
        END; $$
        """
    )
    for table in ("dish_states", "task_membership_heads"):
        op.execute(
            f"CREATE TRIGGER {table}_identity_guard BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION dish_reject_scalar_identity_change()"
        )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_reject_creation_provenance_change()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
          IF NEW.task_id IS DISTINCT FROM OLD.task_id
             OR NEW.creation_route IS DISTINCT FROM OLD.creation_route
             OR NEW.import_run_id IS DISTINCT FROM OLD.import_run_id
             OR NEW.command_execution_id IS DISTINCT FROM OLD.command_execution_id
             OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'DishTask creation provenance is immutable';
          END IF;
          RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER dish_tasks_creation_provenance_immutable "
        "BEFORE UPDATE ON dish_tasks FOR EACH ROW "
        "EXECUTE FUNCTION dish_reject_creation_provenance_change()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_scalar_state()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE receipt dish_mutation_receipts%ROWTYPE;
        DECLARE content task_content_versions%ROWTYPE;
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
               OR (NOT receipt.placement_changed AND (NEW.section_id IS DISTINCT FROM OLD.section_id
                   OR NEW.registry_version_id IS DISTINCT FROM OLD.registry_version_id))
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
          IF TG_OP='UPDATE' AND receipt.content_changed
             AND content.created_dish_version <> NEW.dish_version
          THEN RAISE EXCEPTION 'DishState content occurrence is not current'; END IF;
          IF NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r
              WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id
                AND r.dish_version=content.created_dish_version AND r.content_changed
                AND ((r.source_route='import' AND content.creator_route='import' AND r.import_run_id=content.import_run_id)
                  OR (r.source_route='command_execution' AND content.creator_route='command_execution'
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
                  OR (r.source_route='command_execution' AND NEW.completion_reason IN ('cooked','archive','reopen_planning'))))
          THEN RAISE EXCEPTION 'DishState completion receipt mismatch'; END IF;
          IF NOT EXISTS (SELECT 1 FROM section_registry_entries e
              WHERE e.registry_version_id=NEW.registry_version_id
                AND (NEW.section_id IS NULL OR e.section_id=NEW.section_id))
          THEN RAISE EXCEPTION 'DishState placement is absent from registry'; END IF;
          RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER dish_states_validate BEFORE INSERT OR UPDATE ON dish_states "
        "FOR EACH ROW EXECUTE FUNCTION dish_validate_scalar_state()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_active_registry_bindings()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM dish_states s JOIN active_section_registries a USING (generation_id)
             WHERE s.registry_version_id <> a.registry_version_id
          ) THEN RAISE EXCEPTION 'DishState registry binding is not active'; END IF;
          RETURN NULL;
        END; $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER dish_states_active_registry_guard AFTER INSERT OR UPDATE ON dish_states "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION dish_validate_active_registry_bindings()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER active_registry_dish_states_guard AFTER INSERT OR UPDATE ON active_section_registries "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION dish_validate_active_registry_bindings()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_inspection_placement_occurrence()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
          IF NOT EXISTS (
            SELECT 1
              FROM dish_mutation_receipts r
              JOIN dish_states s
                ON s.generation_id=r.generation_id AND s.task_id=r.task_id
             WHERE r.generation_id=NEW.generation_id
               AND r.task_id=NEW.task_id
               AND r.dish_version=NEW.placement_version
               AND r.placement_changed
               AND s.placement_version=NEW.placement_version
               AND s.section_id=NEW.section_id
               AND s.registry_version_id=NEW.registry_version_id
          ) THEN
            RAISE EXCEPTION 'inspection placement occurrence is not current';
          END IF;
          RETURN NEW;
        END; $$
        """
    )
    op.execute(
        "CREATE TRIGGER verification_inspection_placement_guard "
        "BEFORE INSERT ON verification_inspection_occurrences FOR EACH ROW "
        "EXECUTE FUNCTION dish_validate_inspection_placement_occurrence()"
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
          SELECT status INTO generation_status FROM authority_generations
           WHERE generation_id=NEW.generation_id;
          IF generation_status IS DISTINCT FROM 'active' THEN
            RAISE EXCEPTION 'projection event requires active authority generation';
          END IF;
          IF NOT EXISTS (SELECT 1 FROM dish_states
              WHERE generation_id=NEW.generation_id AND task_id=NEW.task_id) THEN
            RAISE EXCEPTION 'projection event requires task authority in generation';
          END IF;
          IF NEW.source_route='command' THEN
            SELECT generation_id, task_id, status
              INTO execution_generation, execution_task, execution_status
              FROM command_executions WHERE execution_id=NEW.command_execution_id;
            IF execution_generation IS DISTINCT FROM NEW.generation_id
               OR execution_task IS DISTINCT FROM NEW.task_id
               OR execution_status NOT IN ('claimed','committed') THEN
              RAISE EXCEPTION 'projection event command execution mismatch';
            END IF;
          END IF;
          RETURN NEW;
        END; $$
        """
    )


def upgrade() -> None:
    _require_empty_authority()
    with op.batch_alter_table("command_executions") as batch:
        batch.create_unique_constraint(
            "uq_execution_generation_task", ["execution_id", "generation_id", "task_id"]
        )
    _create_receipts()

    # The empty-authority gate proves every dependent workflow table is empty, so replacing
    # these occurrence-bound shapes cannot discard live authority.
    op.drop_table("verification_inspection_occurrences")
    op.drop_table("abandonment_attempts")
    op.drop_table("task_execution_fences")
    op.drop_table("current_task_project_memberships")
    op.drop_table("current_task_section_placements")
    op.drop_table("current_task_completion")
    op.drop_table("task_authority_heads")
    op.drop_table("task_section_placement_events")
    op.drop_table("task_completion_events")
    op.drop_table("task_content_activations")

    with op.batch_alter_table("task_content_versions") as batch:
        batch.add_column(sa.Column("created_dish_version", sa.BigInteger(), nullable=False))
        batch.create_check_constraint("positive_created_dish_version", "created_dish_version > 0")
        batch.create_unique_constraint(
            "uq_content_created_dish_version", ["generation_id", "task_id", "created_dish_version"]
        )
        batch.create_foreign_key(
            "fk_content_version_creation_receipt",
            "dish_mutation_receipts",
            ["generation_id", "task_id", "created_dish_version"],
            ["generation_id", "task_id", "dish_version"],
            ondelete="RESTRICT",
        )
    _create_scalar_heads()
    op.create_table(
        "current_task_project_memberships",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("latest_event_id", sa.Uuid(), nullable=False),
        sa.Column("is_member", sa.Boolean(), nullable=False),
        sa.Column("membership_revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("generation_id", "task_id", "project_id"),
        sa.ForeignKeyConstraint(
            ["generation_id", "task_id"],
            ["task_membership_heads.generation_id", "task_membership_heads.task_id"],
            name="fk_current_membership_head", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["project_id"], ["governed_projects.project_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["latest_event_id"], ["task_project_membership_events.membership_event_id"], ondelete="RESTRICT"),
        sa.CheckConstraint("membership_revision > 0", name="positive_revision"),
        sa.UniqueConstraint("latest_event_id"),
    )
    _create_downstream_tables()
    if op.get_bind().dialect.name == "postgresql":
        _install_postgresql_guards()


def downgrade() -> None:
    _require_empty_authority()
    for table in (
        "verification_inspection_occurrences",
        "abandonment_attempts",
        "task_execution_fences",
        "current_task_project_memberships",
    ):
        op.drop_table(table)
    for table in ("task_membership_heads", "dish_states"):
        op.drop_table(table)
    with op.batch_alter_table("task_content_versions") as batch:
        batch.drop_constraint("fk_content_version_creation_receipt", type_="foreignkey")
        batch.drop_constraint("uq_content_created_dish_version", type_="unique")
        batch.drop_constraint("positive_created_dish_version", type_="check")
        batch.drop_column("created_dish_version")
    op.drop_table("dish_mutation_receipts")
    with op.batch_alter_table("command_executions") as batch:
        batch.drop_constraint("uq_execution_generation_task", type_="unique")

    # Empty-only downgrade: restore the released names and key shapes so the earlier
    # immutable migration chain can downgrade or upgrade normally without fabricating data.
    op.create_table(
        "task_completion_events",
        sa.Column("completion_event_id", sa.Uuid(), primary_key=True),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("completion_revision", sa.BigInteger(), nullable=False),
        sa.Column("provenance_route", sa.String(24), nullable=False),
        sa.Column("import_run_id", sa.Uuid()),
        sa.Column("command_execution_id", sa.Uuid()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "task_content_activations",
        sa.Column("content_activation_id", sa.Uuid(), primary_key=True),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("content_version_id", sa.Uuid(), nullable=False),
        sa.Column("activation_route", sa.String(24), nullable=False),
        sa.Column("import_run_id", sa.Uuid()),
        sa.Column("command_execution_id", sa.Uuid()),
        sa.Column("task_revision", sa.BigInteger(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "task_section_placement_events",
        sa.Column("placement_event_id", sa.Uuid(), primary_key=True),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid()),
        sa.Column("registry_version_id", sa.Uuid(), nullable=False),
        sa.Column("event_kind", sa.String(16), nullable=False),
        sa.Column("placement_revision", sa.BigInteger(), nullable=False),
        sa.Column("provenance_route", sa.String(24), nullable=False),
        sa.Column("import_run_id", sa.Uuid()),
        sa.Column("command_execution_id", sa.Uuid()),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "task_authority_heads",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("current_content_activation_id", sa.Uuid(), nullable=False),
        sa.Column("task_revision", sa.BigInteger(), nullable=False),
        sa.Column("membership_revision", sa.BigInteger(), nullable=False),
        sa.Column("placement_revision", sa.BigInteger(), nullable=False),
        sa.Column("completion_revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("generation_id", "task_id"),
    )
    op.create_table(
        "current_task_completion",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("completed", sa.Boolean(), nullable=False),
        sa.Column("latest_event_id", sa.Uuid(), nullable=False),
        sa.Column("completion_revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("generation_id", "task_id"),
    )
    op.create_table(
        "current_task_project_memberships",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("latest_event_id", sa.Uuid(), nullable=False),
        sa.Column("is_member", sa.Boolean(), nullable=False),
        sa.Column("membership_revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("generation_id", "task_id", "project_id"),
    )
    op.create_table(
        "current_task_section_placements",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid()),
        sa.Column("registry_version_id", sa.Uuid(), nullable=False),
        sa.Column("latest_event_id", sa.Uuid(), nullable=False),
        sa.Column("placement_revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("generation_id", "task_id"),
    )
    dialect = op.get_bind().dialect.name
    wanted = {
        "task_execution_fences",
        "verification_inspection_occurrences",
        "abandonment_attempts",
    }
    for statement in FROZEN_CREATE_SQL["0003_workflow_authority"][dialect]:
        if any(statement.startswith(f"CREATE TABLE {table} ") for table in wanted):
            op.execute(statement)
