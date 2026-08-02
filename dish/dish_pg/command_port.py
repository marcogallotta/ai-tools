"""Stage 4 command/read port over PostgreSQL authority.

The port is transport-neutral and never commits. Callers own the transaction.
Every replay-bound command admits an immutable request before execution, and
all workflow legality is delegated to the shared planner/current policy.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models
from . import stage3_models as wf
from . import stage5_models as projection
from .command_contract import definition_for
from .command_effects import CommandEffectSpec, effect_spec_for
from .planner import (
    AuthorityFence,
    AuthoritativeSnapshot,
    CanonicalCommandIntent,
    plan_command,
)
from .read_model import PostgresReadModel, ReadModelError
from .transition import ProjectionService
from .workflow import ExecutionSpec, RequestSpec, StoredOutcome, WorkflowAuthorityService


PORTED_MUTATION_COMMANDS = frozenset({
    "create", "inspect", "start", "prepare", "approve", "reject", "submit",
    "renew-lease", "recover", "repair-destination", "discard",
    "abandon-operation", "reconcile-abandonment", "reopen-planning", "reopen",
    "supply-evidence", "record-human-decision", "authorize-governed-change",
    "recover-lease", "expire-lease", "migrate", "settle-planning-intent",
})


class CommandPortError(ValueError):
    """Base error for canonical command admission or execution."""


class CommandEffectMismatch(RuntimeError):
    """Committed handler effects disagree with the authoritative command specification."""


class CommandRuleError(CommandPortError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int = 409,
        data: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.data = dict(data or {})


@dataclass(frozen=True)
class CommandCall:
    command_name: str
    arguments: Mapping[str, Any]
    owner_id: str
    principal_class: str
    run_id: uuid.UUID
    request_id: uuid.UUID | None
    now: datetime
    protocol_release: str = "protocol-1"


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    command: str
    code: str
    http_status: int
    data: Mapping[str, Any]
    retryable: bool = False
    request_replayed: bool = False


class ProjectionAuthority(Protocol):
    def record(
        self,
        *,
        generation_id: uuid.UUID,
        execution_id: uuid.UUID,
        task_id: uuid.UUID,
        event_type: str,
        payload: Mapping[str, Any],
        created_at: datetime,
    ) -> uuid.UUID: ...

    def recover(
        self,
        *,
        attempt_id: uuid.UUID,
        route: str,
        arguments: Mapping[str, Any],
        actor: str,
        recovered_at: datetime,
        expected_task_id: uuid.UUID | None = None,
    ) -> Mapping[str, Any]: ...

    def unresolved_attempt_id(self, task_id: uuid.UUID) -> uuid.UUID | None: ...

    def task_freshness(self, task_id: uuid.UUID) -> Mapping[str, Any]: ...


class PostgresCommandPort:
    """Complete retained command surface in one caller-owned transaction."""

    def __init__(
        self,
        session: Session,
        *,
        cursor_secret: bytes,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        projection_recorder: ProjectionAuthority | None = None,
        lease_duration: timedelta = timedelta(minutes=15),
    ) -> None:
        self.session = session
        self.uuid_factory = uuid_factory
        self.reads = PostgresReadModel(session, cursor_secret=cursor_secret)
        self.workflow = WorkflowAuthorityService(session, uuid_factory=uuid_factory)
        self.projection_recorder: ProjectionAuthority = projection_recorder or ProjectionService(
            session, uuid_factory=uuid_factory
        )
        self.lease_duration = lease_duration

    def execute(self, call: CommandCall) -> CommandResult:
        definition = definition_for(call.command_name)
        if not definition.retained:
            return CommandResult(
                False,
                call.command_name,
                "COMMAND_RETIRED",
                410,
                {"retained": False},
            )
        if definition.profile == "Q":
            return self._execute_read(call)
        if call.request_id is None:
            raise CommandRuleError("REQUEST_ID_REQUIRED", "mutation requires request_id", http_status=400)

        generation = self.reads.active_generation()
        binding = self._binding_for(generation)
        payload = {
            "command": call.command_name,
            "arguments": dict(call.arguments),
            "owner_id": call.owner_id,
            "run_id": str(call.run_id),
        }
        admission = self.workflow.admit_request(
            RequestSpec(
                request_id=call.request_id,
                generation_id=generation.generation_id,
                run_id=call.run_id,
                owner_id=call.owner_id,
                principal_class=call.principal_class,
                command_name=call.command_name,
                canonical_payload=payload,
                protocol_release=call.protocol_release,
                dish_release=generation.dish_release,
                admitted_at=call.now,
            )
        )
        if admission.outcome is not None:
            outcome = admission.outcome
            return CommandResult(
                outcome.outcome_class == "success",
                call.command_name,
                outcome.result_code,
                outcome.http_status,
                dict(outcome.result_payload),
                retryable=False,
                request_replayed=True,
            )
        if admission.replayed:
            return CommandResult(
                False,
                call.command_name,
                "REQUEST_PENDING",
                409,
                {"request_id": str(call.request_id)},
                retryable=False,
                request_replayed=True,
            )

        task, operation = self._resolve_targets(call)
        if call.command_name == "start" and call.arguments.get("kind") == "planning" and not call.arguments.get("intent_challenge_id"):
            if task is None:
                raise CommandRuleError("TASK_REQUIRED", "planning start requires a task")
            challenge = self.workflow.issue_planning_challenge(
                challenge_id=self.uuid_factory(),
                issuing_request_id=call.request_id,
                task_id=task.task_id,
                issued_at=call.now,
            )
            data = {
                "request_id": str(call.request_id),
                "intent_challenge_id": str(challenge.challenge_id),
                "required_intent_basis": ["user_requested", "agent_override"],
            }
            self._store_outcome(
                call=call,
                execution_id=None,
                task_id=task.task_id,
                operation_id=None,
                ok=False,
                code="CONFIRMATION_REQUIRED",
                http_status=409,
                data=data,
                audit_event_type="planning_intent_challenge_issued",
            )
            return CommandResult(False, call.command_name, "CONFIRMATION_REQUIRED", 409, data)

        execution_id = self.uuid_factory()
        execution = self.workflow.begin_execution(
            ExecutionSpec(
                execution_id=execution_id,
                request_id=call.request_id,
                generation_id=generation.generation_id,
                task_id=task.task_id if task else None,
                operation_id=operation.operation_id if operation else None,
                command_name=call.command_name,
                transaction_profile=definition.profile,
                canonical_intent=payload,
                pinned_inputs={"now": call.now.isoformat()},
                contract_binding_id=binding.binding_id,
                admitted_at=call.now,
            )
        )
        self.workflow.repo.claim_execution(
            execution_id=execution_id,
            claimant=f"{call.owner_id}:{call.run_id}",
            claim_token=self.uuid_factory(),
            now=call.now,
            ttl=timedelta(minutes=2),
        )
        if task is not None:
            self.workflow.repo.capture_task_fence(
                execution_id=execution_id,
                generation_id=generation.generation_id,
                task_id=task.task_id,
                at=call.now,
            )
        if operation is not None:
            self.workflow.repo.capture_operation_fence(
                execution_id=execution_id,
                operation_id=operation.operation_id,
                at=call.now,
            )

        snapshot = self._planner_snapshot(generation.generation_id, task, operation)
        plan = plan_command(
            snapshot=snapshot,
            intent=CanonicalCommandIntent(
                command_name=call.command_name,
                arguments={**dict(call.arguments), "request_id": str(call.request_id)},
                principal_class=call.principal_class,
                owner_id=call.owner_id,
                run_id=str(call.run_id),
            ),
            pinned_now=call.now,
        )
        if not plan.legal:
            data = {"guidance": dict(plan.recovery_guidance)}
            self._store_outcome(
                call=call,
                execution_id=execution_id,
                task_id=task.task_id if task else None,
                operation_id=operation.operation_id if operation else None,
                ok=False,
                code=plan.result_code,
                http_status=409,
                data=data,
                audit_event_type=plan.audit_event_type,
            )
            return CommandResult(False, call.command_name, plan.result_code, 409, data)

        try:
            data = self._apply(
                call=call,
                generation=generation,
                binding=binding,
                execution=execution,
                task=task,
                operation=operation,
            )
        except CommandRuleError as exc:
            self._store_outcome(
                call=call,
                execution_id=execution_id,
                task_id=task.task_id if task else None,
                operation_id=operation.operation_id if operation else None,
                ok=False,
                code=exc.code,
                http_status=exc.http_status,
                data={"message": str(exc), **exc.data},
                audit_event_type=f"{call.command_name}_rejected",
            )
            return CommandResult(
                False,
                call.command_name,
                exc.code,
                exc.http_status,
                {"message": str(exc), **exc.data},
            )

        self.session.flush()
        self._assert_committed_effects(
            call=call,
            execution=execution,
            task=task,
            operation=operation,
            expected=effect_spec_for(call.command_name, call.arguments),
        )
        data = {"request_id": str(call.request_id), **data}
        self._store_outcome(
            call=call,
            execution_id=execution_id,
            task_id=execution.task_id,
            operation_id=execution.operation_id,
            ok=True,
            code="OK",
            http_status=200,
            data=data,
            audit_event_type=f"{call.command_name}_committed",
        )
        return CommandResult(True, call.command_name, "OK", 200, data)

    def _execute_read(self, call: CommandCall) -> CommandResult:
        if call.request_id is not None:
            raise CommandRuleError(
                "REQUEST_ID_NOT_ALLOWED", "read-only commands do not accept request_id", http_status=400
            )
        if call.command_name == "sections":
            data: Mapping[str, Any] = {"sections": self.reads.sections()}
        elif call.command_name == "section-tasks":
            reference = call.arguments.get("section_id") or call.arguments.get("section_gid")
            if reference is None:
                raise CommandRuleError("SECTION_REQUIRED", "section reference is required", http_status=400)
            page = self.reads.section_tasks(
                section_reference=str(reference),
                cursor=call.arguments.get("cursor"),
                page_size=int(call.arguments.get("page_size", 50)),
            )
            data = {
                "tasks": [asdict(item) | {"task_id": str(item.task_id), "section_id": str(item.section_id)} for item in page.items],
                "next_cursor": page.next_cursor,
                "registry_version_id": str(page.registry_version_id),
                "registry_revision": page.registry_revision,
            }
        elif call.command_name == "read":
            reference = call.arguments.get("task_id") or call.arguments.get("task_gid")
            if reference is None:
                raise CommandRuleError("TASK_REQUIRED", "task reference is required", http_status=400)
            view = self.reads.task_view(str(reference))
            freshness = dict(view.projection_freshness)
            freshness = dict(self.projection_recorder.task_freshness(view.task_id))
            data = asdict(view) | {
                "task_id": str(view.task_id),
                "content_version_id": str(view.content_version_id),
                "section_id": str(view.section_id),
                "operation_id": str(view.operation_id) if view.operation_id else None,
                "projection_freshness": freshness,
            }
        else:
            raise CommandRuleError("NOT_A_QUERY", "command is not a read query")
        return CommandResult(True, call.command_name, "OK", 200, data)

    def _binding_for(self, generation: models.AuthorityGeneration) -> models.HonestContractBinding:
        binding = self.session.scalar(
            select(models.HonestContractBinding)
            .where(models.HonestContractBinding.dish_release == generation.dish_release)
            .order_by(models.HonestContractBinding.resolved_at.desc())
            .limit(1)
        )
        if binding is None:
            raise CommandRuleError("CONTRACT_BINDING_MISSING", "active release has no Honest binding")
        return binding

    def _resolve_targets(
        self, call: CommandCall
    ) -> tuple[models.DishTask | None, wf.WorkflowOperation | None]:
        definition = definition_for(call.command_name)
        operation = None
        operation_ref = call.arguments.get("operation_id") or call.arguments.get("submission_id")
        if operation_ref:
            try:
                operation = self.session.get(wf.WorkflowOperation, uuid.UUID(str(operation_ref)))
            except ValueError as exc:
                raise CommandRuleError("INVALID_OPERATION_ID", "operation identifier must be a UUID", http_status=400) from exc
            if operation is None:
                raise CommandRuleError("OPERATION_NOT_FOUND", "unknown workflow operation", http_status=404)
        task = None
        task_ref = call.arguments.get("task_id") or call.arguments.get("task_gid")
        if task_ref:
            try:
                task = self.reads.resolve_task(str(task_ref))
            except ReadModelError as exc:
                raise CommandRuleError("TASK_NOT_FOUND", str(exc), http_status=404) from exc
        elif operation is not None:
            task = self.session.get(models.DishTask, operation.task_id)
        if definition.task_required and task is None:
            raise CommandRuleError("TASK_REQUIRED", "command requires a task", http_status=400)
        if definition.operation_required and operation is None:
            operation = self.session.scalar(
                select(wf.WorkflowOperation).where(
                    wf.WorkflowOperation.task_id == task.task_id,
                    wf.WorkflowOperation.lifecycle == "open",
                )
            ) if task is not None else None
            if operation is None:
                raise CommandRuleError("OPEN_OPERATION_REQUIRED", "command requires an open operation")
        if task is not None and operation is not None and operation.task_id != task.task_id:
            raise CommandRuleError("TARGET_MISMATCH", "task and operation do not match")
        return task, operation

    def _planner_snapshot(
        self,
        generation_id: uuid.UUID,
        task: models.DishTask | None,
        operation: wf.WorkflowOperation | None,
    ) -> AuthoritativeSnapshot:
        if task is None:
            return AuthoritativeSnapshot(generation_id=str(generation_id), task_id=None, fence=None, workflow=None, task_exists=False)
        view = self.reads.task_view(task.task_id)
        workflow_snapshot = None
        if operation is not None:
            workflow_snapshot = self.reads._workflow_snapshot(
                generation_id=generation_id,
                task_id=task.task_id,
                body=view.body,
                operation=operation,
            )
        return AuthoritativeSnapshot(
            generation_id=str(generation_id),
            task_id=str(task.task_id),
            fence=AuthorityFence(
                task_revision=view.task_revision,
                membership_revision=view.membership_revision,
                placement_revision=view.placement_revision,
                completion_revision=view.completion_revision,
                operation_revision=operation.operation_revision if operation else None,
                operation_phase=operation.phase if operation else None,
            ),
            workflow=workflow_snapshot,
            task_exists=True,
            current_content_version_id=str(view.content_version_id),
            current_section_id=str(view.section_id),
            completed=view.completed,
            active_lease_id=self._active_lease_id(task.task_id),
            unresolved_projection_attempt_id=self._unresolved_projection_attempt_id(task.task_id),
            open_hold_id=self._open_id(wf.EvidenceHold, task.task_id),
            open_human_requirement_id=self._open_id(wf.HumanReviewRequirement, task.task_id),
            open_abandonment_id=self._open_abandonment_id(task.task_id),
        )

    def _unresolved_projection_attempt_id(self, task_id: uuid.UUID) -> str | None:
        value = self.projection_recorder.unresolved_attempt_id(task_id)
        return str(value) if value else None

    def _active_lease_id(self, task_id: uuid.UUID) -> str | None:
        value = self.session.scalar(select(wf.ServiceLease.lease_id).where(wf.ServiceLease.task_id == task_id, wf.ServiceLease.state == "active"))
        return str(value) if value else None

    def _open_id(self, model: Any, task_id: uuid.UUID) -> str | None:
        state = "open"
        identity = model.hold_id if model is wf.EvidenceHold else model.requirement_id
        value = self.session.scalar(select(identity).where(model.task_id == task_id, model.state == state))
        return str(value) if value else None

    def _open_abandonment_id(self, task_id: uuid.UUID) -> str | None:
        value = self.session.scalar(
            select(wf.AbandonmentAttempt.abandonment_id).where(
                wf.AbandonmentAttempt.task_id == task_id,
                wf.AbandonmentAttempt.state.in_(("preparing", "published", "blocked", "reconciling")),
            )
        )
        return str(value) if value else None

    def _apply(
        self,
        *,
        call: CommandCall,
        generation: models.AuthorityGeneration,
        binding: models.HonestContractBinding,
        execution: wf.CommandExecution,
        task: models.DishTask | None,
        operation: wf.WorkflowOperation | None,
    ) -> dict[str, Any]:
        handlers = {
            "create": self._create,
            "start": self._start,
            "prepare": self._prepare,
            "inspect": self._inspect,
            "approve": self._approve,
            "reject": self._reject,
            "submit": self._submit,
            "renew-lease": self._renew_lease,
            "recover": self._projection_only,
            "repair-destination": self._projection_only,
            "discard": self._discard,
            "abandon-operation": self._abandon,
            "reconcile-abandonment": self._reconcile_abandonment,
            "reopen-planning": self._reopen_planning,
            "reopen": self._reopen,
            "supply-evidence": self._supply_evidence,
            "record-human-decision": self._record_human_decision,
            "authorize-governed-change": self._authorize,
            "recover-lease": self._release_lease,
            "expire-lease": self._release_lease,
            "migrate": self._migrate,
            "settle-planning-intent": self._settle_planning,
        }
        handler = handlers.get(call.command_name)
        if handler is None:
            raise CommandRuleError("COMMAND_NOT_PORTED", "retained command has no PostgreSQL handler")
        return handler(call, generation, binding, execution, task, operation)

    def _create(self, call, generation, binding, execution, _task, _operation) -> dict[str, Any]:
        title = str(call.arguments.get("title", "")).strip()
        if not title:
            raise CommandRuleError("TITLE_REQUIRED", "create requires a non-blank title", http_status=400)
        active = self.session.get(models.ActiveSectionRegistry, generation.generation_id)
        entry = self.session.scalar(
            select(models.SectionRegistryEntry).where(
                models.SectionRegistryEntry.registry_version_id == active.registry_version_id,
                models.SectionRegistryEntry.workflow_role == "research_queue",
            )
        ) if active else None
        if active is None or entry is None:
            raise CommandRuleError("RESEARCH_QUEUE_MISSING", "active registry has no Research Queue")
        section = self.session.get(models.GovernedSection, entry.section_id)
        task_id, version_id, activation_id = self.uuid_factory(), self.uuid_factory(), self.uuid_factory()
        body = str(call.arguments.get("body", ""))
        identity = hashlib.sha256((title + "\0" + body).encode()).hexdigest()
        task = models.DishTask(task_id=task_id, existence_state="ordinary", creation_route="create", import_run_id=None, command_execution_id=execution.execution_id, created_at=call.now, retired_at=None)
        version = models.ContentVersion(content_version_id=version_id, generation_id=generation.generation_id, task_id=task_id, representation_kind="document", title=title, body=body, identity_scheme="sha256-title-body-v1", content_identity=identity, creator_route="command_execution", import_run_id=None, command_execution_id=execution.execution_id, predecessor_content_version_id=None, contract_binding_id=binding.binding_id, created_at=call.now)
        activation = models.ContentActivation(content_activation_id=activation_id, generation_id=generation.generation_id, task_id=task_id, content_version_id=version_id, activation_route="command_execution", import_run_id=None, command_execution_id=execution.execution_id, task_revision=1, activated_at=call.now)
        head = models.TaskAuthorityHead(generation_id=generation.generation_id, task_id=task_id, current_content_activation_id=activation_id, task_revision=1, membership_revision=1, placement_revision=1, completion_revision=1, updated_at=call.now)
        membership_id, placement_id, completion_id = self.uuid_factory(), self.uuid_factory(), self.uuid_factory()
        membership_event = models.TaskProjectMembershipEvent(membership_event_id=membership_id, generation_id=generation.generation_id, task_id=task_id, project_id=section.project_id, event_kind="joined", membership_revision=1, provenance_route="command_execution", import_run_id=None, command_execution_id=execution.execution_id, occurred_at=call.now)
        current_membership = models.CurrentTaskProjectMembership(generation_id=generation.generation_id, task_id=task_id, project_id=section.project_id, latest_event_id=membership_id, is_member=True, membership_revision=1, updated_at=call.now)
        placement_event = models.TaskSectionPlacementEvent(placement_event_id=placement_id, generation_id=generation.generation_id, task_id=task_id, section_id=section.section_id, registry_version_id=active.registry_version_id, event_kind="placed", placement_revision=1, provenance_route="command_execution", import_run_id=None, command_execution_id=execution.execution_id, occurred_at=call.now)
        current_placement = models.CurrentTaskSectionPlacement(generation_id=generation.generation_id, task_id=task_id, section_id=section.section_id, registry_version_id=active.registry_version_id, latest_event_id=placement_id, placement_revision=1, updated_at=call.now)
        completion_event = models.TaskCompletionEvent(completion_event_id=completion_id, generation_id=generation.generation_id, task_id=task_id, completed=False, reason="archive", completion_revision=1, provenance_route="command_execution", import_run_id=None, command_execution_id=execution.execution_id, occurred_at=call.now)
        current_completion = models.CurrentTaskCompletion(generation_id=generation.generation_id, task_id=task_id, completed=False, latest_event_id=completion_id, completion_revision=1, updated_at=call.now)
        self.session.add(task)
        self.session.flush()
        self.session.add(version)
        self.session.flush()
        self.session.add(activation)
        self.session.flush()
        self.session.add(head)
        self.session.flush()
        self.session.add_all([membership_event, placement_event, completion_event])
        self.session.flush()
        self.session.add_all([current_membership, current_placement, current_completion])
        execution.task_id = task_id
        self.session.flush()
        projection_id = self._project(generation.generation_id, execution.execution_id, task_id, "create_task", {"title": title}, call.now)
        return {"task_id": str(task_id), "content_version_id": str(version_id), "projection_event_id": projection_id}

    def _start(self, call, generation, binding, execution, task, _operation) -> dict[str, Any]:
        assert task is not None
        kind = str(call.arguments.get("kind", ""))
        challenge_id = call.arguments.get("intent_challenge_id")
        if kind == "planning" and challenge_id:
            self.workflow.claim_planning_challenge(challenge_id=uuid.UUID(str(challenge_id)), claiming_request_id=call.request_id, intent_basis=str(call.arguments.get("intent_basis", "")), override_reason=call.arguments.get("override_reason"))
        phases = {"planning": "prepare_required", "initial": "prepare_required", "change": "prepare_required", "verification": "await_verification"}
        if kind not in phases:
            raise CommandRuleError("INVALID_OPERATION_KIND", "unsupported operation kind", http_status=400)
        operation_id = self.uuid_factory()
        operation = self.workflow.create_operation(operation_id=operation_id, execution_id=execution.execution_id, task_id=task.task_id, kind=kind, phase=phases[kind], persisted_actions=["prepare"] if kind != "verification" else ["inspect"], created_at=call.now)
        sequence = int(self.session.scalar(select(func.coalesce(func.max(wf.OperationActorFact.actor_attempt_sequence), 0)).where(wf.OperationActorFact.task_id == task.task_id)) or 0) + 1
        actor_fact = self.workflow.create_actor_fact(actor_fact_id=self.uuid_factory(), execution_id=execution.execution_id, operation_id=operation_id, run_id=call.run_id, owner_id=call.owner_id, actor_role="verification" if kind == "verification" else "author", agent=str(call.arguments.get("agent", "service")), actor_attempt_sequence=sequence, recorded_at=call.now)
        lease = self.workflow.acquire_actor_lease(lease_id=self.uuid_factory(), execution_id=execution.execution_id, operation_id=operation_id, run_id=call.run_id, owner_id=call.owner_id, actor_role=actor_fact.actor_role, actor_attempt_sequence=sequence, issued_at=call.now, expires_at=call.now + self.lease_duration)
        if kind == "planning" and challenge_id:
            self.workflow.consume_planning_challenge(challenge_id=uuid.UUID(str(challenge_id)), operation_id=operation_id, consumed_at=call.now)
        if kind == "verification":
            head = self.session.get(models.TaskAuthorityHead, (generation.generation_id, task.task_id))
            activation = self.session.get(models.ContentActivation, head.current_content_activation_id)
            self.workflow.open_verification_cycle(cycle_id=self.uuid_factory(), execution_id=execution.execution_id, operation_id=operation_id, reviewed_content_version_id=activation.content_version_id, created_at=call.now)
        return {"operation_id": str(operation_id), "lease_id": str(lease.lease_id), "phase": operation.phase}

    def _prepare(self, call, generation, binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        self.workflow.repo.assert_task_fence(execution.execution_id)
        self.workflow.repo.assert_operation_fence(execution.execution_id)
        head = self.session.get(models.TaskAuthorityHead, (generation.generation_id, task.task_id))
        prior_activation = self.session.get(models.ContentActivation, head.current_content_activation_id)
        prior = self.session.get(models.ContentVersion, prior_activation.content_version_id)
        body = str(call.arguments.get("file_text", call.arguments.get("body", prior.body)))
        title = str(call.arguments.get("title", prior.title)).strip() or prior.title
        version_id, activation_id = self.uuid_factory(), self.uuid_factory()
        identity = hashlib.sha256((title + "\0" + body).encode()).hexdigest()
        revision = head.task_revision + 1
        self.session.add(models.ContentVersion(content_version_id=version_id, generation_id=generation.generation_id, task_id=task.task_id, representation_kind="document", title=title, body=body, identity_scheme="sha256-title-body-v1", content_identity=identity, creator_route="command_execution", import_run_id=None, command_execution_id=execution.execution_id, predecessor_content_version_id=prior.content_version_id, contract_binding_id=binding.binding_id, created_at=call.now))
        self.session.flush()
        self.session.add(models.ContentActivation(content_activation_id=activation_id, generation_id=generation.generation_id, task_id=task.task_id, content_version_id=version_id, activation_route="command_execution", import_run_id=None, command_execution_id=execution.execution_id, task_revision=revision, activated_at=call.now))
        self.session.flush()
        head.current_content_activation_id = activation_id
        head.task_revision = revision
        head.updated_at = call.now
        verification_section_id = self._section_for_role(
            generation.generation_id,
            "verification_queue",
            missing_code="VERIFICATION_QUEUE_MISSING",
            missing_message="active registry has no Verification Queue",
        )
        self._set_placement(
            generation.generation_id,
            task.task_id,
            verification_section_id,
            execution.execution_id,
            call.now,
        )
        operation.phase = "await_verification"
        operation.persisted_actions = ["inspect"]
        operation.operation_revision += 1
        self.session.add(wf.OperationStep(step_id=self.uuid_factory(), operation_id=operation.operation_id, step_name=f"prepare-{operation.operation_revision}", step_sequence=self._next_step(operation.operation_id), outcome="complete", command_execution_id=execution.execution_id, evidence={"content_version_id": str(version_id), "section_id": str(verification_section_id)}, occurred_at=call.now))
        cycle = self.workflow.open_verification_cycle(cycle_id=self.uuid_factory(), execution_id=execution.execution_id, operation_id=operation.operation_id, reviewed_content_version_id=version_id, created_at=call.now)
        self.session.flush()
        projection_id = self._project(generation.generation_id, execution.execution_id, task.task_id, "update_task_document", {"content_version_id": str(version_id)}, call.now)
        placement_projection_id = self._project(generation.generation_id, execution.execution_id, task.task_id, "move_task", {"section_id": str(verification_section_id)}, call.now)
        return {"content_version_id": str(version_id), "cycle_id": str(cycle.cycle_id), "projection_event_id": projection_id, "placement_projection_event_id": placement_projection_id}

    def _inspect(self, call, generation, _binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        cycle = self._latest_cycle(operation.operation_id)
        self._assert_cycle_is_current(generation.generation_id, task.task_id, cycle)
        agent = str(call.arguments.get("agent", "")).strip()
        attestation = str(call.arguments.get("attestation", call.arguments.get("independence_attestation", ""))).strip()
        if not agent or not attestation:
            raise CommandRuleError(
                "VERIFIER_IDENTITY_REQUIRED",
                "inspect requires the exact verifier agent and independence attestation",
                http_status=400,
            )
        conflicting = self.session.scalar(
            select(wf.OperationActorFact).where(
                wf.OperationActorFact.operation_id == operation.operation_id,
                wf.OperationActorFact.actor_role != "verification",
                (wf.OperationActorFact.run_id == call.run_id)
                | (wf.OperationActorFact.agent == agent),
            ).limit(1)
        )
        if conflicting is not None:
            raise CommandRuleError(
                "VERIFIER_NOT_INDEPENDENT",
                "the author or material editor cannot inspect the same candidate",
                data={"conflicting_actor_fact_id": str(conflicting.actor_fact_id)},
            )
        actor = self.session.scalar(
            select(wf.OperationActorFact).where(
                wf.OperationActorFact.operation_id == operation.operation_id,
                wf.OperationActorFact.run_id == call.run_id,
                wf.OperationActorFact.actor_role == "verification",
            ).order_by(wf.OperationActorFact.recorded_at.desc()).limit(1)
        )
        if actor is not None and (actor.owner_id != call.owner_id or actor.agent != agent):
            raise CommandRuleError(
                "VERIFIER_IDENTITY_MISMATCH",
                "the verifier actor fact does not match the exact owner, run, and agent",
            )
        if actor is None:
            sequence = int(self.session.scalar(select(func.coalesce(func.max(wf.OperationActorFact.actor_attempt_sequence), 0)).where(wf.OperationActorFact.task_id == task.task_id)) or 0) + 1
            actor = self.workflow.create_actor_fact(actor_fact_id=self.uuid_factory(), execution_id=execution.execution_id, operation_id=operation.operation_id, run_id=call.run_id, owner_id=call.owner_id, actor_role="verification", agent=agent, actor_attempt_sequence=sequence, recorded_at=call.now)
        inspection = self.workflow.record_inspection(inspection_id=self.uuid_factory(), execution_id=execution.execution_id, cycle_id=cycle.cycle_id, actor_fact_id=actor.actor_fact_id, verifier_run_id=call.run_id, attestation=attestation, inspected_at=call.now)
        operation.persisted_actions = ["approve", "reject"]
        operation.operation_revision += 1
        return {"inspection_id": str(inspection.inspection_id), "cycle_id": str(cycle.cycle_id)}

    def _approve(self, call, generation, binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        cycle = self._latest_cycle(operation.operation_id)
        self._assert_cycle_is_current(generation.generation_id, task.task_id, cycle)
        inspection, _actor = self._exact_verifier_inspection(call, cycle)
        if call.arguments.get("semantic_review_complete") is False or call.arguments.get("provenance_complete") is False:
            raise CommandRuleError(
                "VERIFICATION_INPUTS_INCOMPLETE",
                "semantic review and provenance completion are required",
            )
        reviewed = self.session.get(models.ContentVersion, cycle.reviewed_content_version_id)
        if reviewed is None:
            raise CommandRuleError("REVIEWED_CONTENT_MISSING", "reviewed content version is missing")
        supplied_identity = call.arguments.get("reviewed_identity")
        if supplied_identity is not None and str(supplied_identity) != reviewed.content_identity:
            raise CommandRuleError(
                "REVIEWED_IDENTITY_MISMATCH",
                "the supplied reviewed identity does not match the inspected occurrence",
            )
        correction = str(call.arguments.get("correction", "none"))
        signed_version_id = cycle.reviewed_content_version_id
        projection_id = None
        signoff_kind = "direct"
        if correction == "small":
            body = call.arguments.get("file_text", call.arguments.get("body"))
            if body is None:
                raise CommandRuleError("SMALL_CORRECTION_CONTENT_REQUIRED", "small correction requires file_text", http_status=400)
            head = self.session.get(models.TaskAuthorityHead, (generation.generation_id, task.task_id))
            prior_activation = self.session.get(models.ContentActivation, head.current_content_activation_id)
            if prior_activation is None or prior_activation.content_version_id != cycle.reviewed_content_version_id:
                raise CommandRuleError("STALE_VERIFIER_REVIEW", "the current task no longer matches the inspected candidate")
            title = str(call.arguments.get("title", reviewed.title)).strip() or reviewed.title
            corrected_body = str(body)
            identity = hashlib.sha256((title + "\0" + corrected_body).encode()).hexdigest()
            if identity == reviewed.content_identity:
                raise CommandRuleError("SMALL_CORRECTION_REQUIRED", "small correction must create a distinct content occurrence")
            signed_version_id = self.uuid_factory()
            activation_id = self.uuid_factory()
            revision = head.task_revision + 1
            self.session.add(models.ContentVersion(content_version_id=signed_version_id, generation_id=generation.generation_id, task_id=task.task_id, representation_kind="document", title=title, body=corrected_body, identity_scheme="sha256-title-body-v1", content_identity=identity, creator_route="command_execution", import_run_id=None, command_execution_id=execution.execution_id, predecessor_content_version_id=reviewed.content_version_id, contract_binding_id=binding.binding_id, created_at=call.now))
            self.session.flush()
            self.session.add(models.ContentActivation(content_activation_id=activation_id, generation_id=generation.generation_id, task_id=task.task_id, content_version_id=signed_version_id, activation_route="command_execution", import_run_id=None, command_execution_id=execution.execution_id, task_revision=revision, activated_at=call.now))
            self.session.flush()
            self.session.add(wf.VerificationCorrection(correction_id=self.uuid_factory(), cycle_id=cycle.cycle_id, source_content_version_id=cycle.reviewed_content_version_id, corrected_content_version_id=signed_version_id, correction_class="small", reason=str(call.arguments.get("reason", "exact Small correction")), command_execution_id=execution.execution_id, recorded_at=call.now))
            self.session.flush()
            head.current_content_activation_id = activation_id
            head.task_revision = revision
            head.updated_at = call.now
            projection_id = self._project(generation.generation_id, execution.execution_id, task.task_id, "update_task_document", {"content_version_id": str(signed_version_id)}, call.now)
        elif correction != "none":
            raise CommandRuleError("INVALID_CORRECTION_CLASS", "correction must be none or small", http_status=400)
        signoff = self.workflow.signoff_verification(signoff_id=self.uuid_factory(), execution_id=execution.execution_id, cycle_id=cycle.cycle_id, inspection_id=inspection.inspection_id, signed_content_version_id=signed_version_id, signoff_kind=signoff_kind, signed_at=call.now)
        operation.phase = "await_submission"
        operation.persisted_actions = ["submit"]
        operation.operation_revision += 1
        return {"signoff_id": str(signoff.signoff_id), "cycle_id": str(cycle.cycle_id), "signed_content_version_id": str(signed_version_id), "correction": correction, "projection_event_id": projection_id}

    def _reject(self, call, generation, binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        cycle = self._latest_cycle(operation.operation_id)
        self._assert_cycle_is_current(generation.generation_id, task.task_id, cycle)
        self._exact_verifier_inspection(call, cycle)
        route = str(call.arguments.get("route", "large"))
        if route not in {"large", "evidence", "human-review", "human_review"}:
            raise CommandRuleError("INVALID_REJECTION_ROUTE", "route must be large, evidence, or human-review", http_status=400)
        cycle.lifecycle = "rejected"
        cycle.outcome = "rejected"
        cycle.terminal_at = call.now
        if route == "large":
            body = call.arguments.get("file_text", call.arguments.get("body"))
            if body is None:
                raise CommandRuleError("LARGE_CORRECTION_CONTENT_REQUIRED", "large rejection requires file_text", http_status=400)
            reviewed = self.session.get(models.ContentVersion, cycle.reviewed_content_version_id)
            head = self.session.get(models.TaskAuthorityHead, (generation.generation_id, task.task_id))
            title = str(call.arguments.get("title", reviewed.title)).strip() or reviewed.title
            corrected_body = str(body)
            version_id, activation_id = self.uuid_factory(), self.uuid_factory()
            identity = hashlib.sha256((title + "\0" + corrected_body).encode()).hexdigest()
            if identity == reviewed.content_identity:
                raise CommandRuleError("LARGE_CORRECTION_REQUIRED", "large rejection must create a distinct candidate")
            revision = head.task_revision + 1
            self.session.add(models.ContentVersion(content_version_id=version_id, generation_id=generation.generation_id, task_id=task.task_id, representation_kind="document", title=title, body=corrected_body, identity_scheme="sha256-title-body-v1", content_identity=identity, creator_route="command_execution", import_run_id=None, command_execution_id=execution.execution_id, predecessor_content_version_id=reviewed.content_version_id, contract_binding_id=binding.binding_id, created_at=call.now))
            self.session.flush()
            self.session.add(models.ContentActivation(content_activation_id=activation_id, generation_id=generation.generation_id, task_id=task.task_id, content_version_id=version_id, activation_route="command_execution", import_run_id=None, command_execution_id=execution.execution_id, task_revision=revision, activated_at=call.now))
            self.session.flush()
            self.session.add(wf.VerificationCorrection(correction_id=self.uuid_factory(), cycle_id=cycle.cycle_id, source_content_version_id=cycle.reviewed_content_version_id, corrected_content_version_id=version_id, correction_class="large", reason=str(call.arguments.get("reason", "large Verification correction")), command_execution_id=execution.execution_id, recorded_at=call.now))
            self.session.flush()
            head.current_content_activation_id = activation_id
            head.task_revision = revision
            head.updated_at = call.now
            next_cycle = self.workflow.open_verification_cycle(cycle_id=self.uuid_factory(), execution_id=execution.execution_id, operation_id=operation.operation_id, reviewed_content_version_id=version_id, created_at=call.now)
            operation.phase, operation.persisted_actions = "await_verification", ["inspect"]
            projection_id = self._project(generation.generation_id, execution.execution_id, task.task_id, "update_task_document", {"content_version_id": str(version_id)}, call.now)
            result = {"route": "large", "corrected_content_version_id": str(version_id), "new_cycle_id": str(next_cycle.cycle_id), "projection_event_id": projection_id}
        elif route == "evidence":
            hold = self.workflow.open_evidence_hold(hold_id=self.uuid_factory(), execution_id=execution.execution_id, operation_id=operation.operation_id, baseline_content_version_id=cycle.reviewed_content_version_id, reason=str(call.arguments.get("reason", "evidence required")), opened_at=call.now, cycle_id=cycle.cycle_id)
            operation.phase, operation.persisted_actions = "held_evidence", ["supply-evidence"]
            result = {"route": "evidence", "hold_id": str(hold.hold_id)}
        else:
            requirement = self.workflow.open_human_review(requirement_id=self.uuid_factory(), execution_id=execution.execution_id, operation_id=operation.operation_id, route="human_review", question=str(call.arguments.get("reason", "Marco decision required")), baseline_content_version_id=cycle.reviewed_content_version_id, opened_at=call.now, cycle_id=cycle.cycle_id)
            operation.phase, operation.persisted_actions = "held_human", ["record-human-decision"]
            result = {"route": "human-review", "requirement_id": str(requirement.requirement_id)}
        operation.operation_revision += 1
        return result

    def _submit(self, call, generation, _binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        cycle = self._latest_cycle(operation.operation_id)
        signoff = self.session.scalar(select(wf.VerificationSignoff).where(wf.VerificationSignoff.cycle_id == cycle.cycle_id))
        head = self.session.get(models.TaskAuthorityHead, (generation.generation_id, task.task_id))
        activation = self.session.get(models.ContentActivation, head.current_content_activation_id) if head else None
        if cycle.lifecycle != "approved" or signoff is None or activation is None or signoff.signed_content_version_id != activation.content_version_id:
            raise CommandRuleError("SIGNED_STATE_REQUIRED", "submit requires the exact approved current content occurrence")
        inspection = self.session.get(wf.VerificationInspectionOccurrence, signoff.inspection_id)
        if inspection is None or inspection.cycle_id != cycle.cycle_id or signoff.verifier_actor_fact_id != inspection.verifier_actor_fact_id:
            raise CommandRuleError("SIGNOFF_LINEAGE_INVALID", "submit signoff lineage is incomplete")
        target = call.arguments.get("destination_section_id") or call.arguments.get("destination_section_gid")
        if not target:
            raise CommandRuleError("DESTINATION_REQUIRED", "submit requires an exact destination section", http_status=400)
        section = self.reads.resolve_section(str(target))
        self._set_placement(generation.generation_id, task.task_id, section.section_id, execution.execution_id, call.now)
        operation.lifecycle = "completed"
        operation.phase = "completed"
        operation.terminal_outcome = "submitted"
        operation.terminal_at = call.now
        operation.operation_revision += 1
        projection_id = self._project(generation.generation_id, execution.execution_id, task.task_id, "move_task", {"destination_section_id": str(section.section_id)}, call.now)
        return {"operation_id": str(operation.operation_id), "destination_section_id": str(section.section_id), "projection_event_id": projection_id}

    def _renew_lease(self, call, _generation, _binding, execution, _task, operation) -> dict[str, Any]:
        lease_ref = call.arguments.get("lease_id")
        lease = self.session.get(wf.ServiceLease, uuid.UUID(str(lease_ref))) if lease_ref else self.session.scalar(select(wf.ServiceLease).where(wf.ServiceLease.operation_id == operation.operation_id, wf.ServiceLease.state == "active"))
        if lease is None:
            raise CommandRuleError("ACTIVE_LEASE_REQUIRED", "no active lease")
        row = self.workflow.renew_lease(lease_id=lease.lease_id, execution_id=execution.execution_id, run_id=call.run_id, owner_id=call.owner_id, now=call.now, new_expiry=call.now + self.lease_duration)
        return {"lease_id": str(row.lease_id), "expires_at": row.expires_at.isoformat(), "lease_revision": row.lease_revision}

    def _projection_only(self, call, _generation, _binding, _execution, task, _operation) -> dict[str, Any]:
        attempt_id = call.arguments.get("attempt_id")
        if not attempt_id:
            raise CommandRuleError("PROJECTION_ATTEMPT_REQUIRED", "attempt_id is required", http_status=400)
        try:
            result = self.projection_recorder.recover(
                attempt_id=uuid.UUID(str(attempt_id)),
                route=call.command_name,
                arguments=dict(call.arguments),
                actor=call.owner_id,
                recovered_at=call.now,
                expected_task_id=task.task_id if task is not None else None,
            )
        except ValueError as exc:
            raise CommandRuleError("PROJECTION_RECOVERY_REJECTED", str(exc)) from exc
        return dict(result)

    def _discard(self, call, generation, _binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        if operation.lifecycle != "open":
            raise CommandRuleError("OPEN_OPERATION_REQUIRED", "discard requires an open operation")
        steps = int(self.session.scalar(select(func.count()).select_from(wf.OperationStep).where(wf.OperationStep.operation_id == operation.operation_id)) or 0)
        creation_fence = self.session.get(wf.TaskExecutionFence, operation.creation_execution_id)
        head = self.session.get(models.TaskAuthorityHead, (generation.generation_id, task.task_id))
        placement = self.session.get(models.CurrentTaskSectionPlacement, (generation.generation_id, task.task_id))
        if creation_fence is None or head is None or placement is None:
            raise CommandRuleError("OPERATION_BASELINE_MISSING", "discard requires the immutable pre-operation baseline")
        baseline_matches = (
            head.task_revision == creation_fence.expected_task_revision
            and head.membership_revision == creation_fence.expected_membership_revision
            and head.placement_revision == creation_fence.expected_placement_revision
            and head.completion_revision == creation_fence.expected_completion_revision
        )
        prior_executions = int(self.session.scalar(select(func.count()).select_from(wf.CommandExecution).where(wf.CommandExecution.operation_id == operation.operation_id, wf.CommandExecution.execution_id.notin_([operation.creation_execution_id, execution.execution_id]))) or 0)
        projection_events = int(self.session.scalar(select(func.count()).select_from(projection.ProjectionOutboxEvent).join(wf.CommandExecution, wf.CommandExecution.execution_id == projection.ProjectionOutboxEvent.command_execution_id).where(wf.CommandExecution.operation_id == operation.operation_id)) or 0)
        if steps or prior_executions or projection_events or not baseline_matches or operation.operation_revision != 1:
            raise CommandRuleError(
                "OPERATION_NOT_PROVABLY_UNAPPLIED",
                "operation has workflow progress, external-effect intent, or baseline drift",
                data={"steps": steps, "prior_executions": prior_executions, "projection_events": projection_events, "baseline_matches": baseline_matches},
            )
        lease = self.session.scalar(select(wf.ServiceLease).where(wf.ServiceLease.operation_id == operation.operation_id, wf.ServiceLease.state == "active"))
        if lease is not None:
            self._terminalize_lease(lease, "released", execution, call.now, "operation discarded")
        operation.lifecycle = "cancelled_by_marco"
        operation.phase = "cancelled"
        operation.persisted_actions = []
        operation.terminal_outcome = "discarded"
        operation.terminal_at = call.now
        operation.operation_revision += 1
        return {"operation_id": str(operation.operation_id), "originating_request_id": str(operation.creation_request_id), "originating_execution_id": str(operation.creation_execution_id), "lifecycle": operation.lifecycle}

    def _abandon(self, call, generation, _binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        lease_id = call.arguments.get("lease_id")
        if not lease_id:
            raise CommandRuleError("SOURCE_LEASE_REQUIRED", "abandonment requires the exact actor lease", http_status=400)
        lease = self.session.get(wf.ServiceLease, uuid.UUID(str(lease_id)))
        if lease is None or lease.operation_id != operation.operation_id or lease.state != "active":
            raise CommandRuleError("SOURCE_LEASE_REQUIRED", "abandonment requires the exact active actor lease")
        source_cycle = self.session.scalar(select(wf.VerificationCycle).where(wf.VerificationCycle.operation_id == operation.operation_id, wf.VerificationCycle.lifecycle == "open"))
        attempt = self.workflow.begin_abandonment(abandonment_id=self.uuid_factory(), execution_id=execution.execution_id, source_operation_id=operation.operation_id, source_lease_id=lease.lease_id, reason=str(call.arguments.get("reason", "permanent abandonment")), created_at=call.now, source_cycle_id=source_cycle.cycle_id if source_cycle else None)
        operation.lifecycle = "abandoned"
        operation.terminal_outcome = "abandoned"
        operation.terminal_at = call.now
        operation.operation_revision += 1
        self._terminalize_lease(lease, "released", execution, call.now, "operation abandoned")
        if operation.phase in {"prepare_required", "await_verification", "await_submission"}:
            successor = self._publish_abandonment_successor(attempt, operation, execution, call.now)
            return {"abandonment_id": str(attempt.abandonment_id), "state": attempt.state, "successor_operation_id": str(successor.operation_id)}
        attempt.state = "blocked"
        return {"abandonment_id": str(attempt.abandonment_id), "state": attempt.state, "required_action": "reconcile-abandonment"}

    def _reconcile_abandonment(self, call, _generation, _binding, execution, task, _operation) -> dict[str, Any]:
        assert task is not None
        attempt_id = call.arguments.get("abandonment_id")
        if not attempt_id:
            raise CommandRuleError("ABANDONMENT_ID_REQUIRED", "reconciliation requires an exact abandonment_id", http_status=400)
        attempt = self.session.get(wf.AbandonmentAttempt, uuid.UUID(str(attempt_id)))
        if attempt is None or attempt.task_id != task.task_id or attempt.state != "blocked":
            raise CommandRuleError("BLOCKED_ABANDONMENT_REQUIRED", "no exact blocked abandonment")
        source = self.session.get(wf.WorkflowOperation, attempt.source_operation_id)
        if source is None:
            raise CommandRuleError("SOURCE_OPERATION_REQUIRED", "abandonment source operation is missing")
        successor = self._publish_abandonment_successor(attempt, source, execution, call.now)
        return {"abandonment_id": str(attempt.abandonment_id), "state": attempt.state, "successor_operation_id": str(successor.operation_id)}

    def _reopen_planning(self, call, generation, _binding, execution, task, _operation) -> dict[str, Any]:
        assert task is not None
        self._set_completion(generation.generation_id, task.task_id, False, "reopen_planning", execution.execution_id, call.now)
        projection_id = self._project(generation.generation_id, execution.execution_id, task.task_id, "set_completion", {"completed": False}, call.now)
        return {"task_id": str(task.task_id), "completed": False, "projection_event_id": projection_id}

    def _reopen(self, call, generation, _binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        if operation.phase != "held_human":
            raise CommandRuleError("HUMAN_REVIEW_HOLD_REQUIRED", "reopen requires the exact Human Review hold")
        requirement_id = call.arguments.get("requirement_id")
        if not requirement_id:
            raise CommandRuleError("REQUIREMENT_ID_REQUIRED", "reopen requires requirement_id", http_status=400)
        requirement = self.session.get(wf.HumanReviewRequirement, uuid.UUID(str(requirement_id)))
        decision = self.session.scalar(select(wf.HumanReviewDecision).where(wf.HumanReviewDecision.requirement_id == requirement.requirement_id)) if requirement else None
        if requirement is None or requirement.operation_id != operation.operation_id or requirement.state != "decided" or decision is None:
            raise CommandRuleError("DECIDED_HUMAN_REVIEW_REQUIRED", "the exact Human Review requirement is not decided")
        self._assert_baseline_content_current(generation.generation_id, task.task_id, requirement.baseline_content_version_id)
        prior = self.session.get(wf.VerificationCycle, requirement.cycle_id) if requirement.cycle_id else self._latest_cycle(operation.operation_id)
        cycle = self.workflow.open_verification_cycle(cycle_id=self.uuid_factory(), execution_id=execution.execution_id, operation_id=operation.operation_id, reviewed_content_version_id=requirement.baseline_content_version_id, created_at=call.now)
        operation.phase, operation.persisted_actions = "await_verification", ["inspect"]
        operation.operation_revision += 1
        return {"cycle_id": str(cycle.cycle_id), "reopened_from": str(prior.cycle_id), "requirement_id": str(requirement.requirement_id)}

    def _supply_evidence(self, call, generation, _binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        hold_id = call.arguments.get("hold_id")
        if not hold_id:
            raise CommandRuleError("HOLD_ID_REQUIRED", "supply-evidence requires hold_id", http_status=400)
        hold = self.session.get(wf.EvidenceHold, uuid.UUID(str(hold_id)))
        if hold is None or hold.operation_id != operation.operation_id or hold.state != "open" or operation.phase != "held_evidence":
            raise CommandRuleError("OPEN_EVIDENCE_HOLD_REQUIRED", "the exact Evidence hold is not open")
        self._assert_baseline_content_current(generation.generation_id, task.task_id, hold.baseline_content_version_id)
        self.workflow.supply_evidence(hold_id=hold.hold_id, execution_id=execution.execution_id, evidence_payload=dict(call.arguments.get("evidence", {})), supplied_at=call.now)
        operation.phase, operation.persisted_actions = "prepare_required", ["prepare"]
        operation.operation_revision += 1
        return {"hold_id": str(hold.hold_id), "state": hold.state}

    def _record_human_decision(self, call, generation, _binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        requirement_id = call.arguments.get("requirement_id")
        if not requirement_id:
            raise CommandRuleError("REQUIREMENT_ID_REQUIRED", "record-human-decision requires requirement_id", http_status=400)
        requirement = self.session.get(wf.HumanReviewRequirement, uuid.UUID(str(requirement_id)))
        if requirement is None or requirement.operation_id != operation.operation_id or requirement.state != "open" or operation.phase != "held_human":
            raise CommandRuleError("OPEN_HUMAN_REVIEW_REQUIRED", "the exact Human Review requirement is not open")
        self._assert_baseline_content_current(generation.generation_id, task.task_id, requirement.baseline_content_version_id)
        decision_value = str(call.arguments.get("decision", "")).strip()
        rationale = str(call.arguments.get("rationale", "")).strip()
        if not decision_value or not rationale:
            raise CommandRuleError("HUMAN_DECISION_INCOMPLETE", "decision and rationale are required", http_status=400)
        decision = self.workflow.record_human_decision(decision_id=self.uuid_factory(), requirement_id=requirement.requirement_id, execution_id=execution.execution_id, decision=decision_value, rationale=rationale, actor=call.owner_id, decided_at=call.now)
        return {"decision_id": str(decision.decision_id), "requirement_id": str(requirement.requirement_id), "state": requirement.state}

    def _authorize(self, call, _generation, _binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None
        field_name = str(call.arguments.get("field_name", "")).strip()
        reason = str(call.arguments.get("reason", "")).strip()
        if not field_name or not reason or "before" not in call.arguments or "after" not in call.arguments:
            raise CommandRuleError("AUTHORIZATION_SCOPE_REQUIRED", "field_name, before, after, and reason are required", http_status=400)
        grant = self.workflow.grant_marco_authorization(grant_id=self.uuid_factory(), execution_id=execution.execution_id, task_id=task.task_id, operation_id=operation.operation_id if operation else None, field_name=field_name, before_value=call.arguments["before"], after_value=call.arguments["after"], reason=reason, actor=call.owner_id, run_id=call.run_id, granted_at=call.now)
        return {"grant_id": str(grant.grant_id)}

    def _release_lease(self, call, _generation, _binding, execution, _task, operation) -> dict[str, Any]:
        lease_id = call.arguments.get("lease_id")
        if not lease_id:
            raise CommandRuleError("LEASE_ID_REQUIRED", f"{call.command_name} requires lease_id", http_status=400)
        lease = self.session.get(wf.ServiceLease, uuid.UUID(str(lease_id)))
        if lease is None or lease.state != "active" or (operation is not None and lease.operation_id != operation.operation_id):
            raise CommandRuleError("EXACT_LEASE_REQUIRED", "no matching active lease")
        if call.command_name == "recover-lease" and lease.expires_at > call.now:
            raise CommandRuleError("LEASE_NOT_EXPIRED", "recover-lease requires an expired lease")
        state = "recovered" if call.command_name == "recover-lease" else "expired"
        self._terminalize_lease(lease, state, execution, call.now, call.command_name)
        return {"lease_id": str(lease.lease_id), "state": lease.state}

    def _migrate(self, call, generation, binding, execution, task, _operation) -> dict[str, Any]:
        assert task is not None
        return self._prepare(call, generation, binding, execution, task, self._ensure_migration_operation(call, generation, binding, execution, task))

    def _ensure_migration_operation(self, call, _generation, _binding, execution, task):
        operation = self.session.scalar(select(wf.WorkflowOperation).where(wf.WorkflowOperation.task_id == task.task_id, wf.WorkflowOperation.lifecycle == "open"))
        if operation is None:
            operation = self.workflow.create_operation(operation_id=self.uuid_factory(), execution_id=execution.execution_id, task_id=task.task_id, kind="migration", phase="prepare_required", persisted_actions=["prepare"], created_at=call.now)
            self.workflow.repo.capture_operation_fence(execution_id=execution.execution_id, operation_id=operation.operation_id, at=call.now)
        return operation

    def _settle_planning(self, call, _generation, _binding, _execution, _task, _operation) -> dict[str, Any]:
        challenge_id = call.arguments.get("challenge_id")
        if not challenge_id:
            raise CommandRuleError("CHALLENGE_REQUIRED", "challenge_id is required", http_status=400)
        challenge = self.workflow.settle_planning_challenge(challenge_id=uuid.UUID(str(challenge_id)), actor=call.owner_id, reason=str(call.arguments.get("reason", "settled by Marco")), settled_at=call.now)
        return {"challenge_id": str(challenge.challenge_id), "state": challenge.state}

    def _current_content_version_id(self, generation_id: uuid.UUID, task_id: uuid.UUID) -> uuid.UUID:
        head = self.session.get(models.TaskAuthorityHead, (generation_id, task_id))
        activation = self.session.get(models.ContentActivation, head.current_content_activation_id) if head else None
        if activation is None:
            raise CommandRuleError("CONTENT_AUTHORITY_MISSING", "task has no current content occurrence")
        return activation.content_version_id

    def _assert_baseline_content_current(self, generation_id: uuid.UUID, task_id: uuid.UUID, expected_version_id: uuid.UUID) -> None:
        if self._current_content_version_id(generation_id, task_id) != expected_version_id:
            raise CommandRuleError("HOLD_BASELINE_DRIFT", "the task changed after the hold was created")

    def _assert_cycle_is_current(self, generation_id: uuid.UUID, task_id: uuid.UUID, cycle: wf.VerificationCycle) -> None:
        if cycle.lifecycle != "open":
            raise CommandRuleError("OPEN_VERIFICATION_CYCLE_REQUIRED", "Verification cycle is not open")
        if self._current_content_version_id(generation_id, task_id) != cycle.reviewed_content_version_id:
            raise CommandRuleError("STALE_VERIFIER_REVIEW", "the current task no longer matches the reviewed occurrence")

    def _exact_verifier_inspection(self, call: CommandCall, cycle: wf.VerificationCycle) -> tuple[wf.VerificationInspectionOccurrence, wf.OperationActorFact]:
        inspection = self.session.scalar(select(wf.VerificationInspectionOccurrence).where(wf.VerificationInspectionOccurrence.cycle_id == cycle.cycle_id).order_by(wf.VerificationInspectionOccurrence.inspected_at.desc()).limit(1))
        if inspection is None:
            raise CommandRuleError("INSPECTION_REQUIRED", f"{call.command_name} requires an exact inspection")
        actor = self.session.get(wf.OperationActorFact, inspection.verifier_actor_fact_id)
        agent = str(call.arguments.get("agent", "")).strip()
        if actor is None or actor.actor_role != "verification" or actor.run_id != call.run_id or actor.owner_id != call.owner_id or (agent and actor.agent != agent):
            raise CommandRuleError("VERIFIER_AUTHORITY_MISMATCH", "the command does not match the exact verifier occurrence")
        return inspection, actor

    def _publish_abandonment_successor(self, attempt: wf.AbandonmentAttempt, source: wf.WorkflowOperation, execution: wf.CommandExecution, published_at: datetime) -> wf.WorkflowOperation:
        head = self.session.get(models.TaskAuthorityHead, (attempt.generation_id, attempt.task_id))
        placement = self.session.get(models.CurrentTaskSectionPlacement, (attempt.generation_id, attempt.task_id))
        if head is None or placement is None or head.current_content_activation_id != attempt.baseline_content_activation_id or placement.latest_event_id != attempt.baseline_placement_event_id:
            raise CommandRuleError("ABANDONMENT_BASELINE_DRIFT", "the immutable abandonment baseline no longer matches current authority")
        existing = self.session.scalar(select(wf.OperationSuccessionEdge).where(wf.OperationSuccessionEdge.abandonment_id == attempt.abandonment_id))
        if existing is not None:
            successor = self.session.get(wf.WorkflowOperation, existing.successor_operation_id)
            if successor is None:
                raise CommandRuleError("SUCCESSOR_AUTHORITY_INCOMPLETE", "published successor operation is missing")
            return successor
        successor = self.workflow.create_operation(operation_id=self.uuid_factory(), execution_id=execution.execution_id, task_id=attempt.task_id, kind=source.kind, phase=source.phase, persisted_actions=list(source.persisted_actions), created_at=published_at, predecessor_operation_id=source.operation_id)
        prepared_cycle_id = None
        claim_mode = "operation"
        if attempt.source_cycle_id is not None:
            source_cycle = self.session.get(wf.VerificationCycle, attempt.source_cycle_id)
            if source_cycle is None:
                raise CommandRuleError("SOURCE_CYCLE_REQUIRED", "abandonment source cycle is missing")
            prepared_cycle = self.workflow.open_verification_cycle(cycle_id=self.uuid_factory(), execution_id=execution.execution_id, operation_id=successor.operation_id, reviewed_content_version_id=source_cycle.reviewed_content_version_id, created_at=published_at)
            prepared_cycle_id = prepared_cycle.cycle_id
            claim_mode = "operation_cycle"
        self.session.add(wf.OperationSuccessionEdge(succession_id=self.uuid_factory(), abandonment_id=attempt.abandonment_id, task_id=attempt.task_id, source_operation_id=source.operation_id, successor_operation_id=successor.operation_id, claim_mode=claim_mode, prepared_cycle_id=prepared_cycle_id, published_by_execution_id=execution.execution_id, published_at=published_at))
        attempt.state = "published"
        attempt.successor_operation_id = successor.operation_id
        return successor

    def _latest_cycle(self, operation_id: uuid.UUID) -> wf.VerificationCycle:
        cycle = self.session.scalar(select(wf.VerificationCycle).where(wf.VerificationCycle.operation_id == operation_id).order_by(wf.VerificationCycle.created_at.desc()).limit(1))
        if cycle is None:
            raise CommandRuleError("VERIFICATION_CYCLE_REQUIRED", "operation has no Verification cycle")
        return cycle

    def _next_step(self, operation_id: uuid.UUID) -> int:
        return int(self.session.scalar(select(func.coalesce(func.max(wf.OperationStep.step_sequence), 0)).where(wf.OperationStep.operation_id == operation_id)) or 0) + 1

    def _section_for_role(
        self,
        generation_id: uuid.UUID,
        workflow_role: str,
        *,
        missing_code: str,
        missing_message: str,
    ) -> uuid.UUID:
        active = self.session.get(models.ActiveSectionRegistry, generation_id)
        entry = (
            self.session.scalar(
                select(models.SectionRegistryEntry).where(
                    models.SectionRegistryEntry.registry_version_id == active.registry_version_id,
                    models.SectionRegistryEntry.workflow_role == workflow_role,
                )
            )
            if active is not None
            else None
        )
        if entry is None:
            raise CommandRuleError(missing_code, missing_message)
        return entry.section_id

    def _set_placement(self, generation_id, task_id, section_id, execution_id, at) -> None:
        current = self.session.get(models.CurrentTaskSectionPlacement, (generation_id, task_id))
        head = self.session.get(models.TaskAuthorityHead, (generation_id, task_id))
        active = self.session.get(models.ActiveSectionRegistry, generation_id)
        if current is None or head is None or active is None:
            raise CommandRuleError("PLACEMENT_AUTHORITY_MISSING", "task placement authority is incomplete")
        registered = self.session.get(models.SectionRegistryEntry, (active.registry_version_id, section_id))
        if registered is None:
            raise CommandRuleError("DESTINATION_NOT_REGISTERED", "destination is not in active registry")
        revision = head.placement_revision + 1
        event_id = self.uuid_factory()
        self.session.add(models.TaskSectionPlacementEvent(placement_event_id=event_id, generation_id=generation_id, task_id=task_id, section_id=section_id, registry_version_id=active.registry_version_id, event_kind="placed", placement_revision=revision, provenance_route="command_execution", import_run_id=None, command_execution_id=execution_id, occurred_at=at))
        self.session.flush()
        current.section_id, current.registry_version_id, current.latest_event_id, current.placement_revision, current.updated_at = section_id, active.registry_version_id, event_id, revision, at
        head.placement_revision, head.updated_at = revision, at

    def _set_completion(self, generation_id, task_id, completed, reason, execution_id, at) -> None:
        current = self.session.get(models.CurrentTaskCompletion, (generation_id, task_id))
        head = self.session.get(models.TaskAuthorityHead, (generation_id, task_id))
        if current is None or head is None:
            raise CommandRuleError("COMPLETION_AUTHORITY_MISSING", "task completion authority is incomplete")
        revision = head.completion_revision + 1
        event_id = self.uuid_factory()
        self.session.add(models.TaskCompletionEvent(completion_event_id=event_id, generation_id=generation_id, task_id=task_id, completed=completed, reason=reason, completion_revision=revision, provenance_route="command_execution", import_run_id=None, command_execution_id=execution_id, occurred_at=at))
        self.session.flush()
        current.completed, current.latest_event_id, current.completion_revision, current.updated_at = completed, event_id, revision, at
        head.completion_revision, head.updated_at = revision, at

    def _terminalize_lease(self, lease, state, execution, at, reason) -> None:
        prior_revision, prior_expiry = lease.lease_revision, lease.expires_at
        lease.state, lease.lease_revision, lease.terminal_at = state, prior_revision + 1, at
        self.session.add(wf.LeaseEvent(lease_event_id=self.uuid_factory(), lease_id=lease.lease_id, event_kind="released", request_id=execution.request_id, command_execution_id=execution.execution_id, prior_revision=prior_revision, resulting_revision=prior_revision + 1, prior_expiry=prior_expiry, resulting_expiry=prior_expiry, reason=reason, occurred_at=at))

    def _assert_committed_effects(
        self,
        *,
        call: CommandCall,
        execution: wf.CommandExecution,
        task: models.DishTask | None,
        operation: wf.WorkflowOperation | None,
        expected: CommandEffectSpec,
    ) -> None:
        projection_types = tuple(
            self.session.scalars(
                select(projection.ProjectionOutboxEvent.event_type)
                .where(
                    projection.ProjectionOutboxEvent.command_execution_id
                    == execution.execution_id
                )
                .order_by(projection.ProjectionOutboxEvent.aggregate_sequence)
            ).all()
        )
        if projection_types != expected.projection_event_types:
            raise CommandEffectMismatch(
                f"{call.command_name} projection effects mismatch: "
                f"expected {expected.projection_event_types!r}, observed {projection_types!r}"
            )

        if not expected.verify_mutation_effects:
            return
        if task is None or operation is None:
            raise CommandEffectMismatch(
                f"{call.command_name} effect verification requires task and operation authority"
            )

        observed: set[str] = set()
        execution_id = execution.execution_id
        if self.session.scalar(
            select(models.ContentActivation.content_activation_id).where(
                models.ContentActivation.command_execution_id == execution_id
            )
        ) is not None:
            observed.add(
                "activate_corrected_content_version"
                if call.command_name in {"approve", "reject"}
                else "activate_content_version"
            )
        if self.session.scalar(
            select(models.TaskSectionPlacementEvent.placement_event_id).where(
                models.TaskSectionPlacementEvent.command_execution_id == execution_id
            )
        ) is not None:
            observed.add("place_verification_queue")
        if self.session.scalar(
            select(wf.OperationStep.step_id).where(
                wf.OperationStep.command_execution_id == execution_id
            )
        ) is not None:
            observed.add("append_operation_step")
        if self.session.scalar(
            select(wf.VerificationCycle.cycle_id).where(
                wf.VerificationCycle.created_by_execution_id == execution_id
            )
        ) is not None:
            observed.add("open_verification_cycle")
        if self.session.scalar(
            select(wf.VerificationCorrection.correction_id).where(
                wf.VerificationCorrection.command_execution_id == execution_id
            )
        ) is not None:
            observed.add("record_verification_correction")
        if self.session.scalar(
            select(wf.VerificationSignoff.signoff_id).where(
                wf.VerificationSignoff.command_execution_id == execution_id
            )
        ) is not None:
            observed.add("record_verification_signoff")
        if self.session.scalar(
            select(wf.EvidenceHold.hold_id).where(
                wf.EvidenceHold.opened_by_execution_id == execution_id
            )
        ) is not None:
            observed.add("open_evidence_hold")
        if self.session.scalar(
            select(wf.HumanReviewRequirement.requirement_id).where(
                wf.HumanReviewRequirement.opened_by_execution_id == execution_id
            )
        ) is not None:
            observed.add("open_human_review")

        if call.command_name == "reject":
            rejected_cycle = self.session.scalar(
                select(wf.VerificationCycle.cycle_id).where(
                    wf.VerificationCycle.operation_id == operation.operation_id,
                    wf.VerificationCycle.lifecycle == "rejected",
                    wf.VerificationCycle.outcome == "rejected",
                    wf.VerificationCycle.terminal_at == call.now,
                )
            )
            if rejected_cycle is not None:
                observed.add("reject_verification_cycle")

        expected_phase = {
            "prepare": "await_verification",
            "approve": "await_submission",
            "reject": {
                "large": "await_verification",
                "evidence": "held_evidence",
                "human-review": "held_human",
                "human_review": "held_human",
            }.get(str(call.arguments.get("route", "large")), "held_human"),
        }[call.command_name]
        if operation.phase == expected_phase:
            observed.add("advance_operation")

        expected_mutations = set(expected.mutation_kinds)
        if observed != expected_mutations:
            raise CommandEffectMismatch(
                f"{call.command_name} authoritative effects mismatch: "
                f"expected {sorted(expected_mutations)!r}, observed {sorted(observed)!r}"
            )

    def _project(self, generation_id, execution_id, task_id, event_type, payload, at) -> str:
        value = self.projection_recorder.record(
            generation_id=generation_id,
            execution_id=execution_id,
            task_id=task_id,
            event_type=event_type,
            payload=payload,
            created_at=at,
        )
        return str(value)

    def _store_outcome(self, *, call, execution_id, task_id, operation_id, ok, code, http_status, data, audit_event_type) -> None:
        self.workflow.repo.record_outcome(
            request_id=call.request_id,
            outcome=StoredOutcome(outcome_id=self.uuid_factory(), outcome_class="success" if ok else "rule_error", result_code=code, http_status=http_status, result_payload=dict(data), immutable_success=ok, recorded_at=call.now),
            execution_id=execution_id,
            audit_event_id=self.uuid_factory(),
            audit_event_type=audit_event_type,
            actor=call.owner_id,
            audit_payload={"command": call.command_name, "code": code},
            task_id=task_id,
            operation_id=operation_id,
            obligation_id=self.uuid_factory(),
            invocation_metadata={"surface": "postgresql-port", "protocol_release": call.protocol_release},
        )
