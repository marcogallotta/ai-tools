"""Stage 5 import, shadow, reconciliation, and Asana projection authority."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.schema import DDL

from .models import Base


class SourceImportBatch(Base):
    __tablename__ = "source_import_batches"

    import_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    import_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    source_release: Mapped[str] = mapped_column(String(128), nullable=False)
    source_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    source_database_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sidecars: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    ledger_through_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    expected_entities: Mapped[int] = mapped_column(BigInteger, nullable=False)
    imported_entities: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="capturing")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("length(source_database_sha256) = 64", name="source_hash_length"),
        CheckConstraint("expected_entities >= 0 AND imported_entities >= 0", name="nonnegative_counts"),
        CheckConstraint("imported_entities <= expected_entities", name="count_not_over_expected"),
        CheckConstraint("status IN ('capturing','complete','failed')", name="status_allowed"),
        CheckConstraint(
            "(status = 'capturing' AND completed_at IS NULL) OR "
            "(status IN ('complete','failed') AND completed_at IS NOT NULL)",
            name="terminal_time_consistent",
        ),
    )


class SourceImportEntityEvidence(Base):
    __tablename__ = "source_import_entity_evidence"

    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    import_batch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("source_import_batches.import_batch_id", ondelete="RESTRICT"), nullable=False
    )
    entity_kind: Mapped[str] = mapped_column(String(48), nullable=False)
    source_identity: Mapped[str] = mapped_column(String(512), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    target_entity_type: Mapped[str] = mapped_column(String(48), nullable=False)
    target_entity_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    provenance: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(source_sha256) = 64", name="source_hash_length"),
        UniqueConstraint(
            "import_batch_id", "entity_kind", "source_identity", name="uq_import_entity_source"
        ),
        UniqueConstraint(
            "import_batch_id", "target_entity_type", "target_entity_id", name="uq_import_entity_target"
        ),
    )


class ShadowBaseline(Base):
    __tablename__ = "shadow_baselines"

    shadow_baseline_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    source_generation_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    source_commit: Mapped[str] = mapped_column(String(64), nullable=False)
    baseline_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    disqualification_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("baseline_sequence > 0", name="positive_sequence"),
        CheckConstraint("status IN ('open','closed','disqualified')", name="status_allowed"),
        CheckConstraint(
            "(status = 'open' AND terminal_at IS NULL AND disqualification_reason IS NULL) OR "
            "(status = 'closed' AND terminal_at IS NOT NULL AND disqualification_reason IS NULL) OR "
            "(status = 'disqualified' AND terminal_at IS NOT NULL AND disqualification_reason IS NOT NULL)",
            name="terminal_payload_consistent",
        ),
        UniqueConstraint("generation_id", "baseline_sequence", name="uq_shadow_baseline_sequence"),
        Index(
            "uq_shadow_baseline_one_open_generation",
            "generation_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
    )


class ShadowEnvelope(Base):
    __tablename__ = "shadow_envelopes"

    envelope_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    shadow_baseline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("shadow_baselines.shadow_baseline_id", ondelete="RESTRICT"), nullable=False
    )
    command_name: Mapped[str] = mapped_column(String(64), nullable=False)
    source_request_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    canonical_input: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    canonical_input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_outcome: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    source_outcome_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_post_state: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    rollout_sequence: Mapped[int | None] = mapped_column(BigInteger)
    source_authority_generation: Mapped[str | None] = mapped_column(String(256))
    source_execution_identity: Mapped[str | None] = mapped_column(String(256))
    principal: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    source_pre_state: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    source_pre_state_sha256: Mapped[str | None] = mapped_column(String(64))
    pinned_inputs: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    source_effects: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    capture_qualification: Mapped[str] = mapped_column(String(24), nullable=False, default="legacy")
    source_post_state_sha256: Mapped[str | None] = mapped_column(String(64))
    envelope_schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "length(canonical_input_sha256) = 64 AND length(source_outcome_sha256) = 64",
            name="hash_lengths",
        ),
        CheckConstraint(
            "source_pre_state_sha256 IS NULL OR length(source_pre_state_sha256) = 64",
            name="pre_state_hash_length",
        ),
        CheckConstraint(
            "source_post_state_sha256 IS NULL OR length(source_post_state_sha256) = 64",
            name="post_state_hash_length",
        ),
        CheckConstraint(
            "rollout_sequence IS NULL OR rollout_sequence > 0",
            name="positive_rollout_sequence",
        ),
        CheckConstraint(
            "capture_qualification IN ('legacy','execute','capture_only','excluded')",
            name="capture_qualification_allowed",
        ),
        CheckConstraint("envelope_schema_version > 0", name="positive_schema_version"),
        UniqueConstraint(
            "shadow_baseline_id", "source_request_identity", name="uq_shadow_source_request"
        ),
        Index(
            "uq_shadow_rollout_sequence",
            "shadow_baseline_id",
            "rollout_sequence",
            unique=True,
            postgresql_where=text("rollout_sequence IS NOT NULL"),
            sqlite_where=text("rollout_sequence IS NOT NULL"),
        ),
    )


class ShadowDelivery(Base):
    __tablename__ = "shadow_deliveries"

    delivery_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    envelope_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("shadow_envelopes.envelope_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    claim_owner: Mapped[str | None] = mapped_column(String(256))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("state IN ('pending','claimed','delivered','failed')", name="state_allowed"),
        CheckConstraint("delivery_revision > 0 AND attempts >= 0", name="revision_counts_positive"),
        CheckConstraint(
            "(state = 'claimed' AND claim_owner IS NOT NULL AND claim_token IS NOT NULL "
            "AND claim_expires_at IS NOT NULL AND terminal_at IS NULL) OR "
            "(state = 'pending' AND claim_owner IS NULL AND claim_token IS NULL "
            "AND claim_expires_at IS NULL AND terminal_at IS NULL) OR "
            "(state IN ('delivered','failed') AND claim_owner IS NULL AND claim_token IS NULL "
            "AND claim_expires_at IS NULL AND terminal_at IS NOT NULL)",
            name="claim_state_consistent",
        ),
    )


class ShadowComparison(Base):
    __tablename__ = "shadow_comparisons"

    comparison_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    envelope_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("shadow_envelopes.envelope_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    target_result: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    target_result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    parity_class: Mapped[str] = mapped_column(String(24), nullable=False)
    differences: Mapped[list[Any]] = mapped_column(JSON, nullable=False)
    comparator_release: Mapped[str] = mapped_column(String(128), nullable=False)
    compared_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(target_result_sha256) = 64", name="target_hash_length"),
        CheckConstraint(
            "parity_class IN ('exact','semantic','mismatch','gap')", name="parity_class_allowed"
        ),
    )


class ShadowGap(Base):
    __tablename__ = "shadow_gaps"

    gap_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    shadow_baseline_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("shadow_baselines.shadow_baseline_id", ondelete="RESTRICT"), nullable=False
    )
    envelope_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("shadow_envelopes.envelope_id", ondelete="RESTRICT")
    )
    gap_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    gap_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True))
    gap_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "gap_kind IN ('missing_envelope','delivery_failure','uncomparable','mismatch')",
            name="gap_kind_allowed",
        ),
        CheckConstraint("state IN ('open','resolved','waived')", name="state_allowed"),
        CheckConstraint("gap_revision > 0", name="positive_revision"),
        CheckConstraint(
            "(state = 'open' AND resolved_at IS NULL AND resolution IS NULL) OR "
            "(state IN ('resolved','waived') AND resolved_at IS NOT NULL AND resolution IS NOT NULL)",
            name="resolution_consistent",
        ),
        UniqueConstraint("shadow_baseline_id", "gap_identity", name="uq_shadow_gap_identity"),
    )


class ProjectionEpoch(Base):
    __tablename__ = "projection_epochs"

    projection_epoch_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    epoch_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    activation_reason: Mapped[str] = mapped_column(Text, nullable=False)
    external_effects_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("epoch_number > 0", name="positive_epoch"),
        CheckConstraint("status IN ('active','retired')", name="status_allowed"),
        CheckConstraint(
            "(status = 'active' AND retired_at IS NULL) OR "
            "(status = 'retired' AND retired_at IS NOT NULL)",
            name="retirement_consistent",
        ),
        UniqueConstraint("generation_id", "epoch_number", name="uq_projection_epoch_number"),
        Index(
            "uq_projection_epoch_one_active",
            "generation_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )


class ProjectProjectionMapping(Base):
    __tablename__ = "project_projection_mappings"

    mapping_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    projection_epoch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_epochs.projection_epoch_id", ondelete="RESTRICT"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("governed_projects.project_id", ondelete="RESTRICT"), nullable=False
    )
    alias_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("project_external_aliases.alias_id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    mapping_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("state IN ('active','retired')", name="state_allowed"),
        CheckConstraint("mapping_revision > 0", name="positive_revision"),
        CheckConstraint(
            "(state = 'active' AND retired_at IS NULL) OR "
            "(state = 'retired' AND retired_at IS NOT NULL)",
            name="retirement_consistent",
        ),
        Index(
            "uq_project_projection_mapping_active_alias",
            "alias_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
        Index(
            "uq_project_projection_mapping_active",
            "generation_id",
            "project_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )


class SectionProjectionMapping(Base):
    __tablename__ = "section_projection_mappings"

    mapping_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    projection_epoch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_epochs.projection_epoch_id", ondelete="RESTRICT"), nullable=False
    )
    section_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("governed_sections.section_id", ondelete="RESTRICT"), nullable=False
    )
    alias_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("section_external_aliases.alias_id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    mapping_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("state IN ('active','retired')", name="state_allowed"),
        CheckConstraint("mapping_revision > 0", name="positive_revision"),
        CheckConstraint(
            "(state = 'active' AND retired_at IS NULL) OR "
            "(state = 'retired' AND retired_at IS NOT NULL)",
            name="retirement_consistent",
        ),
        Index(
            "uq_section_projection_mapping_active_alias",
            "alias_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
        Index(
            "uq_section_projection_mapping_active",
            "generation_id",
            "section_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )


class TaskProjectionMapping(Base):
    __tablename__ = "task_projection_mappings"

    mapping_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    projection_epoch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_epochs.projection_epoch_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    alias_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("task_external_aliases.alias_id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    mapping_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    bound_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("state IN ('active','retired')", name="state_allowed"),
        CheckConstraint("mapping_revision > 0", name="positive_revision"),
        CheckConstraint(
            "(state = 'active' AND retired_at IS NULL) OR "
            "(state = 'retired' AND retired_at IS NOT NULL)",
            name="retirement_consistent",
        ),
        Index(
            "uq_task_projection_mapping_active_alias",
            "alias_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
        Index(
            "uq_task_projection_mapping_active",
            "generation_id",
            "task_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )


class ProjectionOutboxEvent(Base):
    __tablename__ = "projection_outbox_events"

    projection_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    projection_epoch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_epochs.projection_epoch_id", ondelete="RESTRICT"), nullable=False
    )
    source_route: Mapped[str] = mapped_column(String(16), nullable=False)
    command_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT")
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    aggregate_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    intent_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    intent_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    claim_owner: Mapped[str | None] = mapped_column(String(256))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outbox_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('create_task','update_task_document','move_task','set_completion','reproject')",
            name="event_type_allowed",
        ),
        CheckConstraint("source_route IN ('command','service')", name="source_route_allowed"),
        CheckConstraint(
            "(source_route = 'command' AND command_execution_id IS NOT NULL) OR "
            "(source_route = 'service' AND command_execution_id IS NULL)",
            name="exact_source_route",
        ),
        CheckConstraint("aggregate_sequence > 0 AND outbox_revision > 0", name="positive_revisions"),
        CheckConstraint("length(idempotency_key) = 64 AND length(intent_sha256) = 64", name="hash_lengths"),
        CheckConstraint(
            "state IN ('pending','claimed','applied','uncertain','blocked','superseded')",
            name="state_allowed",
        ),
        CheckConstraint(
            "(state = 'claimed' AND claim_owner IS NOT NULL AND claim_token IS NOT NULL "
            "AND claim_expires_at IS NOT NULL AND terminal_at IS NULL) OR "
            "(state = 'pending' AND claim_owner IS NULL AND claim_token IS NULL "
            "AND claim_expires_at IS NULL AND terminal_at IS NULL) OR "
            "(state IN ('applied','uncertain','blocked','superseded') AND claim_owner IS NULL "
            "AND claim_token IS NULL AND claim_expires_at IS NULL AND terminal_at IS NOT NULL)",
            name="claim_state_consistent",
        ),
        UniqueConstraint(
            "generation_id", "task_id", "aggregate_sequence", name="uq_projection_task_sequence"
        ),
    )


class ProjectionAttempt(Base):
    __tablename__ = "projection_attempts"

    attempt_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    projection_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_outbox_events.projection_event_id", ondelete="RESTRICT"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(256), nullable=False)
    request_identity: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    intended_external_id: Mapped[str | None] = mapped_column(String(256))
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="dispatched")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("attempt_number > 0", name="positive_attempt"),
        CheckConstraint("length(request_sha256) = 64", name="request_hash_length"),
        CheckConstraint(
            "state IN ('dispatched','confirmed','not_applied','uncertain','blocked')",
            name="state_allowed",
        ),
        CheckConstraint(
            "(state = 'dispatched' AND terminal_at IS NULL) OR "
            "(state <> 'dispatched' AND terminal_at IS NOT NULL)",
            name="terminal_time_consistent",
        ),
        UniqueConstraint(
            "projection_event_id", "attempt_number", name="uq_projection_attempt_number"
        ),
    )


class ProjectionObservation(Base):
    __tablename__ = "projection_observations"

    observation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_attempts.attempt_id", ondelete="RESTRICT"), nullable=False
    )
    observation_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    observation_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    observed_applied: Mapped[bool | None] = mapped_column(Boolean)
    observed_identity: Mapped[str | None] = mapped_column(String(256))
    reread_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("observation_sequence > 0", name="positive_observation_sequence"),
        CheckConstraint(
            "observation_kind IN ('preflight','reread','marker_search','drift_scan')",
            name="kind_allowed",
        ),
        CheckConstraint("length(evidence_sha256) = 64", name="evidence_hash_length"),
        UniqueConstraint(
            "attempt_id", "observation_sequence", name="uq_attempt_observation_sequence"
        ),
    )


class ProjectionAdjudication(Base):
    __tablename__ = "projection_adjudications"

    adjudication_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    attempt_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_attempts.attempt_id", ondelete="RESTRICT"), nullable=False
    )
    observation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_observations.observation_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    adjudication_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(24), nullable=False)
    decision_reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("adjudication_sequence > 0", name="positive_adjudication_sequence"),
        CheckConstraint(
            "outcome IN ('confirmed','not_applied','uncertain','blocked')", name="outcome_allowed"
        ),
        CheckConstraint("decided_by IN ('automatic','marco')", name="decider_allowed"),
        UniqueConstraint(
            "attempt_id", "adjudication_sequence", name="uq_attempt_adjudication_sequence"
        ),
    )


class ProjectionCreateCorrelation(Base):
    __tablename__ = "projection_create_correlations"

    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    projection_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_outbox_events.projection_event_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    marker: Mapped[str] = mapped_column(String(256), nullable=False, unique=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    matched_external_id: Mapped[str | None] = mapped_column(String(256))
    match_count: Mapped[int | None] = mapped_column(Integer)
    mapping_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    correlation_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    last_evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("state IN ('pending','bound','ambiguous','not_found')", name="state_allowed"),
        CheckConstraint("correlation_revision > 0", name="positive_revision"),
        CheckConstraint(
            "(state = 'pending' AND matched_external_id IS NULL AND mapping_id IS NULL) OR "
            "(state = 'bound' AND matched_external_id IS NOT NULL AND match_count = 1 AND mapping_id IS NOT NULL) OR "
            "(state = 'ambiguous' AND match_count > 1 AND mapping_id IS NULL) OR "
            "(state = 'not_found' AND match_count = 0 AND mapping_id IS NULL)",
            name="state_payload_consistent",
        ),
    )


class ProjectionDriftEvent(Base):
    __tablename__ = "projection_drift_events"

    drift_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    task_mapping_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("task_projection_mappings.mapping_id", ondelete="RESTRICT"), nullable=False
    )
    drift_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    external_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    authoritative_snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    reproject_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projection_outbox_events.projection_event_id", ondelete="RESTRICT")
    )
    drift_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "drift_kind IN ('document','placement','completion','unknown_task')", name="kind_allowed"
        ),
        CheckConstraint("state IN ('open','reprojected','isolated')", name="state_allowed"),
        CheckConstraint("drift_revision > 0", name="positive_revision"),
        CheckConstraint(
            "length(external_snapshot_sha256) = 64 AND length(authoritative_snapshot_sha256) = 64",
            name="hash_lengths",
        ),
        CheckConstraint(
            "(state = 'open' AND reproject_event_id IS NULL AND resolved_at IS NULL) OR "
            "(state = 'reprojected' AND reproject_event_id IS NOT NULL AND resolved_at IS NOT NULL) OR "
            "(state = 'isolated' AND resolved_at IS NOT NULL)",
            name="resolution_consistent",
        ),
    )


class ProjectionReconciliationRun(Base):
    __tablename__ = "projection_reconciliation_runs"

    reconciliation_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    projection_epoch_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_epochs.projection_epoch_id", ondelete="RESTRICT"), nullable=False
    )
    corpus_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    expected_items: Mapped[int] = mapped_column(BigInteger, nullable=False)
    processed_items: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("status IN ('running','complete','blocked')", name="status_allowed"),
        CheckConstraint("expected_items >= 0 AND processed_items >= 0", name="nonnegative_counts"),
        CheckConstraint("processed_items <= expected_items", name="count_not_over_expected"),
        CheckConstraint(
            "(status = 'running' AND completed_at IS NULL) OR "
            "(status IN ('complete','blocked') AND completed_at IS NOT NULL)",
            name="terminal_time_consistent",
        ),
        UniqueConstraint(
            "generation_id", "projection_epoch_id", "corpus_identity", name="uq_reconciliation_corpus"
        ),
    )


class ProjectionReconciliationItem(Base):
    __tablename__ = "projection_reconciliation_items"

    reconciliation_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    reconciliation_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("projection_reconciliation_runs.reconciliation_run_id", ondelete="RESTRICT"), nullable=False
    )
    item_identity: Mapped[str] = mapped_column(String(256), nullable=False)
    entity_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    mapping_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("entity_kind IN ('project','section','task')", name="entity_kind_allowed"),
        CheckConstraint(
            "outcome IN ('matched','reprojected','unknown_external','blocked')",
            name="outcome_allowed",
        ),
        UniqueConstraint(
            "reconciliation_run_id", "item_identity", name="uq_reconciliation_item_identity"
        ),
    )


STAGE5_TABLE_NAMES = (
    "source_import_batches",
    "source_import_entity_evidence",
    "shadow_baselines",
    "shadow_envelopes",
    "shadow_deliveries",
    "shadow_comparisons",
    "shadow_gaps",
    "projection_epochs",
    "project_projection_mappings",
    "section_projection_mappings",
    "task_projection_mappings",
    "projection_outbox_events",
    "projection_attempts",
    "projection_observations",
    "projection_adjudications",
    "projection_create_correlations",
    "projection_drift_events",
    "projection_reconciliation_runs",
    "projection_reconciliation_items",
)

STAGE5_IMMUTABLE_TABLE_NAMES = (
    "source_import_entity_evidence",
    "shadow_envelopes",
    "shadow_comparisons",
    "projection_observations",
    "projection_adjudications",
    "projection_reconciliation_items",
)


def _install_sqlite_immutability_triggers() -> None:
    for table_name in STAGE5_IMMUTABLE_TABLE_NAMES:
        table = Base.metadata.tables[table_name]
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {table_name}_immutable_update BEFORE UPDATE ON {table_name} "
                "BEGIN SELECT RAISE(ABORT, 'immutable Stage 5 authority row'); END"
            ).execute_if(dialect="sqlite"),
        )
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {table_name}_immutable_delete BEFORE DELETE ON {table_name} "
                "BEGIN SELECT RAISE(ABORT, 'immutable Stage 5 authority row'); END"
            ).execute_if(dialect="sqlite"),
        )


_install_sqlite_immutability_triggers()


def _install_sqlite_projection_guards() -> None:
    mapping_specs = (
        (
            "project_projection_mappings",
            "project_id",
            "project_external_aliases",
        ),
        (
            "section_projection_mappings",
            "section_id",
            "section_external_aliases",
        ),
        (
            "task_projection_mappings",
            "task_id",
            "task_external_aliases",
        ),
    )
    for table_name, entity_column, alias_table in mapping_specs:
        table = Base.metadata.tables[table_name]
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {table_name}_identity_insert "
                f"BEFORE INSERT ON {table_name} "
                "WHEN NOT EXISTS ("
                f"SELECT 1 FROM {alias_table} a "
                "JOIN projection_epochs e "
                "ON e.projection_epoch_id = NEW.projection_epoch_id "
                f"WHERE a.alias_id = NEW.alias_id AND a.{entity_column} = NEW.{entity_column} "
                "AND a.state = 'active' AND e.generation_id = NEW.generation_id "
                "AND e.status = 'active') "
                "BEGIN SELECT RAISE(ABORT, 'projection mapping identity mismatch'); END"
            ).execute_if(dialect="sqlite"),
        )
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {table_name}_identity_update "
                f"BEFORE UPDATE OF generation_id, projection_epoch_id, {entity_column}, alias_id "
                f"ON {table_name} "
                "BEGIN SELECT RAISE(ABORT, 'projection mapping identity is immutable'); END"
            ).execute_if(dialect="sqlite"),
        )

    outbox = Base.metadata.tables["projection_outbox_events"]
    event.listen(
        outbox,
        "after_create",
        DDL(
            "CREATE TRIGGER projection_outbox_events_authority_insert "
            "BEFORE INSERT ON projection_outbox_events "
            "WHEN NOT EXISTS ("
            "SELECT 1 FROM projection_epochs e "
            "JOIN authority_generations g ON g.generation_id = e.generation_id "
            "JOIN task_authority_heads h ON h.generation_id = e.generation_id "
            "AND h.task_id = NEW.task_id "
            "WHERE e.projection_epoch_id = NEW.projection_epoch_id "
            "AND e.generation_id = NEW.generation_id "
            "AND e.status = 'active' AND g.status = 'active') "
            "OR (NEW.source_route = 'command' AND NOT EXISTS ("
            "SELECT 1 FROM command_executions x "
            "WHERE x.execution_id = NEW.command_execution_id "
            "AND x.generation_id = NEW.generation_id "
            "AND x.task_id = NEW.task_id "
            "AND x.status IN ('claimed','committed'))) "
            "BEGIN SELECT RAISE(ABORT, 'projection outbox authority mismatch'); END"
        ).execute_if(dialect="sqlite"),
    )
    event.listen(
        outbox,
        "after_create",
        DDL(
            "CREATE TRIGGER projection_outbox_events_identity_update "
            "BEFORE UPDATE OF generation_id, projection_epoch_id, source_route, "
            "command_execution_id, task_id, event_type, aggregate_sequence, "
            "idempotency_key, intent_payload, intent_sha256, created_at "
            "ON projection_outbox_events "
            "BEGIN SELECT RAISE(ABORT, 'projection outbox identity is immutable'); END"
        ).execute_if(dialect="sqlite"),
    )


_install_sqlite_projection_guards()
