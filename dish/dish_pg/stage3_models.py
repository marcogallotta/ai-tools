"""Stage 3 PostgreSQL workflow, replay, recovery, and concurrency authority.

These mappings extend the Stage 2 core model without activating the production
service. Immutable occurrences establish evidence; mutable current rows are
strictly revisioned and remain subordinate to those occurrences.
"""
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
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.schema import DDL

from .models import Base


class ServiceRun(Base):
    __tablename__ = "service_runs"

    run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    owner_id: Mapped[str] = mapped_column(String(256), nullable=False)
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    capability_digest: Mapped[bytes] = mapped_column(LargeBinary(32), nullable=False)
    bootstrap_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("generation_bootstrap_authorities.bootstrap_id", ondelete="RESTRICT"),
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("agent IN ('claude','gpt','codex','marco','service')", name="agent_allowed"),
        CheckConstraint("status IN ('active','retired','revoked')", name="status_allowed"),
        CheckConstraint(
            "(status = 'active' AND retired_at IS NULL) OR "
            "(status IN ('retired','revoked') AND retired_at IS NOT NULL)",
            name="retirement_matches_status",
        ),
        UniqueConstraint("generation_id", "owner_id", "run_id", name="uq_run_generation_owner"),
        Index(
            "uq_service_runs_active_capability",
            "generation_id",
            "capability_digest",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )


class ServiceRequest(Base):
    __tablename__ = "service_requests"

    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_runs.run_id", ondelete="RESTRICT"), nullable=False
    )
    owner_id: Mapped[str] = mapped_column(String(256), nullable=False)
    principal_class: Mapped[str] = mapped_column(String(32), nullable=False)
    command_name: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    canonical_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    protocol_release: Mapped[str] = mapped_column(String(128), nullable=False)
    dish_release: Mapped[str] = mapped_column(String(128), nullable=False)
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "principal_class IN ('agent','admin','verification','service')",
            name="principal_class_allowed",
        ),
        CheckConstraint("length(canonical_payload_sha256) = 64", name="payload_hash_length"),
        ForeignKeyConstraint(
            ["generation_id", "owner_id", "run_id"],
            [
                "service_runs.generation_id",
                "service_runs.owner_id",
                "service_runs.run_id",
            ],
            ondelete="RESTRICT",
            name="fk_service_requests_exact_run_owner",
        ),
        UniqueConstraint("generation_id", "request_id", name="uq_request_generation_identity"),
    )


class ServiceRequestOutcome(Base):
    __tablename__ = "service_request_outcomes"

    outcome_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    outcome_class: Mapped[str] = mapped_column(String(32), nullable=False)
    result_code: Mapped[str] = mapped_column(String(96), nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    result_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    immutable_success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "outcome_class IN ('success','rule_error','conflict','uncertain','retired')",
            name="outcome_class_allowed",
        ),
        CheckConstraint("http_status BETWEEN 100 AND 599", name="http_status_range"),
        CheckConstraint("length(result_sha256) = 64", name="result_hash_length"),
        CheckConstraint(
            "(outcome_class = 'success' AND immutable_success) OR "
            "(outcome_class <> 'success' AND NOT immutable_success)",
            name="success_flag_matches_class",
        ),
    )


class RequestUncertaintyResolution(Base):
    __tablename__ = "request_uncertainty_resolutions"

    resolution_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), nullable=False
    )
    prior_outcome_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_request_outcomes.outcome_id", ondelete="RESTRICT"), nullable=False
    )
    resolution_class: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    resolved_by: Mapped[str] = mapped_column(String(256), nullable=False)
    resolved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "resolution_class IN ('confirmed','not_applied','quarantined')",
            name="resolution_class_allowed",
        ),
        UniqueConstraint("request_id", name="uq_request_uncertainty_resolution"),
    )


class CommandExecution(Base):
    __tablename__ = "command_executions"

    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT")
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    command_name: Mapped[str] = mapped_column(String(64), nullable=False)
    transaction_profile: Mapped[str] = mapped_column(String(1), nullable=False)
    canonical_intent: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    pinned_inputs: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    contract_binding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("honest_contract_bindings.binding_id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    claim_owner: Mapped[str | None] = mapped_column(String(256))
    claim_token: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("transaction_profile IN ('E','L','R','P')", name="profile_allowed"),
        CheckConstraint(
            "status IN ('pending','claimed','committed','failed','uncertain','cancelled')",
            name="status_allowed",
        ),
        CheckConstraint("execution_revision > 0", name="positive_revision"),
        CheckConstraint(
            "(status = 'claimed' AND claim_owner IS NOT NULL AND claim_token IS NOT NULL "
            "AND claim_expires_at IS NOT NULL AND terminal_at IS NULL) OR "
            "(status = 'pending' AND claim_owner IS NULL AND claim_token IS NULL "
            "AND claim_expires_at IS NULL AND terminal_at IS NULL) OR "
            "(status IN ('committed','failed','uncertain','cancelled') AND terminal_at IS NOT NULL)",
            name="claim_and_terminal_state_consistent",
        ),
        UniqueConstraint("execution_id", "generation_id", name="uq_execution_generation"),
        Index("ix_command_executions_task_status", "generation_id", "task_id", "status"),
    )


class ExecutionClaimEvent(Base):
    __tablename__ = "execution_claim_events"

    claim_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    claim_token: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    claimant: Mapped[str] = mapped_column(String(256), nullable=False)
    expected_execution_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("event_kind IN ('claimed','renewed','released','taken_over')", name="event_allowed"),
        CheckConstraint("expected_execution_revision > 0", name="positive_revision"),
        UniqueConstraint("execution_id", "claim_token", "event_kind", name="uq_execution_claim_event"),
    )


class TaskExecutionFence(Base):
    __tablename__ = "task_execution_fences"

    execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), primary_key=True
    )
    generation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    expected_task_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_membership_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_placement_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_completion_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        ForeignKeyConstraint(
            ["generation_id", "task_id"],
            ["task_authority_heads.generation_id", "task_authority_heads.task_id"],
            ondelete="RESTRICT",
            name="fk_task_execution_fence_head",
        ),
        CheckConstraint(
            "expected_task_revision > 0 AND expected_membership_revision > 0 "
            "AND expected_placement_revision > 0 AND expected_completion_revision > 0",
            name="positive_revisions",
        ),
    )


class WorkflowOperation(Base):
    __tablename__ = "workflow_operations"

    operation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    phase: Mapped[str] = mapped_column(String(48), nullable=False)
    persisted_actions: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT")
    )
    creation_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT")
    )
    creation_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), unique=True
    )
    contract_binding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("honest_contract_bindings.binding_id", ondelete="RESTRICT"), nullable=False
    )
    predecessor_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT")
    )
    terminal_outcome: Mapped[str | None] = mapped_column(String(64))
    operation_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("kind IN ('planning','initial','change','verification','migration')", name="kind_allowed"),
        CheckConstraint(
            "lifecycle IN ('open','completed','cancelled_by_marco','abandoned','failed')",
            name="lifecycle_allowed",
        ),
        CheckConstraint("operation_revision > 0", name="positive_revision"),
        CheckConstraint(
            "(import_run_id IS NULL AND creation_request_id IS NOT NULL "
            "AND creation_execution_id IS NOT NULL) OR "
            "(import_run_id IS NOT NULL AND creation_request_id IS NULL "
            "AND creation_execution_id IS NULL)",
            name="creation_provenance_exact",
        ),
        CheckConstraint(
            "import_run_id IS NULL OR lifecycle <> 'open'",
            name="imported_history_terminal",
        ),
        CheckConstraint(
            "(lifecycle = 'open' AND terminal_outcome IS NULL AND terminal_at IS NULL) OR "
            "(lifecycle <> 'open' AND terminal_outcome IS NOT NULL AND terminal_at IS NOT NULL)",
            name="terminal_state_consistent",
        ),
        Index(
            "uq_workflow_operations_one_open_per_task",
            "generation_id",
            "task_id",
            unique=True,
            postgresql_where=text("lifecycle = 'open'"),
            sqlite_where=text("lifecycle = 'open'"),
        ),
    )


class OperationExecutionFence(Base):
    __tablename__ = "operation_execution_fences"

    execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), primary_key=True
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT"), nullable=False
    )
    expected_operation_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expected_phase: Mapped[str] = mapped_column(String(48), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("expected_operation_revision > 0", name="positive_revision"),
        UniqueConstraint("operation_id", "execution_id", name="uq_operation_execution_fence"),
    )


class OperationStep(Base):
    __tablename__ = "operation_steps"

    step_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT"), nullable=False
    )
    step_name: Mapped[str] = mapped_column(String(64), nullable=False)
    step_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(24), nullable=False)
    command_execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("step_sequence > 0", name="positive_sequence"),
        CheckConstraint("outcome IN ('complete','failed','skipped','held')", name="outcome_allowed"),
        UniqueConstraint("operation_id", "step_name", name="uq_operation_step_name"),
        UniqueConstraint("operation_id", "step_sequence", name="uq_operation_step_sequence"),
    )


class OperationActorFact(Base):
    __tablename__ = "operation_actor_facts"

    actor_fact_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    actor_role: Mapped[str] = mapped_column(String(32), nullable=False)
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(256), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_runs.run_id", ondelete="RESTRICT"), nullable=False
    )
    actor_attempt_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    command_execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("actor_attempt_sequence > 0", name="positive_attempt_sequence"),
        UniqueConstraint("task_id", "actor_attempt_sequence", name="uq_task_actor_attempt_sequence"),
        UniqueConstraint("operation_id", "run_id", "actor_role", name="uq_operation_run_actor_role"),
    )


class ServiceLease(Base):
    __tablename__ = "service_leases"

    lease_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("service_runs.run_id", ondelete="RESTRICT")
    )
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT")
    )
    source_run_id: Mapped[str | None] = mapped_column(String(256))
    owner_id: Mapped[str] = mapped_column(String(256), nullable=False)
    lease_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    actor_role: Mapped[str | None] = mapped_column(String(32))
    actor_attempt_sequence: Mapped[int | None] = mapped_column(BigInteger)
    verification_cycle_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("lease_kind IN ('actor','admin_request')", name="kind_allowed"),
        CheckConstraint("state IN ('active','released','expired','recovered')", name="state_allowed"),
        CheckConstraint("lease_revision > 0", name="positive_revision"),
        CheckConstraint("expires_at > issued_at", name="positive_duration"),
        CheckConstraint(
            "(import_run_id IS NULL AND run_id IS NOT NULL AND source_run_id IS NULL) OR "
            "(import_run_id IS NOT NULL AND run_id IS NULL AND length(trim(source_run_id)) > 0)",
            name="provenance_exact",
        ),
        CheckConstraint(
            "import_run_id IS NULL OR state <> 'active'",
            name="imported_history_terminal",
        ),
        CheckConstraint(
            "(lease_kind = 'actor' AND operation_id IS NOT NULL "
            "AND actor_attempt_sequence IS NOT NULL "
            "AND (import_run_id IS NOT NULL OR actor_role IS NOT NULL)) OR "
            "(lease_kind = 'admin_request' AND actor_role IS NULL "
            "AND actor_attempt_sequence IS NULL AND verification_cycle_id IS NULL)",
            name="classification_context_complete",
        ),
        CheckConstraint(
            "(state = 'active' AND terminal_at IS NULL) OR "
            "(state <> 'active' AND terminal_at IS NOT NULL)",
            name="terminal_state_consistent",
        ),
        Index(
            "uq_service_leases_one_active_task_actor",
            "generation_id",
            "task_id",
            unique=True,
            postgresql_where=text("state = 'active' AND lease_kind = 'actor'"),
            sqlite_where=text("state = 'active' AND lease_kind = 'actor'"),
        ),
        UniqueConstraint("task_id", "actor_attempt_sequence", name="uq_lease_task_attempt_sequence"),
    )


class LeaseEvent(Base):
    __tablename__ = "lease_events"

    lease_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    lease_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_leases.lease_id", ondelete="RESTRICT"), nullable=False
    )
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), nullable=False
    )
    command_execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    prior_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resulting_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    prior_expiry: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resulting_expiry: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("event_kind IN ('issued','renewed','released','expired','recovered')", name="event_allowed"),
        CheckConstraint("prior_revision >= 0 AND resulting_revision = prior_revision + 1", name="revision_step"),
        UniqueConstraint("lease_id", "resulting_revision", name="uq_lease_event_revision"),
    )


class PlanningIntentChallenge(Base):
    __tablename__ = "planning_intent_challenges"

    challenge_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    issuing_request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_runs.run_id", ondelete="RESTRICT"), nullable=False
    )
    owner_id: Mapped[str] = mapped_column(String(256), nullable=False)
    agent: Mapped[str] = mapped_column(String(32), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="issued")
    claiming_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), unique=True
    )
    intent_basis: Mapped[str | None] = mapped_column(String(24))
    override_reason: Mapped[str | None] = mapped_column(Text)
    resulting_operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    settled_by: Mapped[str | None] = mapped_column(String(256))
    settlement_reason: Mapped[str | None] = mapped_column(Text)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("target_kind = 'planning'", name="planning_only"),
        CheckConstraint("state IN ('issued','claimed','consumed','settled')", name="state_allowed"),
        CheckConstraint(
            "(state = 'issued' AND claiming_request_id IS NULL AND intent_basis IS NULL "
            "AND resulting_operation_id IS NULL AND terminal_at IS NULL) OR "
            "(state = 'claimed' AND claiming_request_id IS NOT NULL AND intent_basis IS NOT NULL "
            "AND resulting_operation_id IS NULL AND terminal_at IS NULL) OR "
            "(state = 'consumed' AND claiming_request_id IS NOT NULL AND intent_basis IS NOT NULL "
            "AND resulting_operation_id IS NOT NULL AND terminal_at IS NOT NULL) OR "
            "(state = 'settled' AND settled_by IS NOT NULL AND settlement_reason IS NOT NULL "
            "AND resulting_operation_id IS NULL AND terminal_at IS NOT NULL)",
            name="state_payload_consistent",
        ),
        CheckConstraint(
            "intent_basis IS NULL OR intent_basis IN ('user_requested','agent_override')",
            name="intent_basis_allowed",
        ),
        CheckConstraint(
            "intent_basis <> 'agent_override' OR length(trim(override_reason)) > 0",
            name="override_reason_required",
        ),
    )


class MarcoAuthorizationGrant(Base):
    __tablename__ = "marco_authorization_grants"

    grant_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT")
    )
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    before_value: Mapped[Any] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    after_value: Mapped[Any] = mapped_column(JSON().with_variant(JSONB, "postgresql"), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_runs.run_id", ondelete="RESTRICT"), nullable=False
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), nullable=False
    )
    command_execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(trim(reason)) > 0", name="reason_nonblank"),
        UniqueConstraint(
            "generation_id", "task_id", "operation_id", "field_name", "before_value", "after_value",
            name="uq_marco_grant_semantic_identity",
        ),
        Index(
            "uq_marco_grant_task_semantic_identity",
            "generation_id",
            "task_id",
            "field_name",
            "before_value",
            "after_value",
            unique=True,
            postgresql_where=text("operation_id IS NULL"),
            sqlite_where=text("operation_id IS NULL"),
        ),
    )


class MarcoAuthorizationState(Base):
    __tablename__ = "marco_authorization_states"

    grant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("marco_authorization_grants.grant_id", ondelete="RESTRICT"), primary_key=True
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="available")
    reservation_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True)
    reservation_request_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT")
    )
    consumed_result_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, unique=True)
    authorization_revision: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("state IN ('available','reserved','consumed')", name="state_allowed"),
        CheckConstraint("authorization_revision > 0", name="positive_revision"),
        CheckConstraint(
            "(state = 'available' AND reservation_token IS NULL AND reservation_request_id IS NULL "
            "AND consumed_result_id IS NULL) OR "
            "(state = 'reserved' AND reservation_token IS NOT NULL AND reservation_request_id IS NOT NULL "
            "AND consumed_result_id IS NULL) OR "
            "(state = 'consumed' AND reservation_token IS NOT NULL "
            "AND reservation_request_id IS NOT NULL AND consumed_result_id IS NOT NULL)",
            name="state_payload_consistent",
        ),
    )


class MarcoAuthorizationEvent(Base):
    __tablename__ = "marco_authorization_events"

    authorization_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    grant_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("marco_authorization_grants.grant_id", ondelete="RESTRICT"), nullable=False
    )
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    reservation_token: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), nullable=False
    )
    command_execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    bound_result_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("event_kind IN ('reserved','released','consumed')", name="event_allowed"),
        CheckConstraint(
            "(event_kind = 'consumed' AND bound_result_id IS NOT NULL) OR "
            "(event_kind <> 'consumed' AND bound_result_id IS NULL)",
            name="consumption_binds_result",
        ),
        UniqueConstraint(
            "grant_id", "reservation_token", "event_kind",
            name="uq_marco_authorization_event_identity",
        ),
    )


class VerificationCycle(Base):
    __tablename__ = "verification_cycles"

    cycle_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT"), nullable=False
    )
    reviewed_content_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("task_content_versions.content_version_id", ondelete="RESTRICT")
    )
    contract_binding_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("honest_contract_bindings.binding_id", ondelete="RESTRICT"), nullable=False
    )
    cycle_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(24), nullable=False, default="open")
    outcome: Mapped[str | None] = mapped_column(String(32))
    import_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("stage_a_import_runs.import_run_id", ondelete="RESTRICT")
    )
    created_by_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("cycle_sequence > 0", name="positive_cycle_sequence"),
        CheckConstraint("lifecycle IN ('open','approved','rejected','reset','abandoned')", name="lifecycle_allowed"),
        CheckConstraint(
            "(import_run_id IS NULL AND reviewed_content_version_id IS NOT NULL "
            "AND created_by_execution_id IS NOT NULL) OR "
            "(import_run_id IS NOT NULL AND reviewed_content_version_id IS NULL "
            "AND created_by_execution_id IS NULL)",
            name="creation_provenance_exact",
        ),
        CheckConstraint(
            "import_run_id IS NULL OR lifecycle <> 'open'",
            name="imported_history_terminal",
        ),
        CheckConstraint(
            "(lifecycle = 'open' AND outcome IS NULL AND terminal_at IS NULL) OR "
            "(lifecycle <> 'open' AND outcome IS NOT NULL AND terminal_at IS NOT NULL)",
            name="terminal_state_consistent",
        ),
        UniqueConstraint(
            "operation_id",
            "cycle_sequence",
            name="uq_verification_cycle_sequence",
        ),
        Index(
            "uq_verification_cycles_one_open_per_operation",
            "operation_id",
            unique=True,
            postgresql_where=text("lifecycle = 'open'"),
            sqlite_where=text("lifecycle = 'open'"),
        ),
    )


class VerificationInspectionOccurrence(Base):
    __tablename__ = "verification_inspection_occurrences"

    inspection_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    cycle_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("verification_cycles.cycle_id", ondelete="RESTRICT"), nullable=False
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    reviewed_content_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("task_content_versions.content_version_id", ondelete="RESTRICT"), nullable=False
    )
    verifier_actor_fact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("operation_actor_facts.actor_fact_id", ondelete="RESTRICT"), nullable=False
    )
    verifier_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_runs.run_id", ondelete="RESTRICT"), nullable=False
    )
    attestation: Mapped[str] = mapped_column(Text, nullable=False)
    section_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("governed_sections.section_id", ondelete="RESTRICT"), nullable=False
    )
    registry_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("section_registry_versions.registry_version_id", ondelete="RESTRICT"), nullable=False
    )
    placement_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("task_section_placement_events.placement_event_id", ondelete="RESTRICT"), nullable=False
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    command_execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    inspected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(trim(attestation)) > 0", name="attestation_nonblank"),
        UniqueConstraint(
            "cycle_id", "reviewed_content_version_id", "verifier_actor_fact_id", "placement_event_id",
            name="uq_verification_inspection_identity",
        ),
    )


class VerificationCorrection(Base):
    __tablename__ = "verification_corrections"

    correction_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    cycle_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("verification_cycles.cycle_id", ondelete="RESTRICT"), nullable=False
    )
    source_content_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("task_content_versions.content_version_id", ondelete="RESTRICT"), nullable=False
    )
    corrected_content_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("task_content_versions.content_version_id", ondelete="RESTRICT"), nullable=False
    )
    correction_class: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    command_execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("correction_class IN ('small','large','evidence','human_review')", name="class_allowed"),
        CheckConstraint("source_content_version_id <> corrected_content_version_id", name="version_changes"),
        UniqueConstraint("cycle_id", "corrected_content_version_id", name="uq_cycle_corrected_candidate"),
    )


class VerificationSignoff(Base):
    __tablename__ = "verification_signoffs"

    signoff_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    cycle_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("verification_cycles.cycle_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    signed_content_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("task_content_versions.content_version_id", ondelete="RESTRICT"), nullable=False
    )
    inspection_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("verification_inspection_occurrences.inspection_id", ondelete="RESTRICT"), nullable=False
    )
    verifier_actor_fact_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("operation_actor_facts.actor_fact_id", ondelete="RESTRICT"), nullable=False
    )
    inherited_from_signoff_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("verification_signoffs.signoff_id", ondelete="RESTRICT")
    )
    signoff_kind: Mapped[str] = mapped_column(String(24), nullable=False)
    command_execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("signoff_kind IN ('direct','inherited_non_material')", name="kind_allowed"),
        CheckConstraint(
            "(signoff_kind = 'direct' AND inherited_from_signoff_id IS NULL) OR "
            "(signoff_kind = 'inherited_non_material' AND inherited_from_signoff_id IS NOT NULL)",
            name="inheritance_matches_kind",
        ),
        UniqueConstraint("cycle_id", name="uq_verification_cycle_signoff"),
        UniqueConstraint("task_id", "signed_content_version_id", name="uq_task_signed_occurrence"),
    )


class EvidenceHold(Base):
    __tablename__ = "evidence_holds"

    hold_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT"), nullable=False
    )
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("verification_cycles.cycle_id", ondelete="RESTRICT")
    )
    baseline_content_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("task_content_versions.content_version_id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    opened_by_execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("state IN ('open','supplied','cancelled')", name="state_allowed"),
        CheckConstraint(
            "(state = 'open' AND terminal_at IS NULL) OR (state <> 'open' AND terminal_at IS NOT NULL)",
            name="terminal_state_consistent",
        ),
        Index(
            "uq_evidence_holds_one_open_per_operation",
            "operation_id",
            unique=True,
            postgresql_where=text("state = 'open'"),
            sqlite_where=text("state = 'open'"),
        ),
    )


class EvidenceHoldEvent(Base):
    __tablename__ = "evidence_hold_events"

    hold_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    hold_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("evidence_holds.hold_id", ondelete="RESTRICT"), nullable=False
    )
    event_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    evidence_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), nullable=False
    )
    command_execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("event_kind IN ('opened','supplied','cancelled')", name="event_allowed"),
        UniqueConstraint("hold_id", "event_kind", name="uq_hold_event_kind"),
    )


class HumanReviewRequirement(Base):
    __tablename__ = "human_review_requirements"

    requirement_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT"), nullable=False
    )
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("verification_cycles.cycle_id", ondelete="RESTRICT")
    )
    route: Mapped[str] = mapped_column(String(32), nullable=False)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    baseline_content_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("task_content_versions.content_version_id", ondelete="RESTRICT"), nullable=False
    )
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="open")
    opened_by_execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("route IN ('human_review','two_pass_hold')", name="route_allowed"),
        CheckConstraint("state IN ('open','decided','reset','cancelled')", name="state_allowed"),
        CheckConstraint(
            "(state = 'open' AND terminal_at IS NULL) OR (state <> 'open' AND terminal_at IS NOT NULL)",
            name="terminal_state_consistent",
        ),
        Index(
            "uq_human_review_one_open_per_operation",
            "operation_id",
            unique=True,
            postgresql_where=text("state = 'open'"),
            sqlite_where=text("state = 'open'"),
        ),
    )


class HumanReviewDecision(Base):
    __tablename__ = "human_review_decisions"

    decision_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    requirement_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("human_review_requirements.requirement_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), nullable=False
    )
    command_execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("length(trim(decision)) > 0", name="decision_nonblank"),
        CheckConstraint("length(trim(rationale)) > 0", name="rationale_nonblank"),
    )


class AbandonmentAttempt(Base):
    __tablename__ = "abandonment_attempts"

    abandonment_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    source_operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT"), nullable=False
    )
    source_lease_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_leases.lease_id", ondelete="RESTRICT"), nullable=False
    )
    source_actor_attempt_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("verification_cycles.cycle_id", ondelete="RESTRICT")
    )
    source_owner_id: Mapped[str] = mapped_column(String(256), nullable=False)
    source_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_runs.run_id", ondelete="RESTRICT"), nullable=False
    )
    baseline_content_activation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("task_content_activations.content_activation_id", ondelete="RESTRICT"), nullable=False
    )
    baseline_placement_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("task_section_placement_events.placement_event_id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False, default="preparing")
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), nullable=False
    )
    command_execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    successor_operation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("source_actor_attempt_sequence > 0", name="positive_attempt_sequence"),
        CheckConstraint(
            "state IN ('preparing','published','blocked','reconciling','completed','cancelled')",
            name="state_allowed",
        ),
        CheckConstraint(
            "(state IN ('preparing','blocked','reconciling') "
            "AND successor_operation_id IS NULL AND terminal_at IS NULL) OR "
            "(state = 'published' AND successor_operation_id IS NOT NULL AND terminal_at IS NULL) OR "
            "(state = 'completed' AND successor_operation_id IS NOT NULL AND terminal_at IS NOT NULL) OR "
            "(state = 'cancelled' AND terminal_at IS NOT NULL)",
            name="state_payload_consistent",
        ),
        Index(
            "uq_abandonment_one_active_per_task",
            "generation_id",
            "task_id",
            unique=True,
            postgresql_where=text("state IN ('preparing','published','blocked','reconciling')"),
            sqlite_where=text("state IN ('preparing','published','blocked','reconciling')"),
        ),
    )


class OperationSuccessionEdge(Base):
    __tablename__ = "operation_succession_edges"

    succession_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    abandonment_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("abandonment_attempts.abandonment_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    task_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT"), nullable=False
    )
    source_operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    successor_operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    claim_mode: Mapped[str] = mapped_column(String(24), nullable=False)
    prepared_cycle_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("verification_cycles.cycle_id", ondelete="RESTRICT")
    )
    published_by_execution_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT"), nullable=False
    )
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("source_operation_id <> successor_operation_id", name="operations_distinct"),
        CheckConstraint("claim_mode IN ('operation','operation_cycle')", name="claim_mode_allowed"),
        CheckConstraint(
            "(claim_mode = 'operation' AND prepared_cycle_id IS NULL) OR "
            "(claim_mode = 'operation_cycle' AND prepared_cycle_id IS NOT NULL)",
            name="cycle_matches_claim_mode",
        ),
    )


class GovernedAuditEvent(Base):
    __tablename__ = "governed_audit_events"

    audit_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), nullable=False
    )
    command_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT")
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("dish_tasks.task_id", ondelete="RESTRICT")
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("workflow_operations.operation_id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(String(96), nullable=False)
    actor: Mapped[str] = mapped_column(String(256), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("request_id", "event_type", name="uq_request_audit_event_type"),
        Index("ix_governed_audit_task_time", "generation_id", "task_id", "occurred_at"),
    )


class CausalityEdge(Base):
    __tablename__ = "causality_edges"

    causality_edge_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), nullable=False
    )
    command_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT")
    )
    cause_type: Mapped[str] = mapped_column(String(64), nullable=False)
    cause_id: Mapped[str] = mapped_column(String(128), nullable=False)
    effect_type: Mapped[str] = mapped_column(String(64), nullable=False)
    effect_id: Mapped[str] = mapped_column(String(128), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "generation_id", "cause_type", "cause_id", "effect_type", "effect_id",
            name="uq_causality_edge_identity",
        ),
    )


class InvocationAuditObligation(Base):
    __tablename__ = "invocation_audit_obligations"

    obligation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    generation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("authority_generations.generation_id", ondelete="RESTRICT"), nullable=False
    )
    request_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_requests.request_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    outcome_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("service_request_outcomes.outcome_id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    command_execution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("command_executions.execution_id", ondelete="RESTRICT")
    )
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    required_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    terminal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("length(payload_sha256) = 64", name="payload_hash_length"),
        CheckConstraint("state IN ('pending','fulfilled','repaired','quarantined')", name="state_allowed"),
        CheckConstraint(
            "(state = 'pending' AND terminal_at IS NULL) OR "
            "(state <> 'pending' AND terminal_at IS NOT NULL)",
            name="terminal_state_consistent",
        ),
        Index("ix_invocation_audit_pending", "generation_id", "state", "created_at"),
    )


class InvocationAuditRepair(Base):
    __tablename__ = "invocation_audit_repairs"

    repair_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    obligation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("invocation_audit_obligations.obligation_id", ondelete="RESTRICT"), nullable=False
    )
    repair_identity: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    quarantine_reason: Mapped[str | None] = mapped_column(Text)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("source IN ('postgresql','local_journal','import')", name="source_allowed"),
        CheckConstraint("outcome IN ('fulfilled','repaired','quarantined')", name="outcome_allowed"),
        CheckConstraint("length(payload_sha256) = 64", name="payload_hash_length"),
        CheckConstraint(
            "(outcome = 'quarantined' AND quarantine_reason IS NOT NULL) OR "
            "(outcome <> 'quarantined' AND quarantine_reason IS NULL)",
            name="quarantine_reason_matches_outcome",
        ),
        UniqueConstraint("obligation_id", "payload_sha256", name="uq_repair_obligation_payload"),
    )


STAGE3_IMMUTABLE_TABLE_NAMES = (
    "service_requests",
    "service_request_outcomes",
    "request_uncertainty_resolutions",
    "execution_claim_events",
    "task_execution_fences",
    "operation_execution_fences",
    "operation_steps",
    "operation_actor_facts",
    "lease_events",
    "marco_authorization_grants",
    "marco_authorization_events",
    "verification_inspection_occurrences",
    "verification_corrections",
    "verification_signoffs",
    "evidence_hold_events",
    "human_review_decisions",
    "operation_succession_edges",
    "governed_audit_events",
    "causality_edges",
    "invocation_audit_repairs",
)

STAGE3_TABLE_NAMES = (
    "service_runs",
    "service_requests",
    "service_request_outcomes",
    "request_uncertainty_resolutions",
    "command_executions",
    "execution_claim_events",
    "task_execution_fences",
    "workflow_operations",
    "operation_execution_fences",
    "operation_steps",
    "operation_actor_facts",
    "service_leases",
    "lease_events",
    "planning_intent_challenges",
    "marco_authorization_grants",
    "marco_authorization_states",
    "marco_authorization_events",
    "verification_cycles",
    "verification_inspection_occurrences",
    "verification_corrections",
    "verification_signoffs",
    "evidence_holds",
    "evidence_hold_events",
    "human_review_requirements",
    "human_review_decisions",
    "abandonment_attempts",
    "operation_succession_edges",
    "governed_audit_events",
    "causality_edges",
    "invocation_audit_obligations",
    "invocation_audit_repairs",
)


def _install_sqlite_stage3_immutability_triggers() -> None:
    for table_name in STAGE3_IMMUTABLE_TABLE_NAMES:
        table = Base.metadata.tables[table_name]
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {table_name}_immutable_update BEFORE UPDATE ON {table_name} "
                "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
            ).execute_if(dialect="sqlite"),
        )
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {table_name}_immutable_delete BEFORE DELETE ON {table_name} "
                "BEGIN SELECT RAISE(ABORT, 'immutable authority row'); END"
            ).execute_if(dialect="sqlite"),
        )



def _install_sqlite_import_provenance_triggers() -> None:
    protected_columns = {
        "workflow_operations": ("import_run_id", "creation_request_id", "creation_execution_id"),
        "service_leases": ("import_run_id", "run_id", "source_run_id"),
        "verification_cycles": ("import_run_id", "reviewed_content_version_id", "created_by_execution_id"),
    }
    for table_name, columns in protected_columns.items():
        table = Base.metadata.tables[table_name]
        event.listen(
            table,
            "after_create",
            DDL(
                f"CREATE TRIGGER {table_name}_import_provenance_immutable "
                f"BEFORE UPDATE OF {','.join(columns)} ON {table_name} "
                "BEGIN SELECT RAISE(ABORT, 'import provenance is immutable'); END"
            ).execute_if(dialect="sqlite"),
        )


_install_sqlite_stage3_immutability_triggers()
_install_sqlite_import_provenance_triggers()
