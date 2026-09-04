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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
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
            "creation_reason IN ('initial_cutover','destructive_restore','test_fixture_recovery')",
            name="creation_reason_allowed",
        ),
        CheckConstraint("status IN ('pending','active','retired')", name="status_allowed"),
        CheckConstraint(
            "(creation_reason = 'initial_cutover' AND predecessor_generation_id IS NULL "
            "AND external_restore_control_id IS NULL) OR "
            "(creation_reason = 'destructive_restore' AND predecessor_generation_id IS NOT NULL "
            "AND external_restore_control_id IS NOT NULL) OR "
            "(creation_reason = 'test_fixture_recovery' AND predecessor_generation_id IS NOT NULL "
            "AND external_restore_control_id IS NULL)",
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
    registry_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("section_registry_versions.registry_version_id", ondelete="RESTRICT", name="fk_authact_registry")
    )
    honest_binding_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("honest_contract_bindings.binding_id", ondelete="RESTRICT", name="fk_authact_honest")
    )
    rehearsal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("rehearsal_runs.rehearsal_id", ondelete="RESTRICT", name="fk_authact_rehearsal")
    )
    schema_head: Mapped[str] = mapped_column(String(64), nullable=False)
    dish_release: Mapped[str] = mapped_column(String(128), nullable=False)
    honest_release: Mapped[str] = mapped_column(String(128), nullable=False)
    protocol_release: Mapped[str] = mapped_column(String(128), nullable=False)
    openapi_release: Mapped[str] = mapped_column(String(128), nullable=False)
    routing_release: Mapped[str] = mapped_column(String(128), nullable=False)
    projection_epoch: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    rollback_burned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "length(trim(legacy_bundle_id)) > 0", name="legacy_bundle_nonblank"
        ),
        CheckConstraint(
            "(registry_version_id IS NULL AND honest_binding_id IS NULL) OR "
            "(registry_version_id IS NOT NULL AND honest_binding_id IS NOT NULL)",
            name="release_contract_identity_pair",
        ),
        CheckConstraint("outcome IN ('activated','aborted')", name="outcome_allowed"),
        CheckConstraint(
            "(outcome = 'activated' AND rollback_burned_at IS NOT NULL) OR "
            "(outcome = 'aborted' AND rollback_burned_at IS NULL)",
            name="rollback_burn_matches_outcome",
        ),
        Index(
            "uq_authority_activation_live_generation",
            "generation_id",
            unique=True,
            postgresql_where=text("outcome = 'activated'"),
            sqlite_where=text("outcome = 'activated'"),
        ),
        Index(
            "uq_authority_activation_live_projection_epoch",
            "projection_epoch",
            unique=True,
            postgresql_where=text("outcome = 'activated'"),
            sqlite_where=text("outcome = 'activated'"),
        ),
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
            "AND migration_metadata_sha256 IS NOT NULL "
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
        Index(
            "uq_honest_binding_null_identity",
            "binding_kind",
            "protocol_sha256",
            "schema_sha256",
            unique=True,
            postgresql_where=text(
                "migration_id IS NULL AND migration_metadata_sha256 IS NULL"
            ),
            sqlite_where=text(
                "migration_id IS NULL AND migration_metadata_sha256 IS NULL"
            ),
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


class Section(Base):
    """Stable native Section identity, independent of legacy Project topology."""

    __tablename__ = "sections"

    section_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    logical_name: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    lifecycle: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("lifecycle IN ('active','retired')", name="lifecycle_allowed"),
        CheckConstraint("length(trim(logical_name)) > 0", name="logical_name_nonblank"),
        CheckConstraint(
            "(lifecycle = 'active' AND retired_at IS NULL) OR "
            "(lifecycle = 'retired' AND retired_at IS NOT NULL)",
            name="retirement_consistent",
        ),
    )


class SectionCatalogVersion(Base):
    """Immutable native catalog definition bound to one Honest contract."""

    __tablename__ = "section_catalog_versions"

    catalog_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    version_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    contract_binding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("honest_contract_bindings.binding_id", ondelete="RESTRICT"),
        nullable=False,
    )
    catalog_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_registry_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("section_registry_versions.registry_version_id", ondelete="RESTRICT"),
    )
    transform_sha256: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("version_number > 0", name="positive_version"),
        CheckConstraint("length(catalog_sha256) = 64", name="catalog_hash_length"),
        CheckConstraint(
            "(source_registry_version_id IS NULL AND transform_sha256 IS NULL) OR "
            "(source_registry_version_id IS NOT NULL AND length(transform_sha256) = 64)",
            name="transition_transform_exact",
        ),
        UniqueConstraint(
            "generation_id", "version_number", name="uq_catalog_generation_version"
        ),
    )


class SectionCatalogEntry(Base):
    __tablename__ = "section_catalog_entries"

    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("section_catalog_versions.catalog_version_id", ondelete="CASCADE"),
        primary_key=True,
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("sections.section_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    workflow_role: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        CheckConstraint("ordinal >= 0", name="nonnegative_ordinal"),
        CheckConstraint("length(trim(display_name)) > 0", name="display_name_nonblank"),
        CheckConstraint("length(trim(workflow_role)) > 0", name="workflow_role_nonblank"),
        UniqueConstraint("catalog_version_id", "ordinal", name="uq_catalog_entry_ordinal"),
        UniqueConstraint(
            "catalog_version_id", "workflow_role", name="uq_catalog_entry_workflow_role"
        ),
    )


class SectionCatalogActivation(Base):
    __tablename__ = "section_catalog_activations"

    catalog_activation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("section_catalog_versions.catalog_version_id", ondelete="RESTRICT"),
        nullable=False,
    )
    activation_route: Mapped[str] = mapped_column(String(24), nullable=False)
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT"),
    )
    command_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    catalog_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "activation_route IN ('transition','command_execution','recovery')",
            name="route_allowed",
        ),
        CheckConstraint(
            "(activation_route = 'transition' AND import_run_id IS NOT NULL "
            "AND command_execution_id IS NULL) OR "
            "(activation_route = 'command_execution' AND import_run_id IS NULL "
            "AND command_execution_id IS NOT NULL) OR "
            "(activation_route = 'recovery' AND import_run_id IS NULL "
            "AND command_execution_id IS NULL)",
            name="exact_provenance_route",
        ),
        CheckConstraint("catalog_revision > 0", name="positive_revision"),
        UniqueConstraint(
            "generation_id", "catalog_revision", name="uq_catalog_activation_revision"
        ),
        UniqueConstraint(
            "generation_id", "catalog_version_id", name="uq_catalog_activation_version"
        ),
    )


class ActiveSectionCatalog(Base):
    """Current native catalog definition; it is not runtime-switch authority."""

    __tablename__ = "active_section_catalogs"

    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    catalog_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("section_catalog_versions.catalog_version_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    catalog_activation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("section_catalog_activations.catalog_activation_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    catalog_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (CheckConstraint("catalog_revision > 0", name="positive_revision"),)


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
    created_dish_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
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
        ForeignKeyConstraint(
            ["generation_id", "task_id", "created_dish_version"],
            [
                "dish_mutation_receipts.generation_id",
                "dish_mutation_receipts.task_id",
                "dish_mutation_receipts.dish_version",
            ],
            ondelete="RESTRICT",
            name="fk_content_version_creation_receipt",
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
        CheckConstraint("created_dish_version > 0", name="positive_created_dish_version"),
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
        UniqueConstraint(
            "generation_id",
            "task_id",
            "created_dish_version",
            name="uq_content_created_dish_version",
        ),
    )


class NativeSectionContentCarryForwardOccurrence(Base):
    """Immutable PR3 transition evidence for one future native-content occurrence."""

    __tablename__ = "native_section_content_carry_forward_occurrences"

    carry_forward_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    source_content_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_dish_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_content_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    source_status: Mapped[str | None] = mapped_column(String(64))
    target_catalog_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    target_section_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    destination_legacy_gid: Mapped[str] = mapped_column(String(32), nullable=False)
    destination_display_name: Mapped[str] = mapped_column(String(256), nullable=False)
    transformed_title: Mapped[str] = mapped_column(Text, nullable=False)
    transformed_body: Mapped[str] = mapped_column(Text, nullable=False)
    transformed_content_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    verification_baseline_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    verification_baseline_text: Mapped[str | None] = mapped_column(Text)
    transform_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    import_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT"), nullable=False
    )
    migration_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("applied_migration_events.migration_event_id", ondelete="RESTRICT"), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["generation_id", "task_id", "source_content_version_id"],
            [
                "task_content_versions.generation_id",
                "task_content_versions.task_id",
                "task_content_versions.content_version_id",
            ],
            ondelete="RESTRICT",
            name="fk_native_section_carry_forward_exact_source_content",
        ),
        ForeignKeyConstraint(
            ["generation_id", "task_id", "source_dish_version"],
            [
                "dish_mutation_receipts.generation_id",
                "dish_mutation_receipts.task_id",
                "dish_mutation_receipts.dish_version",
            ],
            ondelete="RESTRICT",
            name="fk_native_section_carry_forward_exact_source_version",
        ),
        ForeignKeyConstraint(
            ["target_catalog_version_id", "target_section_id"],
            [
                "section_catalog_entries.catalog_version_id",
                "section_catalog_entries.section_id",
            ],
            ondelete="RESTRICT",
            name="fk_native_section_carry_forward_exact_target_entry",
        ),
        CheckConstraint("source_dish_version > 0", name="positive_source_dish_version"),
        CheckConstraint("length(trim(destination_legacy_gid)) > 0", name="legacy_gid_nonblank"),
        CheckConstraint("length(trim(destination_display_name)) > 0", name="display_name_nonblank"),
        CheckConstraint("length(source_content_identity) > 0", name="source_identity_nonblank"),
        CheckConstraint("length(transformed_content_identity) > 0", name="transformed_identity_nonblank"),
        CheckConstraint("length(transform_sha256) = 64", name="transform_hash_length"),
        CheckConstraint(
            "verification_baseline_kind IN ('none','migration_assigned_ready')",
            name="verification_baseline_kind_allowed",
        ),
        CheckConstraint(
            "(verification_baseline_kind = 'none' AND verification_baseline_text IS NULL) OR "
            "(verification_baseline_kind = 'migration_assigned_ready' AND verification_baseline_text IS NOT NULL)",
            name="verification_baseline_exact",
        ),
        UniqueConstraint("generation_id", "task_id", name="uq_native_section_carry_forward_task"),
        UniqueConstraint(
            "generation_id",
            "task_id",
            "source_content_version_id",
            name="uq_native_section_carry_forward_source_content",
        ),
    )


class DishMutationReceipt(Base):
    __tablename__ = "dish_mutation_receipts"

    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), primary_key=True
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), primary_key=True
    )
    dish_version: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    source_route: Mapped[str] = mapped_column(String(24), nullable=False)
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT")
    )
    command_execution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    content_changed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    placement_changed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    completion_changed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    archive_changed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["command_execution_id", "generation_id", "task_id"],
            [
                "command_executions.execution_id",
                "command_executions.generation_id",
                "command_executions.task_id",
            ],
            ondelete="RESTRICT",
            name="fk_dish_mutation_receipt_exact_execution",
        ),
        CheckConstraint("dish_version > 0", name="positive_dish_version"),
        CheckConstraint("source_route IN ('import','command_execution')", name="source_route_allowed"),
        CheckConstraint(
            "(source_route = 'import' AND import_run_id IS NOT NULL AND command_execution_id IS NULL) OR "
            "(source_route = 'command_execution' AND import_run_id IS NULL AND command_execution_id IS NOT NULL)",
            name="exact_source",
        ),
        CheckConstraint(
            "content_changed OR placement_changed OR completion_changed OR archive_changed",
            name="at_least_one_effect",
        ),
        Index(
            "uq_dish_mutation_receipt_execution",
            "command_execution_id",
            unique=True,
            postgresql_where=text("command_execution_id IS NOT NULL"),
            sqlite_where=text("command_execution_id IS NOT NULL"),
        ),
    )


class DishState(Base):
    __tablename__ = "dish_states"

    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), primary_key=True
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), primary_key=True
    )
    current_content_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    section_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("governed_sections.section_id", ondelete="RESTRICT")
    )
    registry_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("section_registry_versions.registry_version_id", ondelete="RESTRICT"), nullable=False
    )
    completed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    completion_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dish_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    placement_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    completion_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["generation_id", "task_id", "current_content_version_id"],
            [
                "task_content_versions.generation_id",
                "task_content_versions.task_id",
                "task_content_versions.content_version_id",
            ],
            ondelete="RESTRICT",
            name="fk_dish_state_exact_content",
        ),
        ForeignKeyConstraint(
            ["generation_id", "task_id", "dish_version"],
            ["dish_mutation_receipts.generation_id", "dish_mutation_receipts.task_id", "dish_mutation_receipts.dish_version"],
            ondelete="RESTRICT",
            name="fk_dish_state_current_receipt",
        ),
        ForeignKeyConstraint(
            ["generation_id", "task_id", "placement_version"],
            ["dish_mutation_receipts.generation_id", "dish_mutation_receipts.task_id", "dish_mutation_receipts.dish_version"],
            ondelete="RESTRICT",
            name="fk_dish_state_placement_receipt",
        ),
        ForeignKeyConstraint(
            ["generation_id", "task_id", "completion_version"],
            ["dish_mutation_receipts.generation_id", "dish_mutation_receipts.task_id", "dish_mutation_receipts.dish_version"],
            ondelete="RESTRICT",
            name="fk_dish_state_completion_receipt",
        ),
        CheckConstraint("dish_version > 0", name="positive_dish_version"),
        CheckConstraint("placement_version > 0", name="positive_placement_version"),
        CheckConstraint("completion_version > 0", name="positive_completion_version"),
        CheckConstraint("placement_version <= dish_version", name="placement_not_future"),
        CheckConstraint("completion_version <= dish_version", name="completion_not_future"),
        CheckConstraint(
            "completion_reason IN ('imported','cooked','archive','reopen_planning')",
            name="completion_reason_allowed",
        ),
        Index("ix_dish_states_section", "generation_id", "section_id", "task_id"),
        Index("ix_dish_states_board", "generation_id", "completed", "section_id", "task_id"),
        Index("ix_dish_states_registry", "generation_id", "registry_version_id", "task_id"),
        Index("ix_dish_states_archive", "generation_id", "archived_at", "task_id"),
    )


class TaskMembershipHead(Base):
    __tablename__ = "task_membership_heads"

    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), primary_key=True
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), primary_key=True
    )
    membership_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("membership_revision >= 0", name="nonnegative_membership_revision"),
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
            ["task_membership_heads.generation_id", "task_membership_heads.task_id"],
            ondelete="RESTRICT",
            name="fk_current_membership_task_head",
        ),
        ForeignKeyConstraint(
            ["project_id"], ["governed_projects.project_id"], ondelete="RESTRICT"
        ),
        CheckConstraint("membership_revision > 0", name="positive_revision"),
    )


IMMUTABLE_TABLE_NAMES = (
    "authority_activations",
    "applied_migration_events",
    "honest_contract_bindings",
    "section_registry_versions",
    "section_registry_entries",
    "section_registry_activations",
    "section_catalog_versions",
    "section_catalog_entries",
    "section_catalog_activations",
    "task_content_versions",
    "dish_mutation_receipts",
    "task_project_membership_events",
)


def _install_sqlite_immutability_triggers() -> None:
    for table_name in IMMUTABLE_TABLE_NAMES:
        table = Base.metadata.tables[table_name]
        safe = table_name.replace("-", "_")
        if table_name == "authority_activations":
            update_ddl = DDL(
                """
                CREATE TRIGGER authority_activations_immutable_update
                BEFORE UPDATE ON authority_activations
                BEGIN
                    SELECT CASE WHEN
                        OLD.activation_id IS NOT NEW.activation_id
                        OR OLD.generation_id IS NOT NEW.generation_id
                        OR OLD.import_run_id IS NOT NEW.import_run_id
                        OR OLD.cutover_approval_id IS NOT NEW.cutover_approval_id
                        OR OLD.legacy_bundle_id IS NOT NEW.legacy_bundle_id
                        OR OLD.registry_version_id IS NOT NEW.registry_version_id
                        OR OLD.honest_binding_id IS NOT NEW.honest_binding_id
                        OR OLD.rehearsal_id IS NOT NEW.rehearsal_id
                        OR OLD.schema_head IS NOT NEW.schema_head
                        OR OLD.dish_release IS NOT NEW.dish_release
                        OR OLD.honest_release IS NOT NEW.honest_release
                        OR OLD.protocol_release IS NOT NEW.protocol_release
                        OR OLD.openapi_release IS NOT NEW.openapi_release
                        OR OLD.routing_release IS NOT NEW.routing_release
                        OR OLD.projection_epoch IS NOT NEW.projection_epoch
                        OR OLD.recorded_at IS NOT NEW.recorded_at
                    THEN RAISE(ABORT, 'immutable authority row') END;
                    SELECT CASE WHEN
                        OLD.outcome <> 'activated'
                        OR NEW.outcome <> 'aborted'
                        OR OLD.rehearsal_id IS NULL
                        OR NEW.rollback_burned_at IS NOT NULL
                        OR NOT EXISTS (
                            SELECT 1
                              FROM cutover_runs cr
                              JOIN rehearsal_runs rr
                                ON rr.rehearsal_id = cr.rehearsal_id
                             WHERE cr.rehearsal_id = OLD.rehearsal_id
                               AND cr.state = 'rehearsal_torn_down'
                               AND rr.status = 'running'
                               AND rr.rehearsal_kind = 'cutover'
                        )
                    THEN RAISE(ABORT, 'immutable authority row') END;
                END
                """
            )
        else:
            update_ddl = DDL(
                f"CREATE TRIGGER {safe}_immutable_update BEFORE UPDATE ON {table_name} "
                "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
            )
        event.listen(
            table,
            "after_create",
            update_ddl.execute_if(dialect="sqlite"),
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


def _install_sqlite_native_catalog_triggers() -> None:
    section = Base.metadata.tables["sections"]
    version = Base.metadata.tables["section_catalog_versions"]
    entry = Base.metadata.tables["section_catalog_entries"]
    activation = Base.metadata.tables["section_catalog_activations"]
    active = Base.metadata.tables["active_section_catalogs"]

    event.listen(
        section,
        "after_create",
        DDL(
            "CREATE TRIGGER sections_identity_immutable BEFORE UPDATE OF section_id ON sections "
            "BEGIN SELECT RAISE(ABORT, 'Section identity is immutable'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        active,
        "after_create",
        DDL(
            "CREATE TRIGGER sections_active_catalog_retirement_guard "
            "BEFORE UPDATE OF lifecycle ON sections "
            "WHEN NEW.lifecycle='retired' AND EXISTS ("
            "SELECT 1 FROM active_section_catalogs a "
            "JOIN authority_generations g ON g.generation_id=a.generation_id "
            "JOIN section_catalog_entries e "
            "ON e.catalog_version_id=a.catalog_version_id "
            "WHERE e.section_id=OLD.section_id AND g.status='active') "
            "BEGIN SELECT RAISE(ABORT, 'active catalog Section cannot be retired'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        section,
        "after_create",
        DDL(
            "CREATE TRIGGER sections_delete_forbidden BEFORE DELETE ON sections "
            "BEGIN SELECT RAISE(ABORT, 'Section cannot be deleted'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        version,
        "after_create",
        DDL(
            "CREATE TRIGGER section_catalog_versions_binding_validate "
            "BEFORE INSERT ON section_catalog_versions WHEN NOT EXISTS ("
            "SELECT 1 FROM authority_generations g JOIN honest_contract_bindings b "
            "ON b.binding_id=NEW.contract_binding_id WHERE g.generation_id=NEW.generation_id "
            "AND g.status='active' AND b.binding_kind='release' "
            "AND b.dish_release=g.dish_release) "
            "BEGIN SELECT RAISE(ABORT, 'native catalog Honest binding mismatch'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        entry,
        "after_create",
        DDL(
            "CREATE TRIGGER section_catalog_entries_section_validate "
            "BEFORE INSERT ON section_catalog_entries WHEN NOT EXISTS ("
            "SELECT 1 FROM sections s WHERE s.section_id=NEW.section_id "
            "AND s.lifecycle='active') "
            "BEGIN SELECT RAISE(ABORT, 'native catalog entry requires active Section'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        activation,
        "after_create",
        DDL(
            "CREATE TRIGGER section_catalog_activations_version_validate "
            "BEFORE INSERT ON section_catalog_activations WHEN NOT EXISTS ("
            "SELECT 1 FROM section_catalog_versions v "
            "WHERE v.catalog_version_id=NEW.catalog_version_id "
            "AND v.generation_id=NEW.generation_id "
            "AND v.version_number=NEW.catalog_revision) "
            "BEGIN SELECT RAISE(ABORT, 'native catalog activation mismatch'); END"
        ).execute_if(dialect="sqlite"),
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
        "JOIN sections s ON s.section_id=e.section_id "
        "WHERE e.catalog_version_id=NEW.catalog_version_id) "
        "AND NOT EXISTS (SELECT 1 FROM section_catalog_entries e "
        "JOIN sections s ON s.section_id=e.section_id "
        "WHERE e.catalog_version_id=NEW.catalog_version_id AND s.lifecycle<>'active'))"
    )
    event.listen(
        active,
        "after_create",
        DDL(
            "CREATE TRIGGER active_section_catalogs_validate_insert "
            "BEFORE INSERT ON active_section_catalogs WHEN "
            f"NEW.catalog_revision<>1 OR NOT {pointer_match} "
            "BEGIN SELECT RAISE(ABORT, 'active native catalog pointer is invalid'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        active,
        "after_create",
        DDL(
            "CREATE TRIGGER active_section_catalogs_validate_update "
            "BEFORE UPDATE ON active_section_catalogs WHEN "
            "NEW.generation_id<>OLD.generation_id OR "
            "NEW.catalog_revision<>OLD.catalog_revision+1 OR "
            f"NOT {pointer_match} "
            "BEGIN SELECT RAISE(ABORT, 'active native catalog transition is invalid'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        active,
        "after_create",
        DDL(
            "CREATE TRIGGER active_section_catalogs_delete_forbidden "
            "BEFORE DELETE ON active_section_catalogs "
            "BEGIN SELECT RAISE(ABORT, 'active native catalog cannot be deleted'); END"
        ).execute_if(dialect="sqlite"),
    )


_install_sqlite_native_catalog_triggers()


def _install_sqlite_scalar_authority_triggers() -> None:
    dish_task = Base.metadata.tables["dish_tasks"]
    content_version = Base.metadata.tables["task_content_versions"]
    dish_state = Base.metadata.tables["dish_states"]
    membership_head = Base.metadata.tables["task_membership_heads"]
    current_membership = Base.metadata.tables["current_task_project_memberships"]
    native_carry_forward = Base.metadata.tables[
        "native_section_content_carry_forward_occurrences"
    ]

    for operation in ("UPDATE", "DELETE"):
        event.listen(
            native_carry_forward,
            "after_create",
            DDL(
                f"CREATE TRIGGER native_section_content_carry_forward_immutable_{operation.lower()} "
                f"BEFORE {operation} ON native_section_content_carry_forward_occurrences "
                "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
            ).execute_if(dialect="sqlite"),
        )

    event.listen(
        dish_task,
        "after_create",
        DDL(
            "CREATE TRIGGER dish_tasks_creation_provenance_immutable "
            "BEFORE UPDATE OF task_id, creation_route, import_run_id, command_execution_id, created_at "
            "ON dish_tasks BEGIN SELECT RAISE(ABORT, 'DishTask creation provenance is immutable'); END"
        ).execute_if(dialect="sqlite"),
    )
    for table, name in ((dish_state, "dish_states"), (membership_head, "task_membership_heads")):
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {name}_identity_immutable BEFORE UPDATE OF generation_id, task_id "
                f"ON {name} BEGIN SELECT RAISE(ABORT, '{name} identity is immutable'); END"
            ).execute_if(dialect="sqlite"),
        )
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {name}_delete_forbidden BEFORE DELETE ON {name} "
                f"BEGIN SELECT RAISE(ABORT, '{name} cannot be deleted'); END"
            ).execute_if(dialect="sqlite"),
        )

    source_match = (
        "((r.source_route = 'import' AND r.import_run_id IS cv.import_run_id "
        "AND cv.creator_route = 'import') OR "
        "(r.source_route = 'command_execution' AND r.command_execution_id IS cv.command_execution_id "
        "AND cv.creator_route = 'command_execution' AND EXISTS ("
        "SELECT 1 FROM command_executions ce WHERE ce.execution_id=cv.command_execution_id "
        "AND ce.generation_id=cv.generation_id AND ce.task_id=cv.task_id "
        "AND ce.contract_binding_id=cv.contract_binding_id)))"
    )
    event.listen(
        content_version,
        "after_create",
        DDL(
            "CREATE TRIGGER task_content_versions_scalar_source_validate "
            "BEFORE INSERT ON task_content_versions WHEN NOT EXISTS ("
            "SELECT 1 FROM dish_mutation_receipts r WHERE r.generation_id=NEW.generation_id "
            "AND r.task_id=NEW.task_id AND r.dish_version=NEW.created_dish_version "
            "AND r.content_changed=1 AND ((r.source_route='import' "
            "AND NEW.creator_route='import' AND r.import_run_id IS NEW.import_run_id) OR "
            "(r.source_route='command_execution' AND NEW.creator_route='command_execution' "
            "AND r.command_execution_id IS NEW.command_execution_id AND EXISTS ("
            "SELECT 1 FROM command_executions ce WHERE ce.execution_id=NEW.command_execution_id "
            "AND ce.generation_id=NEW.generation_id AND ce.task_id=NEW.task_id "
            "AND ce.contract_binding_id=NEW.contract_binding_id))) "
            "AND (NEW.created_dish_version<>1 OR EXISTS ("
            "SELECT 1 FROM authority_generations g WHERE g.generation_id=NEW.generation_id "
            "AND g.creation_reason IN ('destructive_restore','test_fixture_recovery')) OR EXISTS ("
            "SELECT 1 FROM dish_tasks t WHERE t.task_id=NEW.task_id AND ("
            "(t.creation_route='import' AND NEW.creator_route='import' "
            "AND t.import_run_id IS NEW.import_run_id) OR "
            "(t.creation_route='create' AND NEW.creator_route='command_execution' "
            "AND t.command_execution_id IS NEW.command_execution_id))))) "
            "BEGIN SELECT RAISE(ABORT, 'content creation receipt mismatch'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        dish_state,
        "after_create",
        DDL(
            "CREATE TRIGGER dish_states_validate_insert BEFORE INSERT ON dish_states WHEN "
            "NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r WHERE r.generation_id=NEW.generation_id "
            "AND r.task_id=NEW.task_id AND r.dish_version=NEW.dish_version) OR "
            "NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r WHERE r.generation_id=NEW.generation_id "
            "AND r.task_id=NEW.task_id AND r.dish_version=NEW.placement_version "
            "AND r.placement_changed=1) OR "
            "NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r WHERE r.generation_id=NEW.generation_id "
            "AND r.task_id=NEW.task_id AND r.dish_version=NEW.completion_version "
            "AND r.completion_changed=1) OR "
            "NOT EXISTS (SELECT 1 FROM task_content_versions cv JOIN dish_mutation_receipts r "
            "ON r.generation_id=cv.generation_id AND r.task_id=cv.task_id "
            "AND r.dish_version=cv.created_dish_version WHERE cv.generation_id=NEW.generation_id "
            "AND cv.task_id=NEW.task_id AND cv.content_version_id=NEW.current_content_version_id "
            "AND r.content_changed=1 AND " + source_match + ") OR "
            "NOT EXISTS (SELECT 1 FROM authority_generations g WHERE g.generation_id=NEW.generation_id "
            "AND (g.creation_reason='destructive_restore' OR (NEW.dish_version=1 "
            "AND NEW.placement_version=1 AND NEW.completion_version=1 "
            "AND EXISTS (SELECT 1 FROM task_content_versions cv WHERE "
            "cv.generation_id=NEW.generation_id AND cv.task_id=NEW.task_id "
            "AND cv.content_version_id=NEW.current_content_version_id "
            "AND cv.created_dish_version=1)))) OR "
            "EXISTS (SELECT 1 FROM dish_mutation_receipts r "
            "JOIN task_content_versions cv ON cv.generation_id=NEW.generation_id "
            "AND cv.task_id=NEW.task_id AND cv.content_version_id=NEW.current_content_version_id "
            "WHERE r.generation_id=NEW.generation_id AND r.task_id=NEW.task_id "
            "AND r.dish_version IN (NEW.dish_version, NEW.placement_version, "
            "NEW.completion_version, cv.created_dish_version) AND ("
            "r.content_changed <> (r.dish_version=cv.created_dish_version) OR "
            "r.placement_changed <> (r.dish_version=NEW.placement_version) OR "
            "r.completion_changed <> (r.dish_version=NEW.completion_version) OR "
            "r.archive_changed)) OR "
            "NOT EXISTS (SELECT 1 FROM section_registry_entries e WHERE e.registry_version_id=NEW.registry_version_id "
            "AND (NEW.section_id IS NULL OR e.section_id=NEW.section_id)) OR "
            "NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r WHERE r.generation_id=NEW.generation_id "
            "AND r.task_id=NEW.task_id AND r.dish_version=NEW.completion_version "
            "AND ((r.source_route='import' AND NEW.completion_reason='imported') OR "
            "(r.source_route='command_execution' AND NEW.completion_reason IN ('cooked','archive','reopen_planning')))) "
            "BEGIN SELECT RAISE(ABORT, 'invalid initial DishState authority'); END"
        ).execute_if(dialect="sqlite"),
    )
    for operation in ("INSERT", "UPDATE"):
        event.listen(
            current_membership,
            "after_create",
            DDL(
                f"CREATE TRIGGER current_task_project_memberships_validate_{operation.lower()} "
                f"BEFORE {operation} ON current_task_project_memberships WHEN NOT EXISTS ("
                "SELECT 1 FROM task_project_membership_events e "
                "WHERE e.membership_event_id=NEW.latest_event_id "
                "AND e.generation_id=NEW.generation_id AND e.task_id=NEW.task_id "
                "AND e.project_id=NEW.project_id "
                "AND e.membership_revision=NEW.membership_revision "
                "AND ((e.event_kind='joined' AND NEW.is_member=1) "
                "OR (e.event_kind='left' AND NEW.is_member=0))) "
                "BEGIN SELECT RAISE(ABORT, 'current project membership pointer is invalid'); END"
            ).execute_if(dialect="sqlite"),
        )
    event.listen(
        dish_state,
        "after_create",
        DDL(
            "CREATE TRIGGER dish_states_validate_update BEFORE UPDATE ON dish_states WHEN "
            "NEW.dish_version <> OLD.dish_version + 1 OR "
            "NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r WHERE r.generation_id=NEW.generation_id "
            "AND r.task_id=NEW.task_id AND r.dish_version=NEW.dish_version "
            "AND r.content_changed = (NEW.current_content_version_id IS NOT OLD.current_content_version_id) "
            "AND r.placement_changed = (NEW.placement_version <> OLD.placement_version) "
            "AND r.completion_changed = (NEW.completion_version <> OLD.completion_version) "
            "AND r.archive_changed = (NEW.archived_at IS NOT OLD.archived_at) "
            "AND (r.archive_changed=0 OR r.source_route='command_execution')) OR "
            "((NEW.placement_version = OLD.placement_version) AND "
            "(NEW.section_id IS NOT OLD.section_id OR NEW.registry_version_id <> OLD.registry_version_id)) OR "
            "((NEW.placement_version <> OLD.placement_version) AND NEW.placement_version <> NEW.dish_version) OR "
            "((NEW.completion_version = OLD.completion_version) AND "
            "(NEW.completed <> OLD.completed OR NEW.completion_reason <> OLD.completion_reason)) OR "
            "((NEW.completion_version <> OLD.completion_version) AND NEW.completion_version <> NEW.dish_version) OR "
            "NOT EXISTS (SELECT 1 FROM task_content_versions cv JOIN dish_mutation_receipts r "
            "ON r.generation_id=cv.generation_id AND r.task_id=cv.task_id AND r.dish_version=cv.created_dish_version "
            "WHERE cv.generation_id=NEW.generation_id AND cv.task_id=NEW.task_id "
            "AND cv.content_version_id=NEW.current_content_version_id "
            "AND ((NEW.current_content_version_id=OLD.current_content_version_id) OR cv.created_dish_version=NEW.dish_version) "
            "AND " + source_match + ") OR "
            "NOT EXISTS (SELECT 1 FROM section_registry_entries e WHERE e.registry_version_id=NEW.registry_version_id "
            "AND (NEW.section_id IS NULL OR e.section_id=NEW.section_id)) OR "
            "NOT EXISTS (SELECT 1 FROM dish_mutation_receipts r WHERE r.generation_id=NEW.generation_id "
            "AND r.task_id=NEW.task_id AND r.dish_version=NEW.completion_version "
            "AND ((r.source_route='import' AND NEW.completion_reason='imported') OR "
            "(r.source_route='command_execution' AND "
            "NEW.completion_reason IN ('cooked','archive','reopen_planning')))) "
            "BEGIN SELECT RAISE(ABORT, 'invalid DishState transition'); END"
        ).execute_if(dialect="sqlite"),
    )


_install_sqlite_scalar_authority_triggers()


@event.listens_for(Session, "before_commit")
def _validate_sqlite_active_registry_bindings(session: Session) -> None:
    """Emulate the deferred PostgreSQL registry-final-state guard on SQLite."""
    bind = session.get_bind()
    if bind.dialect.name != "sqlite":
        return
    connection = session.connection()
    tables = {
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('dish_states','active_section_registries','section_registry_entries')"
        )
    }
    if tables != {"dish_states", "active_section_registries", "section_registry_entries"}:
        return
    session.flush()
    mismatch = connection.exec_driver_sql(
        "SELECT 1 FROM dish_states s "
        "LEFT JOIN active_section_registries a ON a.generation_id=s.generation_id "
        "WHERE a.generation_id IS NULL OR a.registry_version_id<>s.registry_version_id "
        "OR (s.section_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM section_registry_entries e "
        "WHERE e.registry_version_id=s.registry_version_id AND e.section_id=s.section_id)) "
        "LIMIT 1"
    ).first()
    if mismatch is not None:
        raise IntegrityError(
            "DishState registry binding is not transaction-final",
            params=None,
            orig=RuntimeError("DishState registry binding is not transaction-final"),
        )

CORE_TABLE_NAMES = tuple(Base.metadata.tables)
