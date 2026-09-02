"""Establish native Section/catalog authority beside frozen Asana topology evidence."""

from __future__ import annotations

import dataclasses
import importlib
import uuid

import sqlalchemy as sa
from alembic import context, op
from dish_pg.document_authority import parse_canonical_document, render_parts
from dish_tool._task_document_types import PlanningBrief
from dish_tool.content_versions import CONTENT_IDENTITY_SCHEME, content_identity

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


_CONTENT_TRANSFORM_NAMESPACE = uuid.UUID("1dc43577-e57f-5fe7-8b88-c284a36aa986")


def _uuid(value) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _derived_uuid(kind: str, source) -> uuid.UUID:
    return uuid.uuid5(_CONTENT_TRANSFORM_NAMESPACE, f"{kind}:{source}")


def _db_uuid(bind, value: uuid.UUID):
    return value.hex if bind.dialect.name == "sqlite" else value


def _carry_ready_documents_to_native_sections() -> None:
    """Create immutable native-destination occurrences and inherited signoffs."""
    bind = op.get_bind()
    unsettled = bind.execute(
        sa.text(
            "SELECT operation_id FROM workflow_operations "
            "WHERE lifecycle='open' AND phase<>'await_submission' LIMIT 1"
        )
    ).first()
    if unsettled is not None:
        raise RuntimeError(
            "0045_native_section_authority refuses pending/open workflow work; "
            "settle it before migration"
        )
    rows = bind.execute(
        sa.text(
            "SELECT s.generation_id,s.task_id,s.current_content_version_id,s.dish_version,"
            "s.catalog_version_id,v.title,v.body,v.contract_binding_id "
            "FROM dish_states s JOIN task_content_versions v "
            "ON v.generation_id=s.generation_id AND v.task_id=s.task_id "
            "AND v.content_version_id=s.current_content_version_id"
        )
    ).mappings()
    for row in rows:
        try:
            parts = parse_canonical_document(title=row["title"], body=row["body"])
        except ValueError:
            continue
        destination = parts.document.planning_brief.values["Destination section"]
        # Native destinations and non-destination placeholders need no rewrite.
        if " — section:" in destination or " — " not in destination:
            continue
        display_name, external_gid = destination.rsplit(" — ", 1)
        if not external_gid.isdigit():
            continue
        if parts.document.state.values["Status"] != "ready":
            raise RuntimeError(
                "0045_native_section_authority refuses a pending legacy-GID document; "
                "settle its workflow before migration"
            )
        aliases = bind.execute(
            sa.text(
                "SELECT a.section_id FROM section_external_aliases a "
                "WHERE a.external_system='asana' AND a.external_id=:gid AND a.state='active'"
            ),
            {"gid": external_gid},
        ).all()
        if len(aliases) != 1:
            raise RuntimeError(
                "0045_native_section_authority cannot resolve one native Section for "
                f"legacy destination {external_gid}"
            )
        section_id = _uuid(aliases[0][0])
        native_destination = f"{display_name} — section:{section_id}"
        planning = dict(parts.document.planning_brief.values)
        planning["Destination section"] = native_destination
        transformed = render_parts(
            dataclasses.replace(
                parts.document,
                planning_brief=PlanningBrief(planning),
            )
        )
        # The only open workflow safe to carry is an already approved occurrence
        # waiting for submit. Other live work must be settled before the migration.
        lineage = bind.execute(
            sa.text(
                "SELECT o.operation_id,o.phase,c.cycle_id,c.cycle_sequence,"
                "sg.signoff_id,sg.inspection_id,sg.verifier_actor_fact_id,"
                "sg.command_execution_id,sg.signed_at,i.verifier_run_id,i.attestation,"
                "i.section_id,i.registry_version_id,i.placement_version,e.request_id "
                "FROM workflow_operations o "
                "JOIN verification_cycles c ON c.operation_id=o.operation_id "
                "JOIN verification_signoffs sg ON sg.cycle_id=c.cycle_id "
                "JOIN verification_inspection_occurrences i ON i.inspection_id=sg.inspection_id "
                "JOIN command_executions e ON e.execution_id=sg.command_execution_id "
                "WHERE o.generation_id=:generation_id AND o.task_id=:task_id "
                "AND o.lifecycle='open' AND o.phase='await_submission' "
                "AND c.lifecycle='approved' AND sg.signed_content_version_id=:content_version_id "
                "ORDER BY c.cycle_sequence DESC LIMIT 1"
            ),
            {
                "generation_id": row["generation_id"],
                "task_id": row["task_id"],
                "content_version_id": row["current_content_version_id"],
            },
        ).mappings().first()
        if lineage is None:
            raise RuntimeError(
                "0045_native_section_authority refuses a ready legacy-GID document "
                "without one approved await-submission lineage"
            )
        import_run_id = bind.execute(
            sa.text(
                "SELECT r.import_run_id FROM section_catalog_versions c "
                "JOIN section_registry_versions r "
                "ON r.registry_version_id=c.source_registry_version_id "
                "WHERE c.catalog_version_id=:catalog_version_id"
            ),
            {"catalog_version_id": row["catalog_version_id"]},
        ).scalar_one()
        source_version_id = _uuid(row["current_content_version_id"])
        new_version_id = _derived_uuid("content", source_version_id)
        new_cycle_id = _derived_uuid("cycle", source_version_id)
        new_inspection_id = _derived_uuid("inspection", source_version_id)
        new_signoff_id = _derived_uuid("signoff", source_version_id)
        next_dish_version = int(row["dish_version"]) + 1
        bind.execute(
            sa.text(
                "INSERT INTO dish_mutation_receipts("
                "generation_id,task_id,dish_version,source_route,import_run_id,"
                "command_execution_id,content_changed,placement_changed,completion_changed,"
                "archive_changed,occurred_at) VALUES ("
                ":generation_id,:task_id,:dish_version,'import',:import_run_id,NULL,"
                ":yes,:no,:no,:no,:occurred_at)"
            ),
            {
                "generation_id": row["generation_id"],
                "task_id": row["task_id"],
                "dish_version": next_dish_version,
                "import_run_id": import_run_id,
                "yes": True,
                "no": False,
                "occurred_at": lineage["signed_at"],
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO task_content_versions("
                "content_version_id,generation_id,task_id,representation_kind,title,body,"
                "identity_scheme,content_identity,creator_route,import_run_id,command_execution_id,"
                "predecessor_content_version_id,contract_binding_id,created_dish_version,created_at) "
                "VALUES (:content_version_id,:generation_id,:task_id,'document',:title,:body,"
                ":identity_scheme,:content_identity,'import',:import_run_id,NULL,"
                ":predecessor_content_version_id,:contract_binding_id,:created_dish_version,:created_at)"
            ),
            {
                "content_version_id": _db_uuid(bind, new_version_id),
                "generation_id": row["generation_id"],
                "task_id": row["task_id"],
                "title": transformed.title,
                "body": transformed.body,
                "identity_scheme": CONTENT_IDENTITY_SCHEME,
                "content_identity": content_identity(transformed.title, transformed.body),
                "import_run_id": import_run_id,
                "predecessor_content_version_id": _db_uuid(bind, source_version_id),
                "contract_binding_id": row["contract_binding_id"],
                "created_dish_version": next_dish_version,
                "created_at": lineage["signed_at"],
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO verification_cycles("
                "cycle_id,generation_id,task_id,operation_id,reviewed_content_version_id,"
                "contract_binding_id,cycle_sequence,lifecycle,outcome,import_run_id,"
                "created_by_execution_id,created_at,terminal_at) VALUES ("
                ":cycle_id,:generation_id,:task_id,:operation_id,:content_version_id,"
                ":contract_binding_id,:cycle_sequence,'approved','approved',NULL,"
                ":execution_id,:at,:at)"
            ),
            {
                "cycle_id": _db_uuid(bind, new_cycle_id),
                "generation_id": row["generation_id"],
                "task_id": row["task_id"],
                "operation_id": lineage["operation_id"],
                "content_version_id": _db_uuid(bind, new_version_id),
                "contract_binding_id": row["contract_binding_id"],
                "cycle_sequence": int(lineage["cycle_sequence"]) + 1,
                "execution_id": lineage["command_execution_id"],
                "at": lineage["signed_at"],
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO verification_inspection_occurrences("
                "inspection_id,cycle_id,operation_id,generation_id,task_id,"
                "reviewed_content_version_id,verifier_actor_fact_id,verifier_run_id,attestation,"
                "section_id,registry_version_id,catalog_version_id,placement_version,request_id,"
                "command_execution_id,inspected_at) VALUES ("
                ":inspection_id,:cycle_id,:operation_id,:generation_id,:task_id,"
                ":content_version_id,:actor_id,:run_id,:attestation,:section_id,"
                ":registry_version_id,:catalog_version_id,:placement_version,:request_id,"
                ":execution_id,:at)"
            ),
            {
                "inspection_id": _db_uuid(bind, new_inspection_id),
                "cycle_id": _db_uuid(bind, new_cycle_id),
                "operation_id": lineage["operation_id"],
                "generation_id": row["generation_id"],
                "task_id": row["task_id"],
                "content_version_id": _db_uuid(bind, new_version_id),
                "actor_id": lineage["verifier_actor_fact_id"],
                "run_id": lineage["verifier_run_id"],
                "attestation": (
                    "Native Section identity carry-forward of inspection "
                    f"{lineage['inspection_id']}: {lineage['attestation']}"
                ),
                "section_id": lineage["section_id"],
                "registry_version_id": lineage["registry_version_id"],
                "catalog_version_id": row["catalog_version_id"],
                "placement_version": lineage["placement_version"],
                # The approval request is distinct from the original inspection request.
                "request_id": lineage["request_id"],
                "execution_id": lineage["command_execution_id"],
                "at": lineage["signed_at"],
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO verification_signoffs("
                "signoff_id,cycle_id,task_id,signed_content_version_id,inspection_id,"
                "verifier_actor_fact_id,inherited_from_signoff_id,signoff_kind,"
                "command_execution_id,signed_at) VALUES ("
                ":signoff_id,:cycle_id,:task_id,:content_version_id,:inspection_id,"
                ":actor_id,:prior_signoff_id,'inherited_non_material',:execution_id,:at)"
            ),
            {
                "signoff_id": _db_uuid(bind, new_signoff_id),
                "cycle_id": _db_uuid(bind, new_cycle_id),
                "task_id": row["task_id"],
                "content_version_id": _db_uuid(bind, new_version_id),
                "inspection_id": _db_uuid(bind, new_inspection_id),
                "actor_id": lineage["verifier_actor_fact_id"],
                "prior_signoff_id": lineage["signoff_id"],
                "execution_id": lineage["command_execution_id"],
                "at": lineage["signed_at"],
            },
        )
        bind.execute(
            sa.text(
                "UPDATE dish_states SET current_content_version_id=:content_version_id,"
                "dish_version=:dish_version,updated_at=:at "
                "WHERE generation_id=:generation_id AND task_id=:task_id"
            ),
            {
                "content_version_id": _db_uuid(bind, new_version_id),
                "dish_version": next_dish_version,
                "at": lineage["signed_at"],
                "generation_id": row["generation_id"],
                "task_id": row["task_id"],
            },
        )


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
    if op.get_bind().dialect.name == "postgresql":
        # Catalog backfill and immutable ready-occurrence conversion are one
        # migration-owned rewrite, not ordinary scalar mutations.
        op.execute("DROP TRIGGER dish_states_validate ON dish_states")
        for table in (
            "task_execution_fences",
            "verification_inspection_occurrences",
        ):
            op.execute(f"DROP TRIGGER {table}_immutable_update ON {table}")
        op.execute(
            "DROP TRIGGER authority_activations_immutable_update ON authority_activations"
        )
        op.execute(
            "DROP TRIGGER release_candidate_manifests_immutable_update "
            "ON release_candidate_manifests"
        )
        # PostgreSQL rejects ALTER TABLE after this transaction has queued
        # trigger events on DishState.  Relax the legacy registry binding
        # before the migration-owned data rewrite begins.
        op.execute(
            "ALTER TABLE dish_states ALTER COLUMN registry_version_id DROP NOT NULL"
        )
        op.create_index(
            "ix_dish_states_catalog",
            "dish_states",
            ["generation_id", "catalog_version_id", "task_id"],
        )
    op.execute("UPDATE dish_states SET catalog_version_id=registry_version_id")
    op.execute("UPDATE task_execution_fences SET expected_placement_version=(SELECT s.placement_version FROM dish_states s WHERE s.generation_id=task_execution_fences.generation_id AND s.task_id=task_execution_fences.task_id), catalog_version_id=(SELECT s.catalog_version_id FROM dish_states s WHERE s.generation_id=task_execution_fences.generation_id AND s.task_id=task_execution_fences.task_id)")
    op.execute("UPDATE workflow_operations SET catalog_version_id=(SELECT s.catalog_version_id FROM dish_states s WHERE s.generation_id=workflow_operations.generation_id AND s.task_id=workflow_operations.task_id)")
    op.execute("UPDATE verification_inspection_occurrences SET catalog_version_id=registry_version_id")
    op.execute("UPDATE authority_activations SET catalog_version_id=registry_version_id")
    op.execute("UPDATE release_candidates SET catalog_version_id=registry_version_id")
    op.execute("UPDATE release_candidate_manifests SET catalog_version_id=registry_version_id")
    _carry_ready_documents_to_native_sections()
    if op.get_bind().dialect.name == "postgresql":
        for table in (
            "task_execution_fences",
            "verification_inspection_occurrences",
        ):
            op.execute(
                f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION dish_reject_scalar_authority_mutation()"
            )
        op.execute(
            "CREATE TRIGGER authority_activations_immutable_update "
            "BEFORE UPDATE ON authority_activations FOR EACH ROW "
            "EXECUTE FUNCTION dish_reject_immutable_authority()"
        )
        op.execute(
            "CREATE TRIGGER release_candidate_manifests_immutable_update "
            "BEFORE UPDATE ON release_candidate_manifests FOR EACH ROW "
            "EXECUTE FUNCTION dish_reject_immutable_candidate_manifest_evidence()"
        )
    if op.get_bind().dialect.name != "postgresql":
        dish_state_triggers = _sqlite_suspend_triggers_referencing("dish_states")
        with op.batch_alter_table(
            "dish_states", recreate=_constraint_batch_mode()
        ) as batch:
            batch.alter_column(
                "registry_version_id", existing_type=sa.Uuid(), nullable=True
            )
        _sqlite_restore_triggers(dish_state_triggers)
    fence_triggers = _sqlite_suspend_triggers_referencing("task_execution_fences")
    with op.batch_alter_table("task_execution_fences", recreate=_constraint_batch_mode()) as batch:
        batch.drop_constraint("fk_task_execution_fence_membership_head", type_="foreignkey")
    _sqlite_restore_triggers(fence_triggers)
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
    if op.get_bind().dialect.name != "postgresql":
        op.create_index(
            "ix_dish_states_catalog",
            "dish_states",
            ["generation_id", "catalog_version_id", "task_id"],
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(_candidate_transition_sql(native=True))
        # Retain the scalar receipt/content checks while replacing its legacy
        # placement clause with the post-burn native catalog rule.  Rewriting
        # PostgreSQL's authoritative function text avoids duplicating the long
        # 0044 scalar invariant in this transition revision.
        op.execute(
            r"""
            DO $_dish_0045$
            DECLARE definition text;
            BEGIN
              SELECT pg_get_functiondef('dish_validate_scalar_state()'::regprocedure)
                INTO definition;
              definition := replace(
                definition,
                'OR (NOT receipt.placement_changed AND (NEW.section_id IS DISTINCT FROM OLD.section_id OR NEW.registry_version_id IS DISTINCT FROM OLD.registry_version_id))',
                'OR (NOT receipt.placement_changed AND (NEW.section_id IS DISTINCT FROM OLD.section_id OR NEW.registry_version_id IS DISTINCT FROM OLD.registry_version_id OR NEW.catalog_version_id IS DISTINCT FROM OLD.catalog_version_id))'
              );
              definition := replace(
                definition,
                $old$IF NOT EXISTS (SELECT 1 FROM section_registry_entries e
              WHERE e.registry_version_id=NEW.registry_version_id AND (NEW.section_id IS NULL OR e.section_id=NEW.section_id))
          THEN RAISE EXCEPTION 'DishState placement is absent from registry'; END IF;$old$,
                $new$IF (NEW.registry_version_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM section_registry_entries e
                 WHERE e.registry_version_id=NEW.registry_version_id
                   AND (NEW.section_id IS NULL OR e.section_id=NEW.section_id)))
             OR (NEW.registry_version_id IS NULL AND (
                NOT EXISTS (SELECT 1 FROM authority_activations a
                 WHERE a.generation_id=NEW.generation_id AND a.outcome='activated')
                OR NOT EXISTS (SELECT 1 FROM section_catalog_entries e
                 WHERE e.catalog_version_id=NEW.catalog_version_id
                   AND e.section_id=NEW.section_id)))
          THEN RAISE EXCEPTION 'DishState placement is absent from native authority'; END IF;$new$
              );
              IF position('DishState placement is absent from native authority' in definition) = 0
                 OR position('NEW.catalog_version_id IS DISTINCT FROM OLD.catalog_version_id' in definition) = 0
              THEN
                RAISE EXCEPTION '0045 scalar authority function template drift';
              END IF;
              EXECUTE definition;
            END $_dish_0045$;
            """
        )
        op.execute(
            "CREATE TRIGGER dish_states_validate BEFORE INSERT OR UPDATE ON dish_states "
            "FOR EACH ROW EXECUTE FUNCTION dish_validate_scalar_state()"
        )
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
        # Restore the exact predecessor scalar guard before removing native
        # catalog columns.
        previous = importlib.import_module(
            "dish_pg.migrations.versions.0044_independent_archive"
        )
        previous._replace_postgresql_guard(independent=True)
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
    dish_state_triggers = _sqlite_suspend_triggers_referencing("dish_states")
    with op.batch_alter_table("dish_states", recreate=_constraint_batch_mode()) as batch:
        batch.alter_column("registry_version_id", existing_type=sa.Uuid(), nullable=False)
    _sqlite_restore_triggers(dish_state_triggers)
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
    fence_triggers = _sqlite_suspend_triggers_referencing("task_execution_fences")
    with op.batch_alter_table("task_execution_fences", recreate=_constraint_batch_mode()) as batch:
        batch.create_foreign_key(
            "fk_task_execution_fence_membership_head",
            "task_membership_heads",
            ["generation_id", "task_id"],
            ["generation_id", "task_id"],
            ondelete="RESTRICT",
        )
    _sqlite_restore_triggers(fence_triggers)
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
