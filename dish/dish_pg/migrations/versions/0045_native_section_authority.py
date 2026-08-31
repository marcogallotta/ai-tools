"""Establish native Section/catalog authority beside frozen Asana topology evidence."""

from __future__ import annotations

import importlib

import sqlalchemy as sa
from alembic import context, op

revision = "0045_native_section_authority"
down_revision = "0044_independent_archive"
branch_labels = None
depends_on = None


def _candidate_transition_sql(*, native: bool) -> str:
    previous = importlib.import_module(
        "dish_pg.migrations.versions.0038_cutover_rehearsal_identity"
    )._CANDIDATE_0038
    if not native:
        return previous
    replacements = (
        (
            "            active_registry_version_id uuid;\n",
            (
                "            active_registry_version_id uuid;\n"
                "            active_catalog_version_id uuid;\n"
            ),
        ),
        (
            "               OR OLD.registry_version_id IS DISTINCT FROM NEW.registry_version_id\n",
            (
                "               OR OLD.registry_version_id IS DISTINCT FROM NEW.registry_version_id\n"
                "               OR OLD.catalog_version_id IS DISTINCT FROM NEW.catalog_version_id\n"
            ),
        ),
        (
            "                IF NEW.identity_contract_version IS DISTINCT FROM 'release-identity-v1'\n",
            (
                "                SELECT ac.catalog_version_id\n"
                "                  INTO active_catalog_version_id\n"
                "                  FROM active_section_catalogs ac\n"
                "                 WHERE ac.generation_id=NEW.generation_id\n"
                "                   FOR UPDATE;\n"
                "                IF NOT FOUND THEN\n"
                "                    RAISE EXCEPTION\n"
                "                        'candidate release transition requires exact active native catalog';\n"
                "                END IF;\n\n"
                "                IF NEW.identity_contract_version IS DISTINCT FROM 'release-identity-v1'\n"
            ),
        ),
        (
            "                   OR NEW.registry_version_id IS DISTINCT FROM active_registry_version_id\n",
            (
                "                   OR NEW.registry_version_id IS DISTINCT FROM active_registry_version_id\n"
                "                   OR NEW.catalog_version_id IS DISTINCT FROM active_catalog_version_id\n"
            ),
        ),
        (
            "                   OR manifest.registry_version_id IS DISTINCT FROM NEW.registry_version_id\n",
            (
                "                   OR manifest.registry_version_id IS DISTINCT FROM NEW.registry_version_id\n"
                "                   OR manifest.catalog_version_id IS DISTINCT FROM NEW.catalog_version_id\n"
            ),
        ),
        (
            "                   OR manifest.registry_version_id IS DISTINCT FROM active_registry_version_id\n",
            (
                "                   OR manifest.registry_version_id IS DISTINCT FROM active_registry_version_id\n"
                "                   OR manifest.catalog_version_id IS DISTINCT FROM active_catalog_version_id\n"
            ),
        ),
        (
            "                   OR NEW.registry_version_id <> manifest.registry_version_id\n",
            (
                "                   OR NEW.registry_version_id <> manifest.registry_version_id\n"
                "                   OR NEW.catalog_version_id <> manifest.catalog_version_id\n"
            ),
        ),
        (
            "                      JOIN honest_contract_bindings hb\n",
            (
                "                      JOIN active_section_catalogs ac\n"
                "                        ON ac.generation_id=manifest.generation_id\n"
                "                      JOIN honest_contract_bindings hb\n"
            ),
        ),
        (
            "                       AND ar.registry_version_id=manifest.registry_version_id\n",
            (
                "                       AND ar.registry_version_id=manifest.registry_version_id\n"
                "                       AND ac.catalog_version_id=manifest.catalog_version_id\n"
            ),
        ),
        ("manifest.manifest_version <> 4", "manifest.manifest_version <> 5"),
        ("forward manifest v4", "forward manifest v5"),
        ("b.manifest_version=4", "b.manifest_version=5"),
    )
    rendered = previous
    for old, new in replacements:
        count = rendered.count(old)
        expected = 2 if old in {"manifest.manifest_version <> 4", "forward manifest v4"} else 1
        if count != expected:
            raise RuntimeError(
                f"0045 candidate transition template drift for {old!r}: expected {expected}, got {count}"
            )
        rendered = rendered.replace(old, new)
    return rendered


def _sqlite_suspend_triggers_referencing(table_name: str) -> list[str]:
    bind = op.get_bind()
    if bind.dialect.name != "sqlite":
        return []
    rows = bind.execute(
        sa.text(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL "
            "AND (tbl_name=:table_name OR instr(sql, :table_name) > 0)"
        ),
        {"table_name": table_name},
    ).all()
    definitions: list[str] = []
    for name, sql in rows:
        escaped = str(name).replace('"', '""')
        op.execute(f'DROP TRIGGER IF EXISTS "{escaped}"')
        definitions.append(str(sql))
    return definitions


def _sqlite_restore_triggers(definitions: list[str]) -> None:
    for definition in definitions:
        op.execute(definition)


def _constraint_batch_mode() -> str:
    return "always" if op.get_bind().dialect.name == "sqlite" else "auto"


def _column(table: str, column: sa.Column, *, fk: tuple[str, str] | None = None) -> None:
    if op.get_bind().dialect.name == "sqlite":
        # All additions are nullable transition backfills.  SQLite's native ADD
        # COLUMN preserves the large cross-table trigger inventory; batch-copying
        # one authority table would temporarily invalidate those triggers.
        op.add_column(table, column)
        return
    with op.batch_alter_table(table) as batch:
        batch.add_column(column)
        if fk is not None:
            batch.create_foreign_key(
                op.f(f"fk_{table}_{column.name}_{fk[0]}"),
                fk[0],
                [column.name],
                [fk[1]],
                ondelete="RESTRICT",
            )


def _drop_column(table: str, column: str) -> None:
    if op.get_bind().dialect.name == "sqlite":
        op.execute(f'ALTER TABLE "{table}" DROP COLUMN "{column}"')
        return
    with op.batch_alter_table(table) as batch:
        batch.drop_column(column)


def upgrade() -> None:
    bind = op.get_bind()
    if not context.is_offline_mode():
        duplicate = bind.exec_driver_sql(
            "SELECT logical_name FROM governed_sections GROUP BY logical_name "
            "HAVING count(*) > 1 LIMIT 1"
        ).first()
        if duplicate is not None:
            raise RuntimeError(
                "0045_native_section_authority cannot collapse duplicate Project-scoped "
                f"Section name {duplicate[0]!r}; repair transition mapping first"
            )

    op.create_table(
        "sections",
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("logical_name", sa.String(256), nullable=False),
        sa.Column("lifecycle", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("lifecycle IN ('active','retired')", name=op.f("ck_sections_lifecycle_allowed")),
        sa.CheckConstraint(
            "(lifecycle = 'active' AND retired_at IS NULL) OR "
            "(lifecycle = 'retired' AND retired_at IS NOT NULL)",
            name=op.f("ck_sections_retirement_consistent"),
        ),
        sa.PrimaryKeyConstraint("section_id", name=op.f("pk_sections")),
        sa.UniqueConstraint("logical_name", name=op.f("uq_sections_logical_name")),
    )
    op.create_table(
        "section_catalog_versions",
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("version_number", sa.BigInteger(), nullable=False),
        sa.Column("contract_binding_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_sha256", sa.String(64), nullable=False),
        sa.Column("source_registry_version_id", sa.Uuid()),
        sa.Column("transform_sha256", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version_number > 0", name=op.f("ck_section_catalog_versions_positive_version")),
        sa.CheckConstraint("length(catalog_sha256) = 64", name=op.f("ck_section_catalog_versions_catalog_hash_length")),
        sa.CheckConstraint(
            "(source_registry_version_id IS NULL AND transform_sha256 IS NULL) OR "
            "(source_registry_version_id IS NOT NULL AND length(transform_sha256) = 64)",
            name=op.f("ck_section_catalog_versions_transition_transform_exact"),
        ),
        sa.ForeignKeyConstraint(["generation_id"], ["authority_generations.generation_id"], ondelete="RESTRICT", name=op.f("fk_section_catalog_versions_generation_id_authority_generations")),
        sa.ForeignKeyConstraint(["contract_binding_id"], ["honest_contract_bindings.binding_id"], ondelete="RESTRICT", name=op.f("fk_section_catalog_versions_contract_binding_id_honest_contract_bindings")),
        sa.ForeignKeyConstraint(["source_registry_version_id"], ["section_registry_versions.registry_version_id"], ondelete="RESTRICT", name=op.f("fk_section_catalog_versions_source_registry_version_id_section_registry_versions")),
        sa.PrimaryKeyConstraint("catalog_version_id", name=op.f("pk_section_catalog_versions")),
        sa.UniqueConstraint("generation_id", "version_number", name="uq_catalog_generation_version"),
    )
    op.create_table(
        "section_catalog_entries",
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("workflow_role", sa.String(64), nullable=False),
        sa.CheckConstraint("ordinal >= 0", name=op.f("ck_section_catalog_entries_nonnegative_ordinal")),
        sa.ForeignKeyConstraint(["catalog_version_id"], ["section_catalog_versions.catalog_version_id"], ondelete="CASCADE", name=op.f("fk_section_catalog_entries_catalog_version_id_section_catalog_versions")),
        sa.ForeignKeyConstraint(["section_id"], ["sections.section_id"], ondelete="RESTRICT", name=op.f("fk_section_catalog_entries_section_id_sections")),
        sa.PrimaryKeyConstraint("catalog_version_id", "section_id", name=op.f("pk_section_catalog_entries")),
        sa.UniqueConstraint("catalog_version_id", "ordinal", name="uq_catalog_entry_ordinal"),
        sa.UniqueConstraint("catalog_version_id", "workflow_role", name="uq_catalog_entry_workflow_role"),
    )
    op.create_table(
        "section_catalog_activations",
        sa.Column("catalog_activation_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("activation_route", sa.String(24), nullable=False),
        sa.Column("import_run_id", sa.Uuid()),
        sa.Column("command_execution_id", sa.Uuid()),
        sa.Column("catalog_revision", sa.BigInteger(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("activation_route IN ('transition','command_execution','recovery')", name=op.f("ck_section_catalog_activations_route_allowed")),
        sa.CheckConstraint(
            "(activation_route = 'transition' AND import_run_id IS NOT NULL AND command_execution_id IS NULL) OR "
            "(activation_route = 'command_execution' AND import_run_id IS NULL AND command_execution_id IS NOT NULL) OR "
            "(activation_route = 'recovery' AND import_run_id IS NULL AND command_execution_id IS NULL)",
            name=op.f("ck_section_catalog_activations_exact_provenance_route"),
        ),
        sa.CheckConstraint("catalog_revision > 0", name=op.f("ck_section_catalog_activations_positive_revision")),
        sa.ForeignKeyConstraint(["generation_id"], ["authority_generations.generation_id"], ondelete="RESTRICT", name=op.f("fk_section_catalog_activations_generation_id_authority_generations")),
        sa.ForeignKeyConstraint(["catalog_version_id"], ["section_catalog_versions.catalog_version_id"], ondelete="RESTRICT", name=op.f("fk_section_catalog_activations_catalog_version_id_section_catalog_versions")),
        sa.ForeignKeyConstraint(["import_run_id"], ["stage_a_import_runs.import_run_id"], ondelete="RESTRICT", name=op.f("fk_section_catalog_activations_import_run_id_stage_a_import_runs")),
        sa.PrimaryKeyConstraint("catalog_activation_id", name=op.f("pk_section_catalog_activations")),
        sa.UniqueConstraint("generation_id", "catalog_revision", name="uq_catalog_activation_revision"),
        sa.UniqueConstraint("generation_id", "catalog_version_id", name="uq_catalog_activation_version"),
    )
    op.create_table(
        "active_section_catalogs",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_activation_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("catalog_revision > 0", name=op.f("ck_active_section_catalogs_positive_revision")),
        sa.ForeignKeyConstraint(["generation_id"], ["authority_generations.generation_id"], ondelete="RESTRICT", name=op.f("fk_active_section_catalogs_generation_id_authority_generations")),
        sa.ForeignKeyConstraint(["catalog_version_id"], ["section_catalog_versions.catalog_version_id"], ondelete="RESTRICT", name=op.f("fk_active_section_catalogs_catalog_version_id_section_catalog_versions")),
        sa.ForeignKeyConstraint(["catalog_activation_id"], ["section_catalog_activations.catalog_activation_id"], ondelete="RESTRICT", name=op.f("fk_active_section_catalogs_catalog_activation_id_section_catalog_activations")),
        sa.PrimaryKeyConstraint("generation_id", name=op.f("pk_active_section_catalogs")),
        sa.UniqueConstraint("catalog_version_id", name=op.f("uq_active_section_catalogs_catalog_version_id")),
        sa.UniqueConstraint("catalog_activation_id", name=op.f("uq_active_section_catalogs_catalog_activation_id")),
    )
    op.create_table(
        "native_catalog_runtime_attestations",
        sa.Column("attestation_id", sa.Uuid(), nullable=False),
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_activation_id", sa.Uuid(), nullable=False),
        sa.Column("predecessor_attestation_id", sa.Uuid()),
        sa.Column("authority_activation_id", sa.Uuid()),
        sa.Column("attestation_revision", sa.BigInteger(), nullable=False),
        sa.Column("attestation_sha256", sa.String(64), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attestation_revision > 0", name=op.f("ck_native_catalog_runtime_attestations_positive_revision")),
        sa.CheckConstraint("length(attestation_sha256) = 64", name=op.f("ck_native_catalog_runtime_attestations_attestation_hash_length")),
        sa.CheckConstraint(
            "(attestation_revision = 1 AND predecessor_attestation_id IS NULL AND authority_activation_id IS NOT NULL) OR "
            "(attestation_revision > 1 AND predecessor_attestation_id IS NOT NULL AND authority_activation_id IS NULL)",
            name=op.f("ck_native_catalog_runtime_attestations_root_or_successor_exact"),
        ),
        sa.ForeignKeyConstraint(["generation_id"], ["authority_generations.generation_id"], ondelete="RESTRICT", name=op.f("fk_native_catalog_runtime_attestations_generation_id_authority_generations")),
        sa.ForeignKeyConstraint(["catalog_version_id"], ["section_catalog_versions.catalog_version_id"], ondelete="RESTRICT", name=op.f("fk_native_catalog_runtime_attestations_catalog_version_id_section_catalog_versions")),
        sa.ForeignKeyConstraint(["catalog_activation_id"], ["section_catalog_activations.catalog_activation_id"], ondelete="RESTRICT", name=op.f("fk_native_catalog_runtime_attestations_catalog_activation_id_section_catalog_activations")),
        sa.ForeignKeyConstraint(["predecessor_attestation_id"], ["native_catalog_runtime_attestations.attestation_id"], ondelete="RESTRICT", name=op.f("fk_native_catalog_runtime_attestations_predecessor_attestation_id_native_catalog_runtime_attestations")),
        sa.ForeignKeyConstraint(["authority_activation_id"], ["authority_activations.activation_id"], ondelete="RESTRICT", name=op.f("fk_native_catalog_runtime_attestations_authority_activation_id_authority_activations")),
        sa.PrimaryKeyConstraint("attestation_id", name=op.f("pk_native_catalog_runtime_attestations")),
        sa.UniqueConstraint("generation_id", "attestation_revision", name="uq_native_attestation_revision"),
        sa.UniqueConstraint("generation_id", "catalog_activation_id", name="uq_native_attestation_activation"),
    )
    op.create_table(
        "current_native_catalog_runtimes",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("attestation_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_activation_id", sa.Uuid(), nullable=False),
        sa.Column("attestation_revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attestation_revision > 0", name=op.f("ck_current_native_catalog_runtimes_positive_revision")),
        sa.ForeignKeyConstraint(["generation_id"], ["authority_generations.generation_id"], ondelete="RESTRICT", name=op.f("fk_current_native_catalog_runtimes_generation_id_authority_generations")),
        sa.ForeignKeyConstraint(["attestation_id"], ["native_catalog_runtime_attestations.attestation_id"], ondelete="RESTRICT", name=op.f("fk_current_native_catalog_runtimes_attestation_id_native_catalog_runtime_attestations")),
        sa.ForeignKeyConstraint(["catalog_version_id"], ["section_catalog_versions.catalog_version_id"], ondelete="RESTRICT", name=op.f("fk_current_native_catalog_runtimes_catalog_version_id_section_catalog_versions")),
        sa.ForeignKeyConstraint(["catalog_activation_id"], ["section_catalog_activations.catalog_activation_id"], ondelete="RESTRICT", name=op.f("fk_current_native_catalog_runtimes_catalog_activation_id_section_catalog_activations")),
        sa.PrimaryKeyConstraint("generation_id", name=op.f("pk_current_native_catalog_runtimes")),
        sa.UniqueConstraint("attestation_id", name=op.f("uq_current_native_catalog_runtimes_attestation_id")),
    )

    _column("dish_states", sa.Column("catalog_version_id", sa.Uuid()), fk=("section_catalog_versions", "catalog_version_id"))
    _column("task_execution_fences", sa.Column("expected_placement_version", sa.BigInteger()))
    _column("task_execution_fences", sa.Column("catalog_version_id", sa.Uuid()), fk=("section_catalog_versions", "catalog_version_id"))
    _column("workflow_operations", sa.Column("catalog_version_id", sa.Uuid()), fk=("section_catalog_versions", "catalog_version_id"))
    _column("verification_inspection_occurrences", sa.Column("catalog_version_id", sa.Uuid()), fk=("section_catalog_versions", "catalog_version_id"))
    _column("authority_activations", sa.Column("catalog_version_id", sa.Uuid()), fk=("section_catalog_versions", "catalog_version_id"))
    _column("release_candidates", sa.Column("catalog_version_id", sa.Uuid()), fk=("section_catalog_versions", "catalog_version_id"))
    _column("release_candidate_manifests", sa.Column("catalog_version_id", sa.Uuid()), fk=("section_catalog_versions", "catalog_version_id"))

    op.execute(
        "INSERT INTO sections(section_id,logical_name,lifecycle,created_at,retired_at) "
        "SELECT section_id,logical_name,lifecycle,created_at,retired_at FROM governed_sections"
    )
    op.execute(
        "INSERT INTO section_catalog_versions(catalog_version_id,generation_id,version_number,contract_binding_id,catalog_sha256,source_registry_version_id,transform_sha256,created_at) "
        "SELECT registry_version_id,generation_id,version_number,contract_binding_id,registry_sha256,registry_version_id,registry_sha256,created_at FROM section_registry_versions"
    )
    op.execute(
        "INSERT INTO section_catalog_entries(catalog_version_id,section_id,ordinal,display_name,workflow_role) "
        "SELECT registry_version_id,section_id,ordinal,display_name,workflow_role FROM section_registry_entries"
    )
    op.execute(
        "INSERT INTO section_catalog_activations(catalog_activation_id,generation_id,catalog_version_id,activation_route,import_run_id,command_execution_id,catalog_revision,activated_at) "
        "SELECT a.registry_activation_id,a.generation_id,a.registry_version_id,'transition',v.import_run_id,NULL,a.registry_revision,a.activated_at "
        "FROM section_registry_activations a JOIN section_registry_versions v ON v.registry_version_id=a.registry_version_id"
    )
    op.execute(
        "INSERT INTO active_section_catalogs(generation_id,catalog_version_id,catalog_activation_id,catalog_revision,updated_at) "
        "SELECT generation_id,registry_version_id,registry_activation_id,registry_revision,updated_at FROM active_section_registries"
    )
    op.execute("UPDATE dish_states SET catalog_version_id=registry_version_id")
    op.execute("UPDATE task_execution_fences SET expected_placement_version=(SELECT s.placement_version FROM dish_states s WHERE s.generation_id=task_execution_fences.generation_id AND s.task_id=task_execution_fences.task_id), catalog_version_id=(SELECT s.catalog_version_id FROM dish_states s WHERE s.generation_id=task_execution_fences.generation_id AND s.task_id=task_execution_fences.task_id)")
    op.execute("UPDATE workflow_operations SET catalog_version_id=(SELECT s.catalog_version_id FROM dish_states s WHERE s.generation_id=workflow_operations.generation_id AND s.task_id=workflow_operations.task_id)")
    op.execute("UPDATE verification_inspection_occurrences SET catalog_version_id=registry_version_id")
    op.execute("UPDATE authority_activations SET catalog_version_id=registry_version_id")
    op.execute("UPDATE release_candidates SET catalog_version_id=registry_version_id")
    op.execute("UPDATE release_candidate_manifests SET catalog_version_id=registry_version_id")
    candidate_triggers = _sqlite_suspend_triggers_referencing("release_candidates")
    with op.batch_alter_table("release_candidates", recreate=_constraint_batch_mode()) as batch:
        batch.drop_constraint(op.f("ck_release_candidates_identity_contract_complete"), type_="check")
        batch.create_check_constraint(
            "identity_contract_complete",
            "(identity_contract_version IS NULL AND source_manifest_sha256 IS NULL "
            "AND rehearsal_environment_identity IS NULL AND registry_version_id IS NULL "
            "AND catalog_version_id IS NULL AND honest_binding_id IS NULL) OR "
            "(identity_contract_version = 'release-identity-v1' "
            "AND source_manifest_sha256 IS NOT NULL AND length(source_manifest_sha256) = 64 "
            "AND rehearsal_environment_identity IS NOT NULL AND registry_version_id IS NOT NULL "
            "AND catalog_version_id IS NOT NULL AND honest_binding_id IS NOT NULL)",
        )
    _sqlite_restore_triggers(candidate_triggers)
    with op.batch_alter_table("release_candidate_manifests", recreate=_constraint_batch_mode()) as batch:
        batch.drop_constraint(op.f("ck_release_candidate_manifests_manifest_version_supported"), type_="check")
        batch.drop_constraint(op.f("ck_release_candidate_manifests_component_hash_lengths"), type_="check")
        batch.create_check_constraint("manifest_version_supported", "manifest_version IN (2, 3, 4, 5)")
        batch.create_check_constraint(
            "component_hash_lengths",
            "length(mapping_membership_sha256) = 64 AND length(import_completion_sha256) = 64 "
            "AND length(typed_import_linkage_sha256) = 64 AND length(reconciliation_evidence_sha256) = 64 "
            "AND ((manifest_version = 2 AND approval_reconciliation_run_id IS NULL "
            "AND readiness_inventory_sha256 IS NOT NULL AND length(readiness_inventory_sha256) = 64 "
            "AND readiness_completion_sha256 IS NOT NULL AND length(readiness_completion_sha256) = 64) "
            "OR (manifest_version IN (3, 4, 5) AND approval_reconciliation_run_id IS NOT NULL "
            "AND readiness_inventory_sha256 IS NULL AND readiness_completion_sha256 IS NULL))",
        )
    for table in ("cutover_approval_manifest_bindings", "candidate_manifest_revalidations"):
        with op.batch_alter_table(table, recreate=_constraint_batch_mode()) as batch:
            batch.drop_constraint(op.f(f"ck_{table}_manifest_version_supported"), type_="check")
            batch.create_check_constraint("manifest_version_supported", "manifest_version IN (2, 3, 4, 5)")
            if table == "candidate_manifest_revalidations":
                batch.drop_constraint(op.f("ck_candidate_manifest_revalidations_observed_component_hash_lengths"), type_="check")
                batch.create_check_constraint(
                    "observed_component_hash_lengths",
                    "length(observed_mapping_membership_sha256) = 64 AND length(observed_import_completion_sha256) = 64 "
                    "AND length(observed_typed_import_linkage_sha256) = 64 AND length(observed_reconciliation_evidence_sha256) = 64 "
                    "AND ((manifest_version = 2 AND observed_readiness_inventory_sha256 IS NOT NULL "
                    "AND length(observed_readiness_inventory_sha256) = 64 AND observed_readiness_completion_sha256 IS NOT NULL "
                    "AND length(observed_readiness_completion_sha256) = 64) OR (manifest_version IN (3, 4, 5) "
                    "AND observed_readiness_inventory_sha256 IS NULL AND observed_readiness_completion_sha256 IS NULL))",
                )
    op.create_index("ix_dish_states_catalog", "dish_states", ["generation_id", "catalog_version_id", "task_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_candidate_transition_sql(native=True))
        # The Asana-shaped registry remains immutable transition evidence, but
        # its active pointer no longer governs native Dish placement.
        op.execute("DROP TRIGGER IF EXISTS active_registry_dish_states_guard ON active_section_registries")
        op.execute("DROP TRIGGER IF EXISTS dish_states_active_registry_guard ON dish_states")
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_validate_native_catalog_placement()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
              IF NEW.catalog_version_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM section_catalog_entries e
                 WHERE e.catalog_version_id=NEW.catalog_version_id
                   AND e.section_id=NEW.section_id
              ) THEN
                RAISE EXCEPTION 'DishState placement is absent from native Section catalog';
              END IF;
              IF TG_OP='UPDATE' AND NEW.placement_version=OLD.placement_version
                 AND (NEW.catalog_version_id<>OLD.catalog_version_id
                      OR NEW.section_id<>OLD.section_id)
              THEN RAISE EXCEPTION 'DishState native placement changed without placement revision';
              END IF;
              RETURN NEW;
            END; $$
            """
        )
        op.execute(
            "CREATE TRIGGER dish_states_native_catalog_validate BEFORE INSERT OR UPDATE ON dish_states "
            "FOR EACH ROW EXECUTE FUNCTION dish_validate_native_catalog_placement()"
        )
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_validate_active_catalog_bindings()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
              IF EXISTS (
                SELECT 1 FROM dish_states s JOIN active_section_catalogs a USING (generation_id)
                 WHERE s.catalog_version_id <> a.catalog_version_id
              ) THEN RAISE EXCEPTION 'DishState native catalog binding is not active'; END IF;
              RETURN NULL;
            END; $$
            """
        )
        op.execute(
            "CREATE CONSTRAINT TRIGGER dish_states_active_catalog_guard AFTER INSERT OR UPDATE ON dish_states "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION dish_validate_active_catalog_bindings()"
        )
        op.execute(
            "CREATE CONSTRAINT TRIGGER active_catalog_dish_states_guard AFTER INSERT OR UPDATE ON active_section_catalogs "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION dish_validate_active_catalog_bindings()"
        )
    else:
        op.execute(
            "CREATE TRIGGER dish_states_native_catalog_validate_insert BEFORE INSERT ON dish_states WHEN "
            "NEW.catalog_version_id IS NULL OR NOT EXISTS (SELECT 1 FROM section_catalog_entries e "
            "WHERE e.catalog_version_id=NEW.catalog_version_id AND e.section_id=NEW.section_id) "
            "BEGIN SELECT RAISE(ABORT, 'DishState placement is absent from native Section catalog'); END"
        )
        op.execute(
            "CREATE TRIGGER dish_states_native_catalog_validate_update BEFORE UPDATE ON dish_states WHEN "
            "NEW.catalog_version_id IS NULL OR NOT EXISTS (SELECT 1 FROM section_catalog_entries e "
            "WHERE e.catalog_version_id=NEW.catalog_version_id AND e.section_id=NEW.section_id) OR "
            "EXISTS (SELECT 1 FROM active_section_catalogs a WHERE a.generation_id=NEW.generation_id "
            "AND a.catalog_version_id<>NEW.catalog_version_id) OR "
            "(NEW.placement_version=OLD.placement_version AND "
            "(NEW.catalog_version_id<>OLD.catalog_version_id OR NEW.section_id<>OLD.section_id)) "
            "BEGIN SELECT RAISE(ABORT, 'invalid DishState native catalog placement'); END"
        )
        op.execute(
            "CREATE TRIGGER dish_states_active_catalog_guard AFTER INSERT ON dish_states WHEN EXISTS ("
            "SELECT 1 FROM active_section_catalogs a WHERE a.generation_id=NEW.generation_id "
            "AND a.catalog_version_id<>NEW.catalog_version_id) "
            "BEGIN SELECT RAISE(ABORT, 'DishState native catalog binding is not active'); END"
        )


def downgrade() -> None:
    if not context.is_offline_mode():
        bind = op.get_bind()
        native = bind.exec_driver_sql(
            "SELECT 1 FROM native_catalog_runtime_attestations LIMIT 1"
        ).first()
        divergent = bind.exec_driver_sql(
            "SELECT 1 FROM section_catalog_versions WHERE source_registry_version_id IS NULL LIMIT 1"
        ).first()
        manifest_v5 = bind.exec_driver_sql(
            "SELECT 1 FROM release_candidate_manifests WHERE manifest_version = 5 LIMIT 1"
        ).first()
        if native is not None or divergent is not None or manifest_v5 is not None:
            raise RuntimeError(
                "0045_native_section_authority downgrade refuses native runtime/catalog history"
            )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_candidate_transition_sql(native=False))
        op.execute("DROP TRIGGER IF EXISTS active_catalog_dish_states_guard ON active_section_catalogs")
        op.execute("DROP TRIGGER IF EXISTS dish_states_active_catalog_guard ON dish_states")
        op.execute("DROP TRIGGER IF EXISTS dish_states_native_catalog_validate ON dish_states")
        op.execute("DROP FUNCTION IF EXISTS dish_validate_active_catalog_bindings()")
        op.execute("DROP FUNCTION IF EXISTS dish_validate_native_catalog_placement()")
        op.execute(
            "CREATE CONSTRAINT TRIGGER dish_states_active_registry_guard AFTER INSERT OR UPDATE ON dish_states "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION dish_validate_active_registry_bindings()"
        )
        op.execute(
            "CREATE CONSTRAINT TRIGGER active_registry_dish_states_guard AFTER INSERT OR UPDATE ON active_section_registries "
            "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION dish_validate_active_registry_bindings()"
        )
    else:
        op.execute("DROP TRIGGER IF EXISTS dish_states_active_catalog_guard")
        op.execute("DROP TRIGGER IF EXISTS dish_states_native_catalog_validate_update")
        op.execute("DROP TRIGGER IF EXISTS dish_states_native_catalog_validate_insert")
    op.drop_index("ix_dish_states_catalog", table_name="dish_states")
    for table in ("cutover_approval_manifest_bindings", "candidate_manifest_revalidations"):
        with op.batch_alter_table(table, recreate=_constraint_batch_mode()) as batch:
            batch.drop_constraint(op.f(f"ck_{table}_manifest_version_supported"), type_="check")
            batch.create_check_constraint("manifest_version_supported", "manifest_version IN (2, 3, 4)")
            if table == "candidate_manifest_revalidations":
                batch.drop_constraint(op.f("ck_candidate_manifest_revalidations_observed_component_hash_lengths"), type_="check")
                batch.create_check_constraint(
                    "observed_component_hash_lengths",
                    "length(observed_mapping_membership_sha256) = 64 AND length(observed_import_completion_sha256) = 64 "
                    "AND length(observed_typed_import_linkage_sha256) = 64 AND length(observed_reconciliation_evidence_sha256) = 64 "
                    "AND ((manifest_version = 2 AND observed_readiness_inventory_sha256 IS NOT NULL "
                    "AND length(observed_readiness_inventory_sha256) = 64 AND observed_readiness_completion_sha256 IS NOT NULL "
                    "AND length(observed_readiness_completion_sha256) = 64) OR (manifest_version IN (3, 4) "
                    "AND observed_readiness_inventory_sha256 IS NULL AND observed_readiness_completion_sha256 IS NULL))",
                )
    with op.batch_alter_table("release_candidate_manifests", recreate=_constraint_batch_mode()) as batch:
        batch.drop_constraint(op.f("ck_release_candidate_manifests_manifest_version_supported"), type_="check")
        batch.drop_constraint(op.f("ck_release_candidate_manifests_component_hash_lengths"), type_="check")
        batch.create_check_constraint("manifest_version_supported", "manifest_version IN (2, 3, 4)")
        batch.create_check_constraint(
            "component_hash_lengths",
            "length(mapping_membership_sha256) = 64 AND length(import_completion_sha256) = 64 "
            "AND length(typed_import_linkage_sha256) = 64 AND length(reconciliation_evidence_sha256) = 64 "
            "AND ((manifest_version = 2 AND approval_reconciliation_run_id IS NULL "
            "AND readiness_inventory_sha256 IS NOT NULL AND length(readiness_inventory_sha256) = 64 "
            "AND readiness_completion_sha256 IS NOT NULL AND length(readiness_completion_sha256) = 64) "
            "OR (manifest_version IN (3, 4) AND approval_reconciliation_run_id IS NOT NULL "
            "AND readiness_inventory_sha256 IS NULL AND readiness_completion_sha256 IS NULL))",
        )
    candidate_triggers = _sqlite_suspend_triggers_referencing("release_candidates")
    with op.batch_alter_table("release_candidates", recreate=_constraint_batch_mode()) as batch:
        batch.drop_constraint(op.f("ck_release_candidates_identity_contract_complete"), type_="check")
        batch.create_check_constraint(
            "identity_contract_complete",
            "(identity_contract_version IS NULL AND source_manifest_sha256 IS NULL "
            "AND rehearsal_environment_identity IS NULL AND registry_version_id IS NULL "
            "AND honest_binding_id IS NULL) OR (identity_contract_version = 'release-identity-v1' "
            "AND source_manifest_sha256 IS NOT NULL AND length(source_manifest_sha256) = 64 "
            "AND rehearsal_environment_identity IS NOT NULL AND registry_version_id IS NOT NULL "
            "AND honest_binding_id IS NOT NULL)",
        )
    _sqlite_restore_triggers(candidate_triggers)
    _drop_column("authority_activations", "catalog_version_id")
    _drop_column("release_candidate_manifests", "catalog_version_id")
    _drop_column("release_candidates", "catalog_version_id")
    _drop_column("verification_inspection_occurrences", "catalog_version_id")
    _drop_column("workflow_operations", "catalog_version_id")
    _drop_column("task_execution_fences", "catalog_version_id")
    _drop_column("task_execution_fences", "expected_placement_version")
    _drop_column("dish_states", "catalog_version_id")
    op.drop_table("current_native_catalog_runtimes")
    op.drop_table("native_catalog_runtime_attestations")
    op.drop_table("active_section_catalogs")
    op.drop_table("section_catalog_activations")
    op.drop_table("section_catalog_entries")
    op.drop_table("section_catalog_versions")
    op.drop_table("sections")
