"""Stage A core PostgreSQL authority mappings.

Stage 2 deliberately stops before requests, command executions, workflow operations,
leases, holds, Verification, audit, and projection outbox authority. The models here
own only generation/provenance, governed registry, stable task identity, immutable
complete documents, logical location, and completion.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.schema import DDL

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class ImportRun(Base):
    __tablename__ = "stage_a_import_runs"

    import_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    source_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    source_release: Mapped[str] = mapped_column(String(128), nullable=False)
    legacy_generation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    baseline_high_water_mark: Mapped[str] = mapped_column(String(256), nullable=False)
    source_bundle_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        CheckConstraint("status IN ('complete','failed')", name="status_allowed"),
        CheckConstraint("length(source_bundle_sha256) = 64", name="bundle_hash_length"),
        CheckConstraint(
            "(status = 'complete' AND completed_at IS NOT NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL)",
            name="terminal_has_completion",
        ),
        UniqueConstraint(
            "legacy_generation_id",
            "baseline_high_water_mark",
            name="uq_import_run_legacy_high_water",
        ),
    )


class AuthorityGeneration(Base):
    __tablename__ = "authority_generations"

    generation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    predecessor_generation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT")
    )
    creation_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    external_restore_control_id: Mapped[str | None] = mapped_column(String(256), unique=True)
    schema_head: Mapped[str] = mapped_column(String(64), nullable=False)
    dish_release: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "creation_reason IN ('initial_cutover','destructive_restore')",
            name="creation_reason_allowed",
        ),
        CheckConstraint("status IN ('pending','active','retired')", name="status_allowed"),
        CheckConstraint(
            "(creation_reason = 'initial_cutover' AND predecessor_generation_id IS NULL "
            "AND external_restore_control_id IS NULL) OR "
            "(creation_reason = 'destructive_restore' AND predecessor_generation_id IS NOT NULL "
            "AND external_restore_control_id IS NOT NULL)",
            name="creation_provenance_complete",
        ),
        CheckConstraint(
            "(status = 'retired' AND retired_at IS NOT NULL) OR "
            "(status IN ('pending','active') AND retired_at IS NULL)",
            name="retirement_state_consistent",
        ),
        CheckConstraint(
            "predecessor_generation_id IS NULL OR predecessor_generation_id <> generation_id",
            name="predecessor_not_self",
        ),
        Index(
            "uq_authority_generations_one_active",
            "status",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )


class GenerationBootstrapAuthority(Base):
    __tablename__ = "generation_bootstrap_authorities"

    bootstrap_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    external_control_id: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    capability_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "NOT (consumed_at IS NOT NULL AND retired_at IS NOT NULL)",
            name="single_terminal_route",
        ),
    )


class AuthorityActivation(Base):
    __tablename__ = "authority_activations"

    activation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    import_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    cutover_approval_id: Mapped[str] = mapped_column(String(256), nullable=False)
    legacy_bundle_id: Mapped[str] = mapped_column(String(256), nullable=False)
    schema_head: Mapped[str] = mapped_column(String(64), nullable=False)
    dish_release: Mapped[str] = mapped_column(String(128), nullable=False)
    honest_release: Mapped[str] = mapped_column(String(128), nullable=False)
    protocol_release: Mapped[str] = mapped_column(String(128), nullable=False)
    openapi_release: Mapped[str] = mapped_column(String(128), nullable=False)
    routing_release: Mapped[str] = mapped_column(String(128), nullable=False)
    projection_epoch: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    rollback_burned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("outcome IN ('activated','aborted')", name="outcome_allowed"),
        CheckConstraint(
            "(outcome = 'activated' AND rollback_burned_at IS NOT NULL) OR "
            "(outcome = 'aborted' AND rollback_burned_at IS NULL)",
            name="rollback_burn_matches_outcome",
        ),
        UniqueConstraint("generation_id", "outcome", name="uq_activation_generation_outcome"),
    )


class AppliedMigrationEvent(Base):
    __tablename__ = "applied_migration_events"

    migration_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    revision: Mapped[str] = mapped_column(String(64), nullable=False)
    predecessor_revision: Mapped[str | None] = mapped_column(String(64))
    migration_code_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dish_release: Mapped[str] = mapped_column(String(128), nullable=False)
    initiator: Mapped[str] = mapped_column(String(256), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('applied','failed','repair','reversal','stamp')",
            name="outcome_allowed",
        ),
        CheckConstraint("length(migration_code_sha256) = 64", name="code_hash_length"),
        UniqueConstraint(
            "generation_id", "revision", "outcome", name="uq_migration_generation_revision_outcome"
        ),
    )


class HonestContractBinding(Base):
    __tablename__ = "honest_contract_bindings"

    binding_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    binding_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_identity: Mapped[str] = mapped_column(String(512), nullable=False)
    dish_release: Mapped[str] = mapped_column(String(128), nullable=False)
    honest_release: Mapped[str] = mapped_column(String(128), nullable=False)
    protocol_release: Mapped[str] = mapped_column(String(128), nullable=False)
    protocol_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_release: Mapped[str] = mapped_column(String(128), nullable=False)
    schema_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    migration_id: Mapped[str | None] = mapped_column(String(128))
    source_schema_version: Mapped[str | None] = mapped_column(String(64))
    target_schema_version: Mapped[str | None] = mapped_column(String(64))
    migration_metadata_sha256: Mapped[str | None] = mapped_column(String(64))
    source_ids: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "binding_kind IN ('release','task_schema','migration')",
            name="binding_kind_allowed",
        ),
        CheckConstraint("length(protocol_sha256) = 64", name="protocol_hash_length"),
        CheckConstraint("length(schema_sha256) = 64", name="schema_hash_length"),
        CheckConstraint(
            "(binding_kind <> 'migration' AND migration_id IS NULL "
            "AND source_schema_version IS NULL AND target_schema_version IS NULL "
            "AND migration_metadata_sha256 IS NULL) OR "
            "(binding_kind = 'migration' AND migration_id IS NOT NULL "
            "AND source_schema_version IS NOT NULL AND target_schema_version IS NOT NULL "
            "AND length(migration_metadata_sha256) = 64)",
            name="migration_fields_match_kind",
        ),
        UniqueConstraint(
            "binding_kind",
            "protocol_sha256",
            "schema_sha256",
            "migration_id",
            "migration_metadata_sha256",
            name="uq_honest_binding_exact_identity",
        ),
    )


class GovernedProject(Base):
    __tablename__ = "governed_projects"

    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    logical_name: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False)
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("lifecycle IN ('active','retired')", name="lifecycle_allowed"),
        CheckConstraint(
            "(lifecycle = 'active' AND retired_at IS NULL) OR "
            "(lifecycle = 'retired' AND retired_at IS NOT NULL)",
            name="retirement_consistent",
        ),
    )


class GovernedSection(Base):
    __tablename__ = "governed_sections"

    section_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("governed_projects.project_id", ondelete="RESTRICT"), nullable=False
    )
    logical_name: Mapped[str] = mapped_column(String(256), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False)
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("lifecycle IN ('active','retired')", name="lifecycle_allowed"),
        CheckConstraint(
            "(lifecycle = 'active' AND retired_at IS NULL) OR "
            "(lifecycle = 'retired' AND retired_at IS NOT NULL)",
            name="retirement_consistent",
        ),
        UniqueConstraint("project_id", "logical_name", name="uq_section_project_name"),
    )


class SectionRegistryVersion(Base):
    __tablename__ = "section_registry_versions"

    registry_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    import_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT"), nullable=False
    )
    contract_binding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("honest_contract_bindings.binding_id", ondelete="RESTRICT"), nullable=False
    )
    registry_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("version_number > 0", name="positive_version"),
        CheckConstraint("length(registry_sha256) = 64", name="registry_hash_length"),
        UniqueConstraint("generation_id", "version_number", name="uq_registry_generation_version"),
    )


class SectionRegistryEntry(Base):
    __tablename__ = "section_registry_entries"

    registry_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("section_registry_versions.registry_version_id", ondelete="CASCADE"),
        primary_key=True,
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("governed_sections.section_id", ondelete="RESTRICT"), primary_key=True
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    workflow_role: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="nonnegative_ordinal"),
        UniqueConstraint("registry_version_id", "ordinal", name="uq_registry_entry_ordinal"),
        UniqueConstraint(
            "registry_version_id", "workflow_role", name="uq_registry_entry_workflow_role"
        ),
    )


class SectionRegistryActivation(Base):
    __tablename__ = "section_registry_activations"

    registry_activation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    registry_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("section_registry_versions.registry_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    activation_route: Mapped[str] = mapped_column(String(24), nullable=False)
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT")
    )
    command_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    registry_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "activation_route IN ('import','command_execution')", name="route_allowed"
        ),
        CheckConstraint(
            "(activation_route = 'import' AND import_run_id IS NOT NULL "
            "AND command_execution_id IS NULL) OR "
            "(activation_route = 'command_execution' AND import_run_id IS NULL "
            "AND command_execution_id IS NOT NULL)",
            name="exact_provenance_route",
        ),
        CheckConstraint("registry_revision > 0", name="positive_revision"),
        UniqueConstraint(
            "generation_id", "registry_revision", name="uq_registry_activation_revision"
        ),
        UniqueConstraint(
            "generation_id", "registry_version_id", name="uq_registry_activation_version"
        ),
    )


class ActiveSectionRegistry(Base):
    __tablename__ = "active_section_registries"

    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    registry_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("section_registry_versions.registry_version_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    registry_activation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("section_registry_activations.registry_activation_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    registry_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (CheckConstraint("registry_revision > 0", name="positive_revision"),)


class _ExternalAliasMixin:
    alias_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    external_system: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(256), nullable=False)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT")
    )
    projection_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def _external_alias_constraints(alias_kind: str) -> tuple[object, ...]:
    """Return fresh constraints for one alias table.

    Constraint instances cannot be shared across SQLAlchemy tables, and PostgreSQL
    unique constraints create schema-scoped index names. Each alias table therefore
    receives its own objects and explicit unique-identity name.
    """

    return (
        CheckConstraint("origin IN ('imported','projection')", name="origin_allowed"),
        CheckConstraint("state IN ('active','retired')", name="state_allowed"),
        CheckConstraint(
            "(origin = 'imported' AND import_run_id IS NOT NULL "
            "AND projection_event_id IS NULL) OR "
            "(origin = 'projection' AND import_run_id IS NULL "
            "AND projection_event_id IS NOT NULL)",
            name="exact_origin_provenance",
        ),
        CheckConstraint(
            "(state = 'active' AND retired_at IS NULL) OR "
            "(state = 'retired' AND retired_at IS NOT NULL)",
            name="retirement_consistent",
        ),
        UniqueConstraint(
            "external_system",
            "external_id",
            name=f"uq_{alias_kind}_external_alias_identity",
        ),
    )


class ProjectExternalAlias(_ExternalAliasMixin, Base):
    __tablename__ = "project_external_aliases"

    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("governed_projects.project_id", ondelete="RESTRICT"), nullable=False
    )
    __table_args__ = _external_alias_constraints("project")


class SectionExternalAlias(_ExternalAliasMixin, Base):
    __tablename__ = "section_external_aliases"

    section_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("governed_sections.section_id", ondelete="RESTRICT"), nullable=False
    )
    __table_args__ = _external_alias_constraints("section")


class DishTask(Base):
    __tablename__ = "dish_tasks"

    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    existence_state: Mapped[str] = mapped_column(String(16), nullable=False)
    creation_route: Mapped[str] = mapped_column(String(16), nullable=False)
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT")
    )
    command_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "existence_state IN ('ordinary','isolated','retired')", name="existence_allowed"
        ),
        CheckConstraint("creation_route IN ('import','create')", name="creation_route_allowed"),
        CheckConstraint(
            "(creation_route = 'import' AND import_run_id IS NOT NULL "
            "AND command_execution_id IS NULL) OR "
            "(creation_route = 'create' AND import_run_id IS NULL "
            "AND command_execution_id IS NOT NULL)",
            name="exact_creation_provenance",
        ),
        CheckConstraint(
            "(existence_state = 'retired' AND retired_at IS NOT NULL) OR "
            "(existence_state IN ('ordinary','isolated') AND retired_at IS NULL)",
            name="retirement_consistent",
        ),
    )


class TaskExternalAlias(_ExternalAliasMixin, Base):
    __tablename__ = "task_external_aliases"

    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    __table_args__ = _external_alias_constraints("task")


class ContentVersion(Base):
    __tablename__ = "task_content_versions"

    content_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    representation_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    identity_scheme: Mapped[str] = mapped_column(String(64), nullable=False)
    content_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    creator_route: Mapped[str] = mapped_column(String(24), nullable=False)
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT")
    )
    command_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    predecessor_content_version_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    contract_binding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("honest_contract_bindings.binding_id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["generation_id", "task_id", "predecessor_content_version_id"],
            [
                "task_content_versions.generation_id",
                "task_content_versions.task_id",
                "task_content_versions.content_version_id",
            ],
            ondelete="RESTRICT",
            name="fk_content_version_exact_predecessor",
        ),
        CheckConstraint("representation_kind = 'document'", name="document_only"),
        CheckConstraint("length(trim(title)) > 0", name="title_nonblank"),
        CheckConstraint("length(content_identity) > 0", name="identity_nonblank"),
        CheckConstraint(
            "creator_route IN ('import','command_execution')", name="creator_route_allowed"
        ),
        CheckConstraint(
            "(creator_route = 'import' AND import_run_id IS NOT NULL "
            "AND command_execution_id IS NULL) OR "
            "(creator_route = 'command_execution' AND import_run_id IS NULL "
            "AND command_execution_id IS NOT NULL)",
            name="exact_creator_provenance",
        ),
        CheckConstraint(
            "predecessor_content_version_id IS NULL OR "
            "predecessor_content_version_id <> content_version_id",
            name="predecessor_not_self",
        ),
        UniqueConstraint(
            "generation_id",
            "task_id",
            "identity_scheme",
            "content_identity",
            name="uq_content_exact_identity",
        ),
        UniqueConstraint(
            "generation_id", "task_id", "content_version_id", name="uq_content_generation_task_id"
        ),
    )


class ContentActivation(Base):
    __tablename__ = "task_content_activations"

    content_activation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    content_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    activation_route: Mapped[str] = mapped_column(String(24), nullable=False)
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT")
    )
    command_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    task_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["generation_id", "task_id", "content_version_id"],
            [
                "task_content_versions.generation_id",
                "task_content_versions.task_id",
                "task_content_versions.content_version_id",
            ],
            ondelete="RESTRICT",
            name="fk_content_activation_exact_version",
        ),
        CheckConstraint(
            "activation_route IN ('import','command_execution')", name="route_allowed"
        ),
        CheckConstraint(
            "(activation_route = 'import' AND import_run_id IS NOT NULL "
            "AND command_execution_id IS NULL) OR "
            "(activation_route = 'command_execution' AND import_run_id IS NULL "
            "AND command_execution_id IS NOT NULL)",
            name="exact_provenance_route",
        ),
        CheckConstraint("task_revision > 0", name="positive_task_revision"),
        UniqueConstraint(
            "generation_id", "task_id", "task_revision", name="uq_content_activation_revision"
        ),
        UniqueConstraint(
            "generation_id",
            "task_id",
            "content_version_id",
            name="uq_content_activation_version",
        ),
    )


class TaskAuthorityHead(Base):
    __tablename__ = "task_authority_heads"

    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), primary_key=True
    )
    current_content_activation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("task_content_activations.content_activation_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    task_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    membership_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    placement_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completion_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("task_revision > 0", name="positive_task_revision"),
        CheckConstraint("membership_revision >= 0", name="nonnegative_membership_revision"),
        CheckConstraint("placement_revision >= 0", name="nonnegative_placement_revision"),
        CheckConstraint("completion_revision > 0", name="positive_completion_revision"),
    )


class TaskProjectMembershipEvent(Base):
    __tablename__ = "task_project_membership_events"

    membership_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("governed_projects.project_id", ondelete="RESTRICT"), nullable=False
    )
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    membership_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provenance_route: Mapped[str] = mapped_column(String(24), nullable=False)
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT")
    )
    command_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("event_kind IN ('joined','left')", name="event_kind_allowed"),
        CheckConstraint(
            "provenance_route IN ('import','command_execution')", name="route_allowed"
        ),
        CheckConstraint(
            "(provenance_route = 'import' AND import_run_id IS NOT NULL "
            "AND command_execution_id IS NULL) OR "
            "(provenance_route = 'command_execution' AND import_run_id IS NULL "
            "AND command_execution_id IS NOT NULL)",
            name="exact_provenance_route",
        ),
        CheckConstraint("membership_revision > 0", name="positive_revision"),
        UniqueConstraint(
            "generation_id",
            "task_id",
            "project_id",
            "membership_revision",
            name="uq_membership_event_revision",
        ),
    )


class CurrentTaskProjectMembership(Base):
    __tablename__ = "current_task_project_memberships"

    generation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    latest_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("task_project_membership_events.membership_event_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    is_member: Mapped[bool] = mapped_column(Boolean, nullable=False)
    membership_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["generation_id", "task_id"],
            ["task_authority_heads.generation_id", "task_authority_heads.task_id"],
            ondelete="RESTRICT",
            name="fk_current_membership_task_head",
        ),
        ForeignKeyConstraint(
            ["project_id"], ["governed_projects.project_id"], ondelete="RESTRICT"
        ),
        CheckConstraint("membership_revision > 0", name="positive_revision"),
    )


class TaskSectionPlacementEvent(Base):
    __tablename__ = "task_section_placement_events"

    placement_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("governed_sections.section_id", ondelete="RESTRICT")
    )
    registry_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("section_registry_versions.registry_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    placement_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provenance_route: Mapped[str] = mapped_column(String(24), nullable=False)
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT")
    )
    command_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("event_kind IN ('placed','cleared')", name="event_kind_allowed"),
        CheckConstraint(
            "(event_kind = 'placed' AND section_id IS NOT NULL) OR "
            "(event_kind = 'cleared' AND section_id IS NULL)",
            name="section_matches_event",
        ),
        CheckConstraint(
            "provenance_route IN ('import','command_execution')", name="route_allowed"
        ),
        CheckConstraint(
            "(provenance_route = 'import' AND import_run_id IS NOT NULL "
            "AND command_execution_id IS NULL) OR "
            "(provenance_route = 'command_execution' AND import_run_id IS NULL "
            "AND command_execution_id IS NOT NULL)",
            name="exact_provenance_route",
        ),
        CheckConstraint("placement_revision > 0", name="positive_revision"),
        UniqueConstraint(
            "generation_id", "task_id", "placement_revision", name="uq_placement_event_revision"
        ),
    )


class CurrentTaskSectionPlacement(Base):
    __tablename__ = "current_task_section_placements"

    generation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("governed_sections.section_id", ondelete="RESTRICT")
    )
    registry_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("section_registry_versions.registry_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    latest_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("task_section_placement_events.placement_event_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    placement_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["generation_id", "task_id"],
            ["task_authority_heads.generation_id", "task_authority_heads.task_id"],
            ondelete="RESTRICT",
            name="fk_current_placement_task_head",
        ),
        CheckConstraint("placement_revision > 0", name="positive_revision"),
    )


class TaskCompletionEvent(Base):
    __tablename__ = "task_completion_events"

    completion_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    completion_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    provenance_route: Mapped[str] = mapped_column(String(24), nullable=False)
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT")
    )
    command_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "reason IN ('imported','cooked','archive','reopen_planning')", name="reason_allowed"
        ),
        CheckConstraint(
            "(reason = 'imported' AND provenance_route = 'import') OR "
            "(reason IN ('cooked','archive','reopen_planning') "
            "AND provenance_route = 'command_execution')",
            name="reason_matches_route",
        ),
        CheckConstraint(
            "provenance_route IN ('import','command_execution')", name="route_allowed"
        ),
        CheckConstraint(
            "(provenance_route = 'import' AND import_run_id IS NOT NULL "
            "AND command_execution_id IS NULL) OR "
            "(provenance_route = 'command_execution' AND import_run_id IS NULL "
            "AND command_execution_id IS NOT NULL)",
            name="exact_provenance_route",
        ),
        CheckConstraint("completion_revision > 0", name="positive_revision"),
        UniqueConstraint(
            "generation_id", "task_id", "completion_revision", name="uq_completion_event_revision"
        ),
    )


class CurrentTaskCompletion(Base):
    __tablename__ = "current_task_completion"

    generation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    latest_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("task_completion_events.completion_event_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    completion_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["generation_id", "task_id"],
            ["task_authority_heads.generation_id", "task_authority_heads.task_id"],
            ondelete="RESTRICT",
            name="fk_current_completion_task_head",
        ),
        CheckConstraint("completion_revision > 0", name="positive_revision"),
    )


IMMUTABLE_TABLE_NAMES = (
    "authority_activations",
    "applied_migration_events",
    "honest_contract_bindings",
    "section_registry_versions",
    "section_registry_entries",
    "section_registry_activations",
    "task_content_versions",
    "task_content_activations",
    "task_project_membership_events",
    "task_section_placement_events",
    "task_completion_events",
)


def _install_sqlite_immutability_triggers() -> None:
    for table_name in IMMUTABLE_TABLE_NAMES:
        table = Base.metadata.tables[table_name]
        safe = table_name.replace("-", "_")
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {safe}_immutable_update BEFORE UPDATE ON {table_name} "
                "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
            ).execute_if(dialect="sqlite"),
        )
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {safe}_immutable_delete BEFORE DELETE ON {table_name} "
                "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
            ).execute_if(dialect="sqlite"),
        )


_install_sqlite_immutability_triggers()

CORE_TABLE_NAMES = tuple(Base.metadata.tables)
