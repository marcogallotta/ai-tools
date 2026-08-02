"""Stage 3 transactional workflow authority services.

The service methods never commit. Callers own one SQLAlchemy transaction and can
compose request admission, execution, domain evidence, outcome, audit, causality,
and invocation-audit obligation atomically.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from sqlalchemy import and_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models
from . import stage3_models as wf


class WorkflowAuthorityError(ValueError):
    """The requested authority transition is illegal or stale."""


class RequestIdentityConflict(WorkflowAuthorityError):
    """A request UUID was reused for a different logical request."""


class StaleAuthorityError(WorkflowAuthorityError):
    """A run, fence, claim, or generation is no longer current."""


class ContentionLost(WorkflowAuthorityError):
    """Another compatible transaction won the exclusive authority race."""


class MutationAdmissionClosed(StaleAuthorityError):
    """Stage 6 has not opened PostgreSQL mutation admission."""


def canonical_json(value: Mapping[str, Any] | list[Any] | str | int | bool | None) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_json(value: Mapping[str, Any] | list[Any] | str | int | bool | None) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RequestSpec:
    request_id: uuid.UUID
    generation_id: uuid.UUID
    run_id: uuid.UUID
    owner_id: str
    principal_class: str
    command_name: str
    canonical_payload: Mapping[str, Any]
    protocol_release: str
    dish_release: str
    admitted_at: datetime


@dataclass(frozen=True)
class StoredOutcome:
    outcome_id: uuid.UUID
    outcome_class: str
    result_code: str
    http_status: int
    result_payload: Mapping[str, Any]
    immutable_success: bool
    recorded_at: datetime


@dataclass(frozen=True)
class RequestAdmission:
    request: wf.ServiceRequest
    replayed: bool
    outcome: wf.ServiceRequestOutcome | None


@dataclass(frozen=True)
class ExecutionSpec:
    execution_id: uuid.UUID
    request_id: uuid.UUID
    generation_id: uuid.UUID
    task_id: uuid.UUID | None
    operation_id: uuid.UUID | None
    command_name: str
    transaction_profile: str
    canonical_intent: Mapping[str, Any]
    pinned_inputs: Mapping[str, Any]
    contract_binding_id: uuid.UUID
    admitted_at: datetime


class WorkflowAuthorityRepository:
    """Low-level Stage 3 authority operations in a caller-owned session."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def require_active_generation(self, generation_id: uuid.UUID) -> models.AuthorityGeneration:
        generation = self.session.get(models.AuthorityGeneration, generation_id)
        if generation is None or generation.status != "active":
            raise StaleAuthorityError("authority generation is not active")
        return generation

    def require_active_run(
        self, *, generation_id: uuid.UUID, run_id: uuid.UUID, owner_id: str | None = None
    ) -> wf.ServiceRun:
        self.require_active_generation(generation_id)
        run = self.session.get(wf.ServiceRun, run_id)
        if (
            run is None
            or run.generation_id != generation_id
            or run.status != "active"
            or (owner_id is not None and run.owner_id != owner_id)
        ):
            raise StaleAuthorityError("run is stale, retired, or belongs to another generation")
        return run

    def register_run(self, row: wf.ServiceRun) -> None:
        self.require_active_generation(row.generation_id)
        if row.bootstrap_id is not None:
            bootstrap = self.session.get(models.GenerationBootstrapAuthority, row.bootstrap_id)
            if bootstrap is None or bootstrap.generation_id != row.generation_id:
                raise StaleAuthorityError("bootstrap authority does not match generation")
            if bootstrap.retired_at is not None:
                raise StaleAuthorityError("bootstrap authority is retired")
        self.session.add(row)
        self.session.flush()

    def admit_request(self, spec: RequestSpec) -> RequestAdmission:
        self.require_active_run(
            generation_id=spec.generation_id, run_id=spec.run_id, owner_id=spec.owner_id
        )
        payload = dict(spec.canonical_payload)
        payload_sha = sha256_json(payload)
        existing = self.session.get(wf.ServiceRequest, spec.request_id)
        if existing is not None:
            identity = (
                existing.generation_id == spec.generation_id
                and existing.run_id == spec.run_id
                and existing.owner_id == spec.owner_id
                and existing.principal_class == spec.principal_class
                and existing.command_name == spec.command_name
                and existing.canonical_payload_sha256 == payload_sha
                and existing.protocol_release == spec.protocol_release
                and existing.dish_release == spec.dish_release
            )
            if not identity:
                raise RequestIdentityConflict("service request identity conflict")
            outcome = self.session.scalar(
                select(wf.ServiceRequestOutcome).where(
                    wf.ServiceRequestOutcome.request_id == existing.request_id
                )
            )
            return RequestAdmission(existing, True, outcome)

        row = wf.ServiceRequest(
            request_id=spec.request_id,
            generation_id=spec.generation_id,
            run_id=spec.run_id,
            owner_id=spec.owner_id,
            principal_class=spec.principal_class,
            command_name=spec.command_name,
            canonical_payload_sha256=payload_sha,
            canonical_payload=payload,
            protocol_release=spec.protocol_release,
            dish_release=spec.dish_release,
            admitted_at=spec.admitted_at,
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            if "mutation admission is closed" in str(exc).lower():
                raise MutationAdmissionClosed("PostgreSQL mutation admission is closed") from exc
            raise ContentionLost("concurrent request admission won") from exc
        return RequestAdmission(row, False, None)

    def begin_execution(self, spec: ExecutionSpec) -> wf.CommandExecution:
        request = self.session.get(wf.ServiceRequest, spec.request_id)
        if request is None or request.generation_id != spec.generation_id:
            raise WorkflowAuthorityError("execution requires matching admitted request")
        self.require_active_generation(spec.generation_id)
        binding = self.session.get(models.HonestContractBinding, spec.contract_binding_id)
        if binding is None or binding.dish_release != request.dish_release:
            raise WorkflowAuthorityError("execution requires matching Honest contract binding")
        row = wf.CommandExecution(
            execution_id=spec.execution_id,
            generation_id=spec.generation_id,
            request_id=spec.request_id,
            task_id=spec.task_id,
            operation_id=spec.operation_id,
            command_name=spec.command_name,
            transaction_profile=spec.transaction_profile,
            canonical_intent=dict(spec.canonical_intent),
            pinned_inputs=dict(spec.pinned_inputs),
            contract_binding_id=spec.contract_binding_id,
            status="pending",
            claim_owner=None,
            claim_token=None,
            claim_expires_at=None,
            execution_revision=1,
            admitted_at=spec.admitted_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def claim_execution(
        self,
        *,
        execution_id: uuid.UUID,
        claimant: str,
        claim_token: uuid.UUID,
        now: datetime,
        ttl: timedelta,
    ) -> wf.CommandExecution:
        execution = self.session.get(wf.CommandExecution, execution_id)
        if execution is None:
            raise WorkflowAuthorityError("unknown command execution")
        self.require_active_generation(execution.generation_id)
        previous_revision = execution.execution_revision
        allowed = execution.status == "pending" or (
            execution.status == "claimed"
            and execution.claim_expires_at is not None
            and execution.claim_expires_at <= now
        )
        if not allowed:
            raise ContentionLost("execution is already claimed or terminal")
        prior_status = execution.status
        claim_expiry = now + ttl
        result = self.session.execute(
            update(wf.CommandExecution)
            .where(
                wf.CommandExecution.execution_id == execution_id,
                wf.CommandExecution.execution_revision == previous_revision,
                wf.CommandExecution.status == prior_status,
            )
            .values(
                status="claimed",
                claim_owner=claimant,
                claim_token=claim_token,
                claim_expires_at=claim_expiry,
                execution_revision=previous_revision + 1,
            )
        )
        if result.rowcount != 1:
            raise ContentionLost("execution claim lost to a concurrent claimant")
        self.session.add(
            wf.ExecutionClaimEvent(
                claim_event_id=uuid.uuid4(),
                execution_id=execution_id,
                claim_token=claim_token,
                event_kind="claimed" if prior_status == "pending" else "taken_over",
                claimant=claimant,
                expected_execution_revision=previous_revision,
                claim_expires_at=claim_expiry,
                occurred_at=now,
            )
        )
        self.session.flush()
        self.session.expire(execution)
        return execution

    def capture_task_fence(
        self, *, execution_id: uuid.UUID, generation_id: uuid.UUID, task_id: uuid.UUID, at: datetime
    ) -> wf.TaskExecutionFence:
        head = self.session.get(models.TaskAuthorityHead, (generation_id, task_id))
        if head is None:
            raise WorkflowAuthorityError("task has no authority head")
        row = wf.TaskExecutionFence(
            execution_id=execution_id,
            generation_id=generation_id,
            task_id=task_id,
            expected_task_revision=head.task_revision,
            expected_membership_revision=head.membership_revision,
            expected_placement_revision=head.placement_revision,
            expected_completion_revision=head.completion_revision,
            captured_at=at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def assert_task_fence(self, execution_id: uuid.UUID) -> models.TaskAuthorityHead:
        fence = self.session.get(wf.TaskExecutionFence, execution_id)
        if fence is None:
            raise WorkflowAuthorityError("execution has no task fence")
        head = self.session.get(models.TaskAuthorityHead, (fence.generation_id, fence.task_id))
        if head is None or (
            head.task_revision,
            head.membership_revision,
            head.placement_revision,
            head.completion_revision,
        ) != (
            fence.expected_task_revision,
            fence.expected_membership_revision,
            fence.expected_placement_revision,
            fence.expected_completion_revision,
        ):
            raise StaleAuthorityError("task fence is stale")
        return head

    def capture_operation_fence(
        self, *, execution_id: uuid.UUID, operation_id: uuid.UUID, at: datetime
    ) -> wf.OperationExecutionFence:
        operation = self.session.get(wf.WorkflowOperation, operation_id)
        if operation is None:
            raise WorkflowAuthorityError("unknown workflow operation")
        row = wf.OperationExecutionFence(
            execution_id=execution_id,
            operation_id=operation_id,
            expected_operation_revision=operation.operation_revision,
            expected_phase=operation.phase,
            captured_at=at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def assert_operation_fence(self, execution_id: uuid.UUID) -> wf.WorkflowOperation:
        fence = self.session.get(wf.OperationExecutionFence, execution_id)
        if fence is None:
            raise WorkflowAuthorityError("execution has no operation fence")
        operation = self.session.get(wf.WorkflowOperation, fence.operation_id)
        if operation is None or (
            operation.operation_revision != fence.expected_operation_revision
            or operation.phase != fence.expected_phase
        ):
            raise StaleAuthorityError("operation fence is stale")
        return operation

    def record_outcome(
        self,
        *,
        request_id: uuid.UUID,
        outcome: StoredOutcome,
        execution_id: uuid.UUID | None,
        audit_event_id: uuid.UUID,
        audit_event_type: str,
        actor: str,
        audit_payload: Mapping[str, Any],
        task_id: uuid.UUID | None,
        operation_id: uuid.UUID | None,
        obligation_id: uuid.UUID,
        invocation_metadata: Mapping[str, Any],
    ) -> wf.ServiceRequestOutcome:
        request = self.session.get(wf.ServiceRequest, request_id)
        if request is None:
            raise WorkflowAuthorityError("cannot complete an unknown request")
        existing = self.session.scalar(
            select(wf.ServiceRequestOutcome).where(wf.ServiceRequestOutcome.request_id == request_id)
        )
        if existing is not None:
            return existing
        payload = dict(outcome.result_payload)
        row = wf.ServiceRequestOutcome(
            outcome_id=outcome.outcome_id,
            request_id=request_id,
            outcome_class=outcome.outcome_class,
            result_code=outcome.result_code,
            http_status=outcome.http_status,
            result_payload=payload,
            result_sha256=sha256_json(payload),
            immutable_success=outcome.immutable_success,
            recorded_at=outcome.recorded_at,
        )
        self.session.add(row)
        self.session.flush()
        if execution_id is not None:
            execution = self.session.get(wf.CommandExecution, execution_id)
            if execution is None or execution.request_id != request_id:
                raise WorkflowAuthorityError("outcome execution does not own request")
            execution.status = (
                "committed"
                if outcome.outcome_class == "success"
                else "uncertain"
                if outcome.outcome_class == "uncertain"
                else "failed"
            )
            execution.claim_owner = None
            execution.claim_token = None
            execution.claim_expires_at = None
            execution.execution_revision += 1
            execution.terminal_at = outcome.recorded_at
        self.session.add(
            wf.GovernedAuditEvent(
                audit_event_id=audit_event_id,
                generation_id=request.generation_id,
                request_id=request_id,
                command_execution_id=execution_id,
                task_id=task_id,
                operation_id=operation_id,
                event_type=audit_event_type,
                actor=actor,
                payload=dict(audit_payload),
                occurred_at=outcome.recorded_at,
            )
        )
        invocation_payload = {
            "request_id": str(request_id),
            "outcome_id": str(outcome.outcome_id),
            "metadata": dict(invocation_metadata),
        }
        self.session.add(
            wf.InvocationAuditObligation(
                obligation_id=obligation_id,
                generation_id=request.generation_id,
                request_id=request_id,
                outcome_id=outcome.outcome_id,
                command_execution_id=execution_id,
                payload_sha256=sha256_json(invocation_payload),
                required_metadata=dict(invocation_metadata),
                state="pending",
                created_at=outcome.recorded_at,
                terminal_at=None,
            )
        )
        self.session.flush()
        return row


class WorkflowAuthorityService:
    """Stage 3 domain orchestration with caller-owned transaction boundaries."""

    def __init__(
        self,
        session: Session,
        *,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    ) -> None:
        self.session = session
        self.uuid_factory = uuid_factory
        self.repo = WorkflowAuthorityRepository(session)

    def register_run(
        self,
        *,
        run_id: uuid.UUID,
        generation_id: uuid.UUID,
        owner_id: str,
        agent: str,
        capability_digest: bytes,
        registered_at: datetime,
        bootstrap_id: uuid.UUID | None = None,
    ) -> wf.ServiceRun:
        row = wf.ServiceRun(
            run_id=run_id,
            generation_id=generation_id,
            owner_id=owner_id,
            agent=agent,
            capability_digest=capability_digest,
            bootstrap_id=bootstrap_id,
            status="active",
            registered_at=registered_at,
            retired_at=None,
        )
        self.repo.register_run(row)
        return row

    def admit_request(self, spec: RequestSpec) -> RequestAdmission:
        return self.repo.admit_request(spec)

    def begin_execution(self, spec: ExecutionSpec) -> wf.CommandExecution:
        return self.repo.begin_execution(spec)

    def create_operation(
        self,
        *,
        operation_id: uuid.UUID,
        execution_id: uuid.UUID,
        task_id: uuid.UUID,
        kind: str,
        phase: str,
        persisted_actions: list[str],
        created_at: datetime,
        predecessor_operation_id: uuid.UUID | None = None,
    ) -> wf.WorkflowOperation:
        execution = self.session.get(wf.CommandExecution, execution_id)
        if execution is None or execution.task_id != task_id:
            raise WorkflowAuthorityError("operation requires matching command execution")
        self.repo.assert_task_fence(execution_id)
        row = wf.WorkflowOperation(
            operation_id=operation_id,
            generation_id=execution.generation_id,
            task_id=task_id,
            kind=kind,
            lifecycle="open",
            phase=phase,
            persisted_actions=list(persisted_actions),
            creation_request_id=execution.request_id,
            creation_execution_id=execution_id,
            contract_binding_id=execution.contract_binding_id,
            predecessor_operation_id=predecessor_operation_id,
            terminal_outcome=None,
            operation_revision=1,
            created_at=created_at,
            terminal_at=None,
        )
        self.session.add(row)
        execution.operation_id = operation_id
        self.session.flush()
        return row

    def issue_planning_challenge(
        self,
        *,
        challenge_id: uuid.UUID,
        issuing_request_id: uuid.UUID,
        task_id: uuid.UUID,
        issued_at: datetime,
    ) -> wf.PlanningIntentChallenge:
        request = self.session.get(wf.ServiceRequest, issuing_request_id)
        if request is None or request.command_name != "start":
            raise WorkflowAuthorityError("planning challenge requires an admitted start request")
        run = self.repo.require_active_run(
            generation_id=request.generation_id, run_id=request.run_id, owner_id=request.owner_id
        )
        row = wf.PlanningIntentChallenge(
            challenge_id=challenge_id,
            generation_id=request.generation_id,
            issuing_request_id=issuing_request_id,
            task_id=task_id,
            run_id=request.run_id,
            owner_id=request.owner_id,
            agent=run.agent,
            target_kind="planning",
            state="issued",
            claiming_request_id=None,
            intent_basis=None,
            override_reason=None,
            resulting_operation_id=None,
            settled_by=None,
            settlement_reason=None,
            issued_at=issued_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def claim_planning_challenge(
        self,
        *,
        challenge_id: uuid.UUID,
        claiming_request_id: uuid.UUID,
        intent_basis: str,
        override_reason: str | None,
    ) -> wf.PlanningIntentChallenge:
        challenge = self.session.get(wf.PlanningIntentChallenge, challenge_id)
        request = self.session.get(wf.ServiceRequest, claiming_request_id)
        if challenge is None or request is None:
            raise WorkflowAuthorityError("unknown challenge or claiming request")
        if challenge.state != "issued":
            raise ContentionLost("planning challenge is no longer issuable")
        if (
            request.generation_id != challenge.generation_id
            or request.run_id != challenge.run_id
            or request.owner_id != challenge.owner_id
            or request.command_name != "start"
        ):
            raise WorkflowAuthorityError("claiming request does not match issued challenge")
        if intent_basis not in {"user_requested", "agent_override"}:
            raise WorkflowAuthorityError("invalid planning intent basis")
        if intent_basis == "agent_override" and not (override_reason or "").strip():
            raise WorkflowAuthorityError("agent override requires a reason")
        result = self.session.execute(
            update(wf.PlanningIntentChallenge)
            .where(
                wf.PlanningIntentChallenge.challenge_id == challenge_id,
                wf.PlanningIntentChallenge.state == "issued",
            )
            .values(
                state="claimed",
                claiming_request_id=claiming_request_id,
                intent_basis=intent_basis,
                override_reason=override_reason,
            )
        )
        if result.rowcount != 1:
            raise ContentionLost("planning challenge claim lost")
        self.session.flush()
        self.session.expire(challenge)
        return challenge

    def consume_planning_challenge(
        self,
        *,
        challenge_id: uuid.UUID,
        operation_id: uuid.UUID,
        consumed_at: datetime,
    ) -> wf.PlanningIntentChallenge:
        challenge = self.session.get(wf.PlanningIntentChallenge, challenge_id)
        operation = self.session.get(wf.WorkflowOperation, operation_id)
        if challenge is None or operation is None:
            raise WorkflowAuthorityError("unknown challenge or operation")
        if challenge.state != "claimed" or challenge.task_id != operation.task_id:
            raise WorkflowAuthorityError("challenge is not claimable for this operation")
        challenge.state = "consumed"
        challenge.resulting_operation_id = operation_id
        challenge.terminal_at = consumed_at
        self.session.flush()
        return challenge

    def settle_planning_challenge(
        self,
        *,
        challenge_id: uuid.UUID,
        actor: str,
        reason: str,
        settled_at: datetime,
    ) -> wf.PlanningIntentChallenge:
        challenge = self.session.get(wf.PlanningIntentChallenge, challenge_id)
        if challenge is None or challenge.state != "issued":
            raise WorkflowAuthorityError("only an issued challenge may be settled")
        if not reason.strip():
            raise WorkflowAuthorityError("settlement reason is required")
        challenge.state = "settled"
        challenge.settled_by = actor
        challenge.settlement_reason = reason
        challenge.terminal_at = settled_at
        self.session.flush()
        return challenge

    def acquire_actor_lease(
        self,
        *,
        lease_id: uuid.UUID,
        execution_id: uuid.UUID,
        operation_id: uuid.UUID,
        run_id: uuid.UUID,
        owner_id: str,
        actor_role: str,
        actor_attempt_sequence: int,
        issued_at: datetime,
        expires_at: datetime,
        verification_cycle_id: uuid.UUID | None = None,
    ) -> wf.ServiceLease:
        execution = self.session.get(wf.CommandExecution, execution_id)
        operation = self.session.get(wf.WorkflowOperation, operation_id)
        if execution is None or operation is None or execution.task_id != operation.task_id:
            raise WorkflowAuthorityError("lease requires matching execution and operation")
        self.repo.require_active_run(
            generation_id=execution.generation_id, run_id=run_id, owner_id=owner_id
        )
        row = wf.ServiceLease(
            lease_id=lease_id,
            generation_id=execution.generation_id,
            task_id=operation.task_id,
            operation_id=operation_id,
            run_id=run_id,
            owner_id=owner_id,
            lease_kind="actor",
            actor_role=actor_role,
            actor_attempt_sequence=actor_attempt_sequence,
            verification_cycle_id=verification_cycle_id,
            state="active",
            issued_at=issued_at,
            expires_at=expires_at,
            lease_revision=1,
            terminal_at=None,
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError as exc:
            raise ContentionLost("another active actor lease or attempt sequence already exists") from exc
        self.session.add(
            wf.LeaseEvent(
                lease_event_id=self.uuid_factory(),
                lease_id=lease_id,
                event_kind="issued",
                request_id=execution.request_id,
                command_execution_id=execution_id,
                prior_revision=0,
                resulting_revision=1,
                prior_expiry=issued_at,
                resulting_expiry=expires_at,
                reason="actor authority issued",
                occurred_at=issued_at,
            )
        )
        self.session.flush()
        return row

    def renew_lease(
        self,
        *,
        lease_id: uuid.UUID,
        execution_id: uuid.UUID,
        run_id: uuid.UUID,
        owner_id: str,
        now: datetime,
        new_expiry: datetime,
    ) -> wf.ServiceLease:
        lease = self.session.get(wf.ServiceLease, lease_id)
        execution = self.session.get(wf.CommandExecution, execution_id)
        if lease is None or execution is None:
            raise WorkflowAuthorityError("unknown lease or execution")
        if (
            lease.state != "active"
            or lease.run_id != run_id
            or lease.owner_id != owner_id
            or lease.expires_at <= now
            or new_expiry <= now
        ):
            raise StaleAuthorityError("lease is expired or caller does not own it")
        prior_revision = lease.lease_revision
        prior_expiry = lease.expires_at
        lease.expires_at = new_expiry
        lease.lease_revision += 1
        self.session.add(
            wf.LeaseEvent(
                lease_event_id=self.uuid_factory(),
                lease_id=lease_id,
                event_kind="renewed",
                request_id=execution.request_id,
                command_execution_id=execution_id,
                prior_revision=prior_revision,
                resulting_revision=prior_revision + 1,
                prior_expiry=prior_expiry,
                resulting_expiry=new_expiry,
                reason="owner renewal",
                occurred_at=now,
            )
        )
        self.session.flush()
        return lease

    def record_inspection(
        self,
        *,
        inspection_id: uuid.UUID,
        execution_id: uuid.UUID,
        cycle_id: uuid.UUID,
        actor_fact_id: uuid.UUID,
        verifier_run_id: uuid.UUID,
        attestation: str,
        inspected_at: datetime,
    ) -> wf.VerificationInspectionOccurrence:
        execution = self.session.get(wf.CommandExecution, execution_id)
        cycle = self.session.get(wf.VerificationCycle, cycle_id)
        actor = self.session.get(wf.OperationActorFact, actor_fact_id)
        if execution is None or cycle is None or actor is None:
            raise WorkflowAuthorityError("inspection authority is incomplete")
        if cycle.lifecycle != "open" or actor.operation_id != cycle.operation_id:
            raise WorkflowAuthorityError("inspection actor/cycle mismatch")
        if actor.run_id != verifier_run_id or not attestation.strip():
            raise WorkflowAuthorityError("inspection requires exact verifier run and attestation")
        placement = self.session.get(
            models.CurrentTaskSectionPlacement, (cycle.generation_id, cycle.task_id)
        )
        if placement is None:
            raise WorkflowAuthorityError("inspection requires current placement evidence")
        row = wf.VerificationInspectionOccurrence(
            inspection_id=inspection_id,
            cycle_id=cycle_id,
            operation_id=cycle.operation_id,
            task_id=cycle.task_id,
            reviewed_content_version_id=cycle.reviewed_content_version_id,
            verifier_actor_fact_id=actor_fact_id,
            verifier_run_id=verifier_run_id,
            attestation=attestation,
            section_id=placement.section_id,
            registry_version_id=placement.registry_version_id,
            placement_event_id=placement.latest_event_id,
            request_id=execution.request_id,
            command_execution_id=execution_id,
            inspected_at=inspected_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def create_actor_fact(
        self,
        *,
        actor_fact_id: uuid.UUID,
        execution_id: uuid.UUID,
        operation_id: uuid.UUID,
        run_id: uuid.UUID,
        owner_id: str,
        actor_role: str,
        agent: str,
        actor_attempt_sequence: int,
        recorded_at: datetime,
    ) -> wf.OperationActorFact:
        execution = self.session.get(wf.CommandExecution, execution_id)
        operation = self.session.get(wf.WorkflowOperation, operation_id)
        if execution is None or operation is None or execution.task_id != operation.task_id:
            raise WorkflowAuthorityError("actor fact requires matching execution and operation")
        self.repo.require_active_run(
            generation_id=execution.generation_id, run_id=run_id, owner_id=owner_id
        )
        row = wf.OperationActorFact(
            actor_fact_id=actor_fact_id,
            operation_id=operation_id,
            task_id=operation.task_id,
            actor_role=actor_role,
            agent=agent,
            owner_id=owner_id,
            run_id=run_id,
            actor_attempt_sequence=actor_attempt_sequence,
            command_execution_id=execution_id,
            recorded_at=recorded_at,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def grant_marco_authorization(
        self,
        *,
        grant_id: uuid.UUID,
        execution_id: uuid.UUID,
        task_id: uuid.UUID,
        operation_id: uuid.UUID | None,
        field_name: str,
        before_value: Any,
        after_value: Any,
        reason: str,
        actor: str,
        run_id: uuid.UUID,
        granted_at: datetime,
    ) -> wf.MarcoAuthorizationGrant:
        execution = self.session.get(wf.CommandExecution, execution_id)
        if execution is None or execution.task_id != task_id:
            raise WorkflowAuthorityError("authorization requires matching command execution")
        request = self.session.get(wf.ServiceRequest, execution.request_id)
        if request is None or request.principal_class != "admin":
            raise WorkflowAuthorityError("only an admitted admin request may grant authority")
        if not reason.strip():
            raise WorkflowAuthorityError("authorization reason is required")
        grant = wf.MarcoAuthorizationGrant(
            grant_id=grant_id,
            generation_id=execution.generation_id,
            task_id=task_id,
            operation_id=operation_id,
            field_name=field_name,
            before_value=before_value,
            after_value=after_value,
            reason=reason,
            actor=actor,
            run_id=run_id,
            request_id=execution.request_id,
            command_execution_id=execution_id,
            granted_at=granted_at,
        )
        self.session.add(grant)
        self.session.flush()
        self.session.add(
            wf.MarcoAuthorizationState(
                grant_id=grant_id,
                state="available",
                reservation_token=None,
                reservation_request_id=None,
                consumed_result_id=None,
                authorization_revision=1,
                updated_at=granted_at,
            )
        )
        self.session.flush()
        return grant

    def reserve_marco_authorization(
        self,
        *,
        grant_id: uuid.UUID,
        reservation_token: uuid.UUID,
        execution_id: uuid.UUID,
        reserved_at: datetime,
    ) -> wf.MarcoAuthorizationState:
        execution = self.session.get(wf.CommandExecution, execution_id)
        state = self.session.get(wf.MarcoAuthorizationState, grant_id)
        if execution is None or state is None:
            raise WorkflowAuthorityError("unknown authorization or execution")
        expected_revision = state.authorization_revision
        result = self.session.execute(
            update(wf.MarcoAuthorizationState)
            .where(
                wf.MarcoAuthorizationState.grant_id == grant_id,
                wf.MarcoAuthorizationState.state == "available",
                wf.MarcoAuthorizationState.authorization_revision == expected_revision,
            )
            .values(
                state="reserved",
                reservation_token=reservation_token,
                reservation_request_id=execution.request_id,
                authorization_revision=expected_revision + 1,
                updated_at=reserved_at,
            )
        )
        if result.rowcount != 1:
            raise ContentionLost("authorization reservation lost")
        self.session.add(
            wf.MarcoAuthorizationEvent(
                authorization_event_id=self.uuid_factory(),
                grant_id=grant_id,
                event_kind="reserved",
                reservation_token=reservation_token,
                request_id=execution.request_id,
                command_execution_id=execution_id,
                bound_result_id=None,
                occurred_at=reserved_at,
            )
        )
        self.session.flush()
        self.session.expire(state)
        return state

    def release_marco_authorization(
        self,
        *,
        grant_id: uuid.UUID,
        reservation_token: uuid.UUID,
        execution_id: uuid.UUID,
        released_at: datetime,
    ) -> wf.MarcoAuthorizationState:
        execution = self.session.get(wf.CommandExecution, execution_id)
        state = self.session.get(wf.MarcoAuthorizationState, grant_id)
        if execution is None or state is None:
            raise WorkflowAuthorityError("unknown authorization or execution")
        if state.state != "reserved" or state.reservation_token != reservation_token:
            raise StaleAuthorityError("authorization reservation does not match")
        revision = state.authorization_revision
        state.state = "available"
        state.reservation_token = None
        state.reservation_request_id = None
        state.authorization_revision = revision + 1
        state.updated_at = released_at
        self.session.add(
            wf.MarcoAuthorizationEvent(
                authorization_event_id=self.uuid_factory(),
                grant_id=grant_id,
                event_kind="released",
                reservation_token=reservation_token,
                request_id=execution.request_id,
                command_execution_id=execution_id,
                bound_result_id=None,
                occurred_at=released_at,
            )
        )
        self.session.flush()
        return state

    def consume_marco_authorization(
        self,
        *,
        grant_id: uuid.UUID,
        reservation_token: uuid.UUID,
        execution_id: uuid.UUID,
        bound_result_id: uuid.UUID,
        consumed_at: datetime,
    ) -> wf.MarcoAuthorizationState:
        execution = self.session.get(wf.CommandExecution, execution_id)
        state = self.session.get(wf.MarcoAuthorizationState, grant_id)
        if execution is None or state is None:
            raise WorkflowAuthorityError("unknown authorization or execution")
        if state.state != "reserved" or state.reservation_token != reservation_token:
            raise StaleAuthorityError("authorization reservation does not match")
        state.state = "consumed"
        state.consumed_result_id = bound_result_id
        state.authorization_revision += 1
        state.updated_at = consumed_at
        self.session.add(
            wf.MarcoAuthorizationEvent(
                authorization_event_id=self.uuid_factory(),
                grant_id=grant_id,
                event_kind="consumed",
                reservation_token=reservation_token,
                request_id=execution.request_id,
                command_execution_id=execution_id,
                bound_result_id=bound_result_id,
                occurred_at=consumed_at,
            )
        )
        self.session.flush()
        return state

    def open_verification_cycle(
        self,
        *,
        cycle_id: uuid.UUID,
        execution_id: uuid.UUID,
        operation_id: uuid.UUID,
        reviewed_content_version_id: uuid.UUID,
        created_at: datetime,
    ) -> wf.VerificationCycle:
        execution = self.session.get(wf.CommandExecution, execution_id)
        operation = self.session.get(wf.WorkflowOperation, operation_id)
        version = self.session.get(models.ContentVersion, reviewed_content_version_id)
        if execution is None or operation is None or version is None:
            raise WorkflowAuthorityError("verification cycle authority is incomplete")
        if execution.task_id != operation.task_id or version.task_id != operation.task_id:
            raise WorkflowAuthorityError("verification cycle task mismatch")
        row = wf.VerificationCycle(
            cycle_id=cycle_id,
            generation_id=execution.generation_id,
            task_id=operation.task_id,
            operation_id=operation_id,
            reviewed_content_version_id=reviewed_content_version_id,
            contract_binding_id=execution.contract_binding_id,
            lifecycle="open",
            outcome=None,
            created_by_execution_id=execution_id,
            created_at=created_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def signoff_verification(
        self,
        *,
        signoff_id: uuid.UUID,
        execution_id: uuid.UUID,
        cycle_id: uuid.UUID,
        inspection_id: uuid.UUID,
        signed_content_version_id: uuid.UUID,
        signoff_kind: str,
        signed_at: datetime,
        inherited_from_signoff_id: uuid.UUID | None = None,
    ) -> wf.VerificationSignoff:
        execution = self.session.get(wf.CommandExecution, execution_id)
        cycle = self.session.get(wf.VerificationCycle, cycle_id)
        inspection = self.session.get(wf.VerificationInspectionOccurrence, inspection_id)
        if execution is None or cycle is None or inspection is None:
            raise WorkflowAuthorityError("signoff authority is incomplete")
        if cycle.lifecycle != "open" or inspection.cycle_id != cycle_id:
            raise WorkflowAuthorityError("signoff inspection/cycle mismatch")
        if inspection.reviewed_content_version_id != signed_content_version_id:
            correction = self.session.scalar(
                select(wf.VerificationCorrection).where(
                    wf.VerificationCorrection.cycle_id == cycle_id,
                    wf.VerificationCorrection.source_content_version_id
                    == inspection.reviewed_content_version_id,
                    wf.VerificationCorrection.corrected_content_version_id
                    == signed_content_version_id,
                )
            )
            if correction is None:
                raise WorkflowAuthorityError(
                    "signoff must bind the inspected occurrence or its exact recorded correction"
                )
        row = wf.VerificationSignoff(
            signoff_id=signoff_id,
            cycle_id=cycle_id,
            task_id=cycle.task_id,
            signed_content_version_id=signed_content_version_id,
            inspection_id=inspection_id,
            verifier_actor_fact_id=inspection.verifier_actor_fact_id,
            inherited_from_signoff_id=inherited_from_signoff_id,
            signoff_kind=signoff_kind,
            command_execution_id=execution_id,
            signed_at=signed_at,
        )
        self.session.add(row)
        cycle.lifecycle = "approved"
        cycle.outcome = "approved"
        cycle.terminal_at = signed_at
        self.session.flush()
        return row

    def open_evidence_hold(
        self,
        *,
        hold_id: uuid.UUID,
        execution_id: uuid.UUID,
        operation_id: uuid.UUID,
        baseline_content_version_id: uuid.UUID,
        reason: str,
        opened_at: datetime,
        cycle_id: uuid.UUID | None = None,
    ) -> wf.EvidenceHold:
        execution = self.session.get(wf.CommandExecution, execution_id)
        operation = self.session.get(wf.WorkflowOperation, operation_id)
        if execution is None or operation is None or execution.task_id != operation.task_id:
            raise WorkflowAuthorityError("hold requires matching execution and operation")
        row = wf.EvidenceHold(
            hold_id=hold_id,
            generation_id=execution.generation_id,
            task_id=operation.task_id,
            operation_id=operation_id,
            cycle_id=cycle_id,
            baseline_content_version_id=baseline_content_version_id,
            state="open",
            reason=reason,
            opened_by_execution_id=execution_id,
            opened_at=opened_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        self.session.add(
            wf.EvidenceHoldEvent(
                hold_event_id=self.uuid_factory(),
                hold_id=hold_id,
                event_kind="opened",
                evidence_payload={"reason": reason},
                request_id=execution.request_id,
                command_execution_id=execution_id,
                occurred_at=opened_at,
            )
        )
        self.session.flush()
        return row

    def supply_evidence(
        self,
        *,
        hold_id: uuid.UUID,
        execution_id: uuid.UUID,
        evidence_payload: Mapping[str, Any],
        supplied_at: datetime,
    ) -> wf.EvidenceHold:
        hold = self.session.get(wf.EvidenceHold, hold_id)
        execution = self.session.get(wf.CommandExecution, execution_id)
        if hold is None or execution is None or hold.state != "open":
            raise WorkflowAuthorityError("evidence hold is not open")
        hold.state = "supplied"
        hold.terminal_at = supplied_at
        self.session.add(
            wf.EvidenceHoldEvent(
                hold_event_id=self.uuid_factory(),
                hold_id=hold_id,
                event_kind="supplied",
                evidence_payload=dict(evidence_payload),
                request_id=execution.request_id,
                command_execution_id=execution_id,
                occurred_at=supplied_at,
            )
        )
        self.session.flush()
        return hold

    def open_human_review(
        self,
        *,
        requirement_id: uuid.UUID,
        execution_id: uuid.UUID,
        operation_id: uuid.UUID,
        route: str,
        question: str,
        baseline_content_version_id: uuid.UUID,
        opened_at: datetime,
        cycle_id: uuid.UUID | None = None,
    ) -> wf.HumanReviewRequirement:
        execution = self.session.get(wf.CommandExecution, execution_id)
        operation = self.session.get(wf.WorkflowOperation, operation_id)
        if execution is None or operation is None or execution.task_id != operation.task_id:
            raise WorkflowAuthorityError("human review requires matching execution and operation")
        row = wf.HumanReviewRequirement(
            requirement_id=requirement_id,
            generation_id=execution.generation_id,
            task_id=operation.task_id,
            operation_id=operation_id,
            cycle_id=cycle_id,
            route=route,
            question=question,
            baseline_content_version_id=baseline_content_version_id,
            state="open",
            opened_by_execution_id=execution_id,
            opened_at=opened_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def record_human_decision(
        self,
        *,
        decision_id: uuid.UUID,
        requirement_id: uuid.UUID,
        execution_id: uuid.UUID,
        decision: str,
        rationale: str,
        actor: str,
        decided_at: datetime,
    ) -> wf.HumanReviewDecision:
        requirement = self.session.get(wf.HumanReviewRequirement, requirement_id)
        execution = self.session.get(wf.CommandExecution, execution_id)
        if requirement is None or execution is None or requirement.state != "open":
            raise WorkflowAuthorityError("human review requirement is not open")
        row = wf.HumanReviewDecision(
            decision_id=decision_id,
            requirement_id=requirement_id,
            decision=decision,
            rationale=rationale,
            actor=actor,
            request_id=execution.request_id,
            command_execution_id=execution_id,
            decided_at=decided_at,
        )
        self.session.add(row)
        requirement.state = "decided"
        requirement.terminal_at = decided_at
        self.session.flush()
        return row

    def begin_abandonment(
        self,
        *,
        abandonment_id: uuid.UUID,
        execution_id: uuid.UUID,
        source_operation_id: uuid.UUID,
        source_lease_id: uuid.UUID,
        reason: str,
        created_at: datetime,
        source_cycle_id: uuid.UUID | None = None,
    ) -> wf.AbandonmentAttempt:
        execution = self.session.get(wf.CommandExecution, execution_id)
        operation = self.session.get(wf.WorkflowOperation, source_operation_id)
        lease = self.session.get(wf.ServiceLease, source_lease_id)
        if execution is None or operation is None or lease is None:
            raise WorkflowAuthorityError("abandonment authority is incomplete")
        if lease.operation_id != source_operation_id or lease.task_id != operation.task_id:
            raise WorkflowAuthorityError("abandonment lease/operation mismatch")
        head = self.session.get(
            models.TaskAuthorityHead, (execution.generation_id, operation.task_id)
        )
        placement = self.session.get(
            models.CurrentTaskSectionPlacement, (execution.generation_id, operation.task_id)
        )
        if head is None or placement is None:
            raise WorkflowAuthorityError("abandonment requires exact task baseline")
        row = wf.AbandonmentAttempt(
            abandonment_id=abandonment_id,
            generation_id=execution.generation_id,
            task_id=operation.task_id,
            source_operation_id=source_operation_id,
            source_lease_id=source_lease_id,
            source_actor_attempt_sequence=lease.actor_attempt_sequence or 0,
            source_cycle_id=source_cycle_id,
            source_owner_id=lease.owner_id,
            source_run_id=lease.run_id,
            baseline_content_activation_id=head.current_content_activation_id,
            baseline_placement_event_id=placement.latest_event_id,
            reason=reason,
            state="preparing",
            request_id=execution.request_id,
            command_execution_id=execution_id,
            successor_operation_id=None,
            created_at=created_at,
            terminal_at=None,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def mark_abandonment_blocked(
        self, *, abandonment_id: uuid.UUID, reason: str
    ) -> wf.AbandonmentAttempt:
        attempt = self.session.get(wf.AbandonmentAttempt, abandonment_id)
        if attempt is None or attempt.state not in {"preparing", "reconciling"}:
            raise WorkflowAuthorityError("abandonment cannot enter blocked state")
        attempt.state = "blocked"
        attempt.reason = reason
        self.session.flush()
        return attempt

    def repair_invocation_audit(
        self,
        *,
        obligation_id: uuid.UUID,
        repair_identity: str,
        source: str,
        payload: Mapping[str, Any],
        outcome: str,
        recorded_at: datetime,
        quarantine_reason: str | None = None,
    ) -> wf.InvocationAuditRepair:
        obligation = self.session.get(wf.InvocationAuditObligation, obligation_id)
        if obligation is None:
            raise WorkflowAuthorityError("unknown invocation-audit obligation")
        payload_dict = dict(payload)
        payload_sha = sha256_json(payload_dict)
        existing = self.session.scalar(
            select(wf.InvocationAuditRepair).where(
                wf.InvocationAuditRepair.repair_identity == repair_identity
            )
        )
        if existing is not None:
            if existing.payload_sha256 != payload_sha or existing.obligation_id != obligation_id:
                raise RequestIdentityConflict("repair identity conflict")
            return existing
        row = wf.InvocationAuditRepair(
            repair_id=self.uuid_factory(),
            obligation_id=obligation_id,
            repair_identity=repair_identity,
            source=source,
            payload=payload_dict,
            payload_sha256=payload_sha,
            outcome=outcome,
            quarantine_reason=quarantine_reason,
            recorded_at=recorded_at,
        )
        self.session.add(row)
        obligation.state = outcome
        obligation.terminal_at = recorded_at
        self.session.flush()
        return row
