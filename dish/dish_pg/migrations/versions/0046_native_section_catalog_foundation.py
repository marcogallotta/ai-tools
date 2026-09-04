"""Add the native Section/catalog definition foundation without switching runtime authority."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import context, op

revision = "0046_native_section_catalog_foundation"
down_revision = "0045_cook_log_entries"
branch_labels = None
depends_on = None


def _online_preflight() -> None:
    if context.is_offline_mode():
        return
    bind = op.get_bind()
    duplicate = bind.exec_driver_sql(
        "SELECT logical_name FROM governed_sections GROUP BY logical_name "
        "HAVING count(*) > 1 LIMIT 1"
    ).first()
    if duplicate is not None:
        raise RuntimeError(
            "0046_native_section_catalog_foundation cannot remove Project scope from "
            f"duplicate Section name {duplicate[0]!r}"
        )
    mismatch = bind.exec_driver_sql(
        "SELECT a.generation_id FROM section_registry_activations a "
        "JOIN section_registry_versions v ON v.registry_version_id=a.registry_version_id "
        "WHERE a.generation_id<>v.generation_id "
        "OR a.registry_revision<>v.version_number LIMIT 1"
    ).first()
    if mismatch is not None:
        raise RuntimeError(
            "0046_native_section_catalog_foundation requires contiguous legacy registry "
            f"version/activation identity for generation {mismatch[0]}"
        )


def _create_tables() -> None:
    op.create_table(
        "sections",
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("logical_name", sa.String(256), nullable=False),
        sa.Column("lifecycle", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "lifecycle IN ('active','retired')",
            name=op.f("ck_sections_lifecycle_allowed"),
        ),
        sa.CheckConstraint(
            "length(trim(logical_name)) > 0",
            name=op.f("ck_sections_logical_name_nonblank"),
        ),
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
        sa.CheckConstraint(
            "version_number > 0",
            name=op.f("ck_section_catalog_versions_positive_version"),
        ),
        sa.CheckConstraint(
            "length(catalog_sha256) = 64",
            name=op.f("ck_section_catalog_versions_catalog_hash_length"),
        ),
        sa.CheckConstraint(
            "(source_registry_version_id IS NULL AND transform_sha256 IS NULL) OR "
            "(source_registry_version_id IS NOT NULL AND length(transform_sha256) = 64)",
            name=op.f("ck_section_catalog_versions_transition_transform_exact"),
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["authority_generations.generation_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["contract_binding_id"],
            ["honest_contract_bindings.binding_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_registry_version_id"],
            ["section_registry_versions.registry_version_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "catalog_version_id", name=op.f("pk_section_catalog_versions")
        ),
        sa.UniqueConstraint(
            "generation_id", "version_number", name="uq_catalog_generation_version"
        ),
    )
    op.create_table(
        "section_catalog_entries",
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("workflow_role", sa.String(64), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 0", name=op.f("ck_section_catalog_entries_nonnegative_ordinal")
        ),
        sa.CheckConstraint(
            "length(trim(display_name)) > 0",
            name=op.f("ck_section_catalog_entries_display_name_nonblank"),
        ),
        sa.CheckConstraint(
            "length(trim(workflow_role)) > 0",
            name=op.f("ck_section_catalog_entries_workflow_role_nonblank"),
        ),
        sa.ForeignKeyConstraint(
            ["catalog_version_id"],
            ["section_catalog_versions.catalog_version_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["section_id"], ["sections.section_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint(
            "catalog_version_id", "section_id", name=op.f("pk_section_catalog_entries")
        ),
        sa.UniqueConstraint(
            "catalog_version_id", "ordinal", name="uq_catalog_entry_ordinal"
        ),
        sa.UniqueConstraint(
            "catalog_version_id", "workflow_role", name="uq_catalog_entry_workflow_role"
        ),
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
        sa.CheckConstraint(
            "activation_route IN ('transition','command_execution','recovery')",
            name=op.f("ck_section_catalog_activations_route_allowed"),
        ),
        sa.CheckConstraint(
            "(activation_route = 'transition' AND import_run_id IS NOT NULL "
            "AND command_execution_id IS NULL) OR "
            "(activation_route = 'command_execution' AND import_run_id IS NULL "
            "AND command_execution_id IS NOT NULL) OR "
            "(activation_route = 'recovery' AND import_run_id IS NULL "
            "AND command_execution_id IS NULL)",
            name=op.f("ck_section_catalog_activations_exact_provenance_route"),
        ),
        sa.CheckConstraint(
            "catalog_revision > 0",
            name=op.f("ck_section_catalog_activations_positive_revision"),
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["authority_generations.generation_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_version_id"],
            ["section_catalog_versions.catalog_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["import_run_id"],
            ["stage_a_import_runs.import_run_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "catalog_activation_id", name=op.f("pk_section_catalog_activations")
        ),
        sa.UniqueConstraint(
            "generation_id", "catalog_revision", name="uq_catalog_activation_revision"
        ),
        sa.UniqueConstraint(
            "generation_id", "catalog_version_id", name="uq_catalog_activation_version"
        ),
    )
    op.create_table(
        "active_section_catalogs",
        sa.Column("generation_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_version_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_activation_id", sa.Uuid(), nullable=False),
        sa.Column("catalog_revision", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "catalog_revision > 0",
            name=op.f("ck_active_section_catalogs_positive_revision"),
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["authority_generations.generation_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_version_id"],
            ["section_catalog_versions.catalog_version_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["catalog_activation_id"],
            ["section_catalog_activations.catalog_activation_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint(
            "generation_id", name=op.f("pk_active_section_catalogs")
        ),
        sa.UniqueConstraint(
            "catalog_version_id",
            name=op.f("uq_active_section_catalogs_catalog_version_id"),
        ),
        sa.UniqueConstraint(
            "catalog_activation_id",
            name=op.f("uq_active_section_catalogs_catalog_activation_id"),
        ),
    )


def _copy_transition_catalog() -> None:
    op.execute(
        "INSERT INTO sections(section_id,logical_name,lifecycle,created_at,retired_at) "
        "SELECT section_id,logical_name,lifecycle,created_at,retired_at FROM governed_sections"
    )
    op.execute(
        "INSERT INTO section_catalog_versions("
        "catalog_version_id,generation_id,version_number,contract_binding_id,catalog_sha256,"
        "source_registry_version_id,transform_sha256,created_at) "
        "SELECT registry_version_id,generation_id,version_number,contract_binding_id,"
        "registry_sha256,registry_version_id,registry_sha256,created_at "
        "FROM section_registry_versions"
    )
    op.execute(
        "INSERT INTO section_catalog_entries("
        "catalog_version_id,section_id,ordinal,display_name,workflow_role) "
        "SELECT registry_version_id,section_id,ordinal,display_name,workflow_role "
        "FROM section_registry_entries"
    )
    op.execute(
        "INSERT INTO section_catalog_activations("
        "catalog_activation_id,generation_id,catalog_version_id,activation_route,"
        "import_run_id,command_execution_id,catalog_revision,activated_at) "
        "SELECT a.registry_activation_id,a.generation_id,a.registry_version_id,'transition',"
        "v.import_run_id,NULL,a.registry_revision,a.activated_at "
        "FROM section_registry_activations a "
        "JOIN section_registry_versions v ON v.registry_version_id=a.registry_version_id"
    )
    op.execute(
        "INSERT INTO active_section_catalogs("
        "generation_id,catalog_version_id,catalog_activation_id,catalog_revision,updated_at) "
        "SELECT generation_id,registry_version_id,registry_activation_id,"
        "registry_revision,updated_at FROM active_section_registries"
    )


def _install_postgresql_guards() -> None:
    immutable_tables = (
        "section_catalog_versions",
        "section_catalog_entries",
        "section_catalog_activations",
    )
    for table in immutable_tables:
        op.execute(
            f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION dish_reject_immutable_authority()"
        )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_protect_native_section_identity()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'native Section identity is immutable' USING ERRCODE='23514';
            END IF;
            IF OLD.section_id IS DISTINCT FROM NEW.section_id THEN
                RAISE EXCEPTION 'native Section identity is immutable' USING ERRCODE='23514';
            END IF;
            IF NEW.lifecycle = 'retired' AND OLD.lifecycle <> 'retired' AND EXISTS (
                SELECT 1
                  FROM active_section_catalogs a
                  JOIN authority_generations g
                    ON g.generation_id=a.generation_id
                  JOIN section_catalog_entries e
                    ON e.catalog_version_id=a.catalog_version_id
                 WHERE e.section_id=OLD.section_id
                   AND g.status='active'
            ) THEN
                RAISE EXCEPTION 'active catalog Section cannot be retired'
                    USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER sections_identity_guard BEFORE UPDATE OR DELETE ON sections "
        "FOR EACH ROW EXECUTE FUNCTION dish_protect_native_section_identity()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_section_catalog_version()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                  FROM authority_generations g
                  JOIN honest_contract_bindings b ON b.binding_id=NEW.contract_binding_id
                 WHERE g.generation_id=NEW.generation_id
                   AND g.status='active'
                   AND b.binding_kind='release'
                   AND b.dish_release=g.dish_release
            ) THEN
                RAISE EXCEPTION 'native catalog Honest binding mismatch' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER section_catalog_versions_binding_validate BEFORE INSERT "
        "ON section_catalog_versions FOR EACH ROW "
        "EXECUTE FUNCTION dish_validate_section_catalog_version()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_section_catalog_entry()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM sections s
                 WHERE s.section_id=NEW.section_id AND s.lifecycle='active'
            ) THEN
                RAISE EXCEPTION 'native catalog entry requires active Section' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER section_catalog_entries_section_validate BEFORE INSERT "
        "ON section_catalog_entries FOR EACH ROW "
        "EXECUTE FUNCTION dish_validate_section_catalog_entry()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_section_catalog_activation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM section_catalog_versions v
                 WHERE v.catalog_version_id=NEW.catalog_version_id
                   AND v.generation_id=NEW.generation_id
                   AND v.version_number=NEW.catalog_revision
            ) THEN
                RAISE EXCEPTION 'native catalog activation mismatch' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER section_catalog_activations_version_validate BEFORE INSERT "
        "ON section_catalog_activations FOR EACH ROW "
        "EXECUTE FUNCTION dish_validate_section_catalog_activation()"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION dish_validate_active_section_catalog()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' AND (
                NEW.generation_id IS DISTINCT FROM OLD.generation_id
                OR NEW.catalog_revision IS DISTINCT FROM OLD.catalog_revision + 1
            ) THEN
                RAISE EXCEPTION 'active native catalog transition is not contiguous'
                    USING ERRCODE='23514';
            END IF;
            IF NOT EXISTS (
                SELECT 1
                  FROM section_catalog_activations a
                  JOIN section_catalog_versions v
                    ON v.catalog_version_id=a.catalog_version_id
                 WHERE a.catalog_activation_id=NEW.catalog_activation_id
                   AND a.generation_id=NEW.generation_id
                   AND a.catalog_version_id=NEW.catalog_version_id
                   AND a.catalog_revision=NEW.catalog_revision
                   AND v.generation_id=NEW.generation_id
                   AND v.version_number=NEW.catalog_revision
                   AND EXISTS (
                       SELECT 1 FROM section_catalog_entries e
                       WHERE e.catalog_version_id=NEW.catalog_version_id
                   )
                   AND NOT EXISTS (
                       SELECT 1 FROM section_catalog_entries e
                       JOIN sections s ON s.section_id=e.section_id
                       WHERE e.catalog_version_id=NEW.catalog_version_id
                         AND s.lifecycle<>'active'
                   )
            ) THEN
                RAISE EXCEPTION 'active native catalog pointer is invalid' USING ERRCODE='23514';
            END IF;
            IF TG_OP = 'INSERT' AND NEW.catalog_revision <> 1 THEN
                RAISE EXCEPTION 'initial active native catalog revision must be 1'
                    USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER active_section_catalogs_validate BEFORE INSERT OR UPDATE "
        "ON active_section_catalogs FOR EACH ROW "
        "EXECUTE FUNCTION dish_validate_active_section_catalog()"
    )
    op.execute(
        "CREATE TRIGGER active_section_catalogs_delete_forbidden BEFORE DELETE "
        "ON active_section_catalogs FOR EACH ROW "
        "EXECUTE FUNCTION dish_reject_immutable_authority()"
    )


def _install_sqlite_guards() -> None:
    for table in (
        "section_catalog_versions",
        "section_catalog_entries",
        "section_catalog_activations",
    ):
        op.execute(
            f"CREATE TRIGGER {table}_immutable_update BEFORE UPDATE ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
        )
        op.execute(
            f"CREATE TRIGGER {table}_immutable_delete BEFORE DELETE ON {table} "
            "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
        )
    op.execute(
        "CREATE TRIGGER sections_identity_immutable BEFORE UPDATE OF section_id ON sections "
        "BEGIN SELECT RAISE(ABORT, 'Section identity is immutable'); END"
    )
    op.execute(
        "CREATE TRIGGER sections_delete_forbidden BEFORE DELETE ON sections "
        "BEGIN SELECT RAISE(ABORT, 'Section cannot be deleted'); END"
    )
    op.execute(
        "CREATE TRIGGER sections_active_catalog_retirement_guard "
        "BEFORE UPDATE OF lifecycle ON sections "
        "WHEN NEW.lifecycle='retired' AND EXISTS ("
        "SELECT 1 FROM active_section_catalogs a "
        "JOIN authority_generations g ON g.generation_id=a.generation_id "
        "JOIN section_catalog_entries e "
        "ON e.catalog_version_id=a.catalog_version_id "
        "WHERE e.section_id=OLD.section_id AND g.status='active') "
        "BEGIN SELECT RAISE(ABORT, 'active catalog Section cannot be retired'); END"
    )
    op.execute(
        "CREATE TRIGGER section_catalog_versions_binding_validate "
        "BEFORE INSERT ON section_catalog_versions WHEN NOT EXISTS ("
        "SELECT 1 FROM authority_generations g JOIN honest_contract_bindings b "
        "ON b.binding_id=NEW.contract_binding_id WHERE g.generation_id=NEW.generation_id "
        "AND g.status='active' AND b.binding_kind='release' "
        "AND b.dish_release=g.dish_release) "
        "BEGIN SELECT RAISE(ABORT, 'native catalog Honest binding mismatch'); END"
    )
    op.execute(
        "CREATE TRIGGER section_catalog_entries_section_validate "
        "BEFORE INSERT ON section_catalog_entries WHEN NOT EXISTS ("
        "SELECT 1 FROM sections s WHERE s.section_id=NEW.section_id "
        "AND s.lifecycle='active') "
        "BEGIN SELECT RAISE(ABORT, 'native catalog entry requires active Section'); END"
    )
    op.execute(
        "CREATE TRIGGER section_catalog_activations_version_validate "
        "BEFORE INSERT ON section_catalog_activations WHEN NOT EXISTS ("
        "SELECT 1 FROM section_catalog_versions v "
        "WHERE v.catalog_version_id=NEW.catalog_version_id "
        "AND v.generation_id=NEW.generation_id "
        "AND v.version_number=NEW.catalog_revision) "
        "BEGIN SELECT RAISE(ABORT, 'native catalog activation mismatch'); END"
    )
    pointer_match = (
        "EXISTS (SELECT 1 FROM section_catalog_activations a "
        "JOIN section_catalog_versions v ON v.catalog_version_id=a.catalog_version_id "
        "WHERE a.catalog_activation_id=NEW.catalog_activation_id "
        "AND a.generation_id=NEW.generation_id "
        "AND a.catalog_version_id=NEW.catalog_version_id "
        "AND a.catalog_revision=NEW.catalog_revision "
        "AND v.generation_id=NEW.generation_id "
        "AND v.version_number=NEW.catalog_revision "
        "AND EXISTS (SELECT 1 FROM section_catalog_entries e "
        "WHERE e.catalog_version_id=NEW.catalog_version_id) "
        "AND NOT EXISTS (SELECT 1 FROM section_catalog_entries e "
        "JOIN sections s ON s.section_id=e.section_id "
        "WHERE e.catalog_version_id=NEW.catalog_version_id AND s.lifecycle<>'active'))"
    )
    op.execute(
        "CREATE TRIGGER active_section_catalogs_validate_insert "
        "BEFORE INSERT ON active_section_catalogs WHEN "
        f"NEW.catalog_revision<>1 OR NOT {pointer_match} "
        "BEGIN SELECT RAISE(ABORT, 'active native catalog pointer is invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER active_section_catalogs_validate_update "
        "BEFORE UPDATE ON active_section_catalogs WHEN "
        "NEW.generation_id<>OLD.generation_id OR "
        "NEW.catalog_revision<>OLD.catalog_revision+1 OR "
        f"NOT {pointer_match} "
        "BEGIN SELECT RAISE(ABORT, 'active native catalog transition is invalid'); END"
    )
    op.execute(
        "CREATE TRIGGER active_section_catalogs_delete_forbidden "
        "BEFORE DELETE ON active_section_catalogs "
        "BEGIN SELECT RAISE(ABORT, 'active native catalog cannot be deleted'); END"
    )


def upgrade() -> None:
    _online_preflight()
    _create_tables()
    _copy_transition_catalog()
    if op.get_bind().dialect.name == "postgresql":
        _install_postgresql_guards()
    else:
        _install_sqlite_guards()


def _downgrade_guard() -> None:
    if context.is_offline_mode():
        if op.get_bind().dialect.name == "postgresql":
            op.execute(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM section_catalog_versions
                         WHERE source_registry_version_id IS NULL
                            OR catalog_version_id<>source_registry_version_id
                    ) OR EXISTS (
                        SELECT 1
                          FROM sections s
                          LEFT JOIN governed_sections g
                            ON g.section_id=s.section_id
                         WHERE g.section_id IS NULL
                            OR g.logical_name IS DISTINCT FROM s.logical_name
                            OR g.lifecycle IS DISTINCT FROM s.lifecycle
                            OR g.retired_at IS DISTINCT FROM s.retired_at
                    ) THEN
                        RAISE EXCEPTION '0046 downgrade refuses native Section/catalog changes';
                    END IF;
                END
                $$
                """
            )
        return
    bind = op.get_bind()
    section_drift = (
        "g.section_id IS NULL OR g.logical_name IS DISTINCT FROM s.logical_name "
        "OR g.lifecycle IS DISTINCT FROM s.lifecycle "
        "OR g.retired_at IS DISTINCT FROM s.retired_at"
        if bind.dialect.name == "postgresql"
        else "g.section_id IS NULL OR g.logical_name IS NOT s.logical_name "
        "OR g.lifecycle IS NOT s.lifecycle OR g.retired_at IS NOT s.retired_at"
    )
    changed = bind.exec_driver_sql(
        "SELECT 1 FROM section_catalog_versions "
        "WHERE source_registry_version_id IS NULL "
        "OR catalog_version_id<>source_registry_version_id "
        "UNION ALL SELECT 1 FROM sections s "
        "LEFT JOIN governed_sections g ON g.section_id=s.section_id "
        f"WHERE {section_drift} LIMIT 1"
    ).first()
    if changed is not None:
        raise RuntimeError("0046 downgrade refuses native Section/catalog changes")


def downgrade() -> None:
    _downgrade_guard()
    op.drop_table("active_section_catalogs")
    op.drop_table("section_catalog_activations")
    op.drop_table("section_catalog_entries")
    op.drop_table("section_catalog_versions")
    op.drop_table("sections")
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS dish_validate_active_section_catalog()")
        op.execute("DROP FUNCTION IF EXISTS dish_validate_section_catalog_activation()")
        op.execute("DROP FUNCTION IF EXISTS dish_validate_section_catalog_entry()")
        op.execute("DROP FUNCTION IF EXISTS dish_validate_section_catalog_version()")
        op.execute("DROP FUNCTION IF EXISTS dish_protect_native_section_identity()")
