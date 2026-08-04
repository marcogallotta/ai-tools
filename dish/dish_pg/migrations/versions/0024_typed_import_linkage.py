"""Link imported evidence to typed native PostgreSQL objects.

Revision ID: 0024_typed_import_linkage
Revises: 0023_legacy_request_tombstones
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0024_typed_import_linkage"
down_revision = "0023_legacy_request_tombstones"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "source_import_native_links",
        sa.Column("link_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("import_batch_id", sa.Uuid(), nullable=False),
        sa.Column("import_run_id", sa.Uuid(), nullable=False),
        sa.Column("entity_kind", sa.String(32), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=True),
        sa.Column("section_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("content_version_id", sa.Uuid(), nullable=True),
        sa.Column("request_tombstone_id", sa.Uuid(), nullable=True),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(entity_kind='project' AND project_id IS NOT NULL AND section_id IS NULL AND task_id IS NULL AND content_version_id IS NULL AND request_tombstone_id IS NULL) OR "
            "(entity_kind='section' AND project_id IS NULL AND section_id IS NOT NULL AND task_id IS NULL AND content_version_id IS NULL AND request_tombstone_id IS NULL) OR "
            "(entity_kind='task' AND project_id IS NULL AND section_id IS NULL AND task_id IS NOT NULL AND content_version_id IS NULL AND request_tombstone_id IS NULL) OR "
            "(entity_kind='content' AND project_id IS NULL AND section_id IS NULL AND task_id IS NULL AND content_version_id IS NOT NULL AND request_tombstone_id IS NULL) OR "
            "(entity_kind='request_tombstone' AND project_id IS NULL AND section_id IS NULL AND task_id IS NULL AND content_version_id IS NULL AND request_tombstone_id IS NOT NULL)",
            name="ck_source_import_native_links_exact_typed_target",
        ),
        sa.ForeignKeyConstraint(["evidence_id"], ["source_import_entity_evidence.evidence_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["import_batch_id"], ["source_import_batches.import_batch_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["import_run_id"], ["stage_a_import_runs.import_run_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["governed_projects.project_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["section_id"], ["governed_sections.section_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["dish_tasks.task_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["content_version_id"], ["task_content_versions.content_version_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["request_tombstone_id"], ["legacy_request_tombstones.tombstone_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("link_id", name="pk_source_import_native_links"),
        sa.UniqueConstraint("evidence_id", name="uq_source_import_native_links_evidence_id"),
        sa.UniqueConstraint("import_batch_id", "project_id", name="uq_import_native_project"),
        sa.UniqueConstraint("import_batch_id", "section_id", name="uq_import_native_section"),
        sa.UniqueConstraint("import_batch_id", "task_id", name="uq_import_native_task"),
        sa.UniqueConstraint("import_batch_id", "content_version_id", name="uq_import_native_content"),
        sa.UniqueConstraint("import_batch_id", "request_tombstone_id", name="uq_import_native_tombstone"),
    )
    op.create_index("ix_import_native_links_run_kind", "source_import_native_links", ["import_run_id", "entity_kind"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION dish_validate_source_import_native_link()
            RETURNS trigger LANGUAGE plpgsql AS $$
            DECLARE expected_type text; target UUID; target_run UUID;
            BEGIN
                IF NEW.entity_kind='project' THEN expected_type='governed_project'; target=NEW.project_id;
                    SELECT import_run_id INTO target_run FROM governed_projects WHERE project_id=target;
                ELSIF NEW.entity_kind='section' THEN expected_type='governed_section'; target=NEW.section_id;
                    SELECT import_run_id INTO target_run FROM governed_sections WHERE section_id=target;
                ELSIF NEW.entity_kind='task' THEN expected_type='dish_task'; target=NEW.task_id;
                    SELECT import_run_id INTO target_run FROM dish_tasks WHERE task_id=target;
                ELSIF NEW.entity_kind='content' THEN expected_type='task_content_version'; target=NEW.content_version_id;
                    SELECT import_run_id INTO target_run FROM task_content_versions WHERE content_version_id=target;
                ELSIF NEW.entity_kind='request_tombstone' THEN expected_type='legacy_request_tombstone'; target=NEW.request_tombstone_id;
                    SELECT import_run_id INTO target_run FROM legacy_request_tombstones WHERE tombstone_id=target;
                ELSE RAISE EXCEPTION 'unsupported typed import entity kind';
                END IF;
                IF target_run IS DISTINCT FROM NEW.import_run_id THEN
                    RAISE EXCEPTION 'typed import target does not belong to expected import run';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM source_import_entity_evidence e
                    JOIN source_import_batches b ON b.import_batch_id=e.import_batch_id
                    WHERE e.evidence_id=NEW.evidence_id
                      AND e.import_batch_id=NEW.import_batch_id
                      AND b.import_run_id=NEW.import_run_id
                      AND e.entity_kind=NEW.entity_kind
                      AND e.target_entity_type=expected_type
                      AND e.target_entity_id=target
                ) THEN
                    RAISE EXCEPTION 'source evidence does not exactly identify typed native target';
                END IF;
                RETURN NEW;
            END; $$;
            CREATE TRIGGER source_import_native_links_validate
            BEFORE INSERT ON source_import_native_links FOR EACH ROW
            EXECUTE FUNCTION dish_validate_source_import_native_link();

            CREATE OR REPLACE FUNCTION dish_reject_source_import_native_link_mutation()
            RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                RAISE EXCEPTION 'typed import links are immutable';
            END; $$;
            CREATE TRIGGER source_import_native_links_immutable_update
            BEFORE UPDATE ON source_import_native_links FOR EACH ROW
            EXECUTE FUNCTION dish_reject_source_import_native_link_mutation();
            CREATE TRIGGER source_import_native_links_immutable_delete
            BEFORE DELETE ON source_import_native_links FOR EACH ROW
            EXECUTE FUNCTION dish_reject_source_import_native_link_mutation();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if int(bind.execute(sa.text("SELECT count(*) FROM source_import_native_links")).scalar_one()):
        raise RuntimeError("refusing lossy downgrade: typed import linkage exists")
    if bind.dialect.name == "postgresql":
        op.execute("DROP FUNCTION IF EXISTS dish_validate_source_import_native_link() CASCADE")
        op.execute("DROP FUNCTION IF EXISTS dish_reject_source_import_native_link_mutation() CASCADE")
    op.drop_index("ix_import_native_links_run_kind", table_name="source_import_native_links")
    op.drop_table("source_import_native_links")
