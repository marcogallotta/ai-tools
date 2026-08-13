"""Stage 4 command/read port over PostgreSQL authority.

The port is transport-neutral and never commits. Callers own the transaction.
Every replay-bound command admits an immutable request before execution, and
all workflow legality is delegated to the shared planner/current policy.
"""
from __future__ import annotations

import copy
import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import models
from . import stage3_models as wf
from . import stage5_models as projection
from .command_contract import definition_for
from .command_effects import effect_spec_for
from .command_effect_runtime import (
    ProjectionAuthority,
    assert_committed_command_effects,
    record_projection_intent,
)
from .document_authority import (
    CanonicalDocumentError,
    destination_gid,
    held_document,
    parse_canonical_document,
    prepared_document,
    ready_document,
    resumed_document,
)
from .planner import (
    AuthorityFence,
    AuthoritativeSnapshot,
    CanonicalCommandIntent,
    plan_command,
)
from .read_model import PostgresReadModel, ReadModelError
from .repositories import (
    AuthorityRepository,
    CoreAuthorityError,
    REGISTRY_ROLE_CORRECTION_KIND,
    REGISTRY_ROLE_CORRECTION_SOURCE_RELEASE,
    RegistryRepository,
    registry_source_import_run,
)
from .transition import ProjectionService
from dish_tool.dish_urls import dish_uuid_from_url
from dish_tool.errors import DishRuleError
from dish_tool.task_urls import task_gid_from_url
from .workflow import (
    ContentionLost,
    ExecutionSpec,
    RequestSpec,
    StoredOutcome,
    VALIDATION_FAILURE_REQUEST_KIND,
    WorkflowAuthorityError,
    WorkflowAuthorityService,
)


class CommandPortError(ValueError):
    """Base error for canonical command admission or execution."""



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


def _task_reference_from_dish(value: str) -> str | None:
    """Reduce a legacy ``dish`` reference (Asana task URL, bare Asana gid, Dish
    frontend URL, or bare Dish UUID) to a value ``resolve_task`` can look up.

    Mirrors the reference shapes ``dish_tool.database._admin_target_task_gid``
    accepts, minus the SQLite-backed exact-operation-id fallback, which does
    not apply to task resolution.
    """
    clean = value.strip()
    if not clean:
        return None
    if clean.isdecimal():
        return clean
    if clean.startswith("/dishes/") or "://" in clean:
        try:
            return dish_uuid_from_url(clean)
        except DishRuleError:
            if clean.startswith("/dishes/"):
                return None
        try:
            return task_gid_from_url(clean)
        except DishRuleError:
            return None
    return clean


class PostgresCommandPort:
    """Complete retained command surface in one caller-owned transaction."""

    def __init__(
        self,
        session: Session,
        *,
        cursor_secret: bytes,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        projection_recorder: ProjectionAuthority | None = None,
        projection_origin: str = "live",
        lease_duration: timedelta = timedelta(minutes=15),
    ) -> None:
        self.session = session
        self.uuid_factory = uuid_factory
        self.reads = PostgresReadModel(session, cursor_secret=cursor_secret)
        self.workflow = WorkflowAuthorityService(session, uuid_factory=uuid_factory)
        if projection_origin not in {"live", "shadow"}:
            raise ValueError("projection_origin must be 'live' or 'shadow'")
        self.projection_origin = projection_origin
        self.projection_recorder: ProjectionAuthority = projection_recorder or ProjectionService(
            session, uuid_factory=uuid_factory
        )
        self.lease_duration = lease_duration

    def record_validation_failure(
        self,
        call: CommandCall,
        *,
        result_payload: Mapping[str, Any],
        invocation_surface: str,
    ) -> tuple[dict[str, Any], bool]:
        """Record a pre-execution rule failure through canonical replay authority."""

        if call.request_id is None:
            raise CommandRuleError(
                "REQUEST_ID_REQUIRED",
                "validation failure requires request_id",
                http_status=400,
            )
        generation = self.session.scalar(
            select(models.AuthorityGeneration).where(
                models.AuthorityGeneration.status == "active"
            )
        )
        if generation is None:
            raise WorkflowAuthorityError("no active authority generation")
        try:
            binding = RegistryRepository(self.session).active_release_contract(
                generation.generation_id
            ).honest_binding
        except CoreAuthorityError as exc:
            raise WorkflowAuthorityError(str(exc)) from exc
        validation_error = {
            "code": result_payload["code"],
            "retryable": result_payload["retryable"],
            "message": result_payload["data"]["message"],
            "errors": [dict(item) for item in result_payload["errors"]],
        }
        admission = self.workflow.record_validation_failure(
            spec=RequestSpec(
                request_id=call.request_id,
                generation_id=generation.generation_id,
                run_id=call.run_id,
                owner_id=call.owner_id,
                principal_class=call.principal_class,
                command_name=call.command_name,
                canonical_payload={
                    "request_kind": VALIDATION_FAILURE_REQUEST_KIND,
                    "command": call.command_name,
                    "arguments": dict(call.arguments),
                    "owner_id": call.owner_id,
                    "run_id": str(call.run_id),
                    "validation_error": validation_error,
                },
                protocol_release=binding.protocol_release,
                dish_release=generation.dish_release,
                admitted_at=call.now,
            ),
            outcome=StoredOutcome(
                outcome_id=self.uuid_factory(),
                outcome_class="rule_error",
                result_code=validation_error["code"],
                http_status=400,
                result_payload=dict(result_payload),
                immutable_success=False,
                recorded_at=call.now,
            ),
            audit_event_id=self.uuid_factory(),
            audit_event_type=f"{call.command_name}_validation_rejected",
            actor=f"{call.owner_id}:{call.run_id}",
            audit_payload={
                "code": validation_error["code"],
                "data": dict(result_payload["data"]),
                "errors": validation_error["errors"],
            },
            obligation_id=self.uuid_factory(),
            invocation_metadata={
                "surface": invocation_surface,
                "protocol_release": binding.protocol_release,
            },
        )
        if admission.outcome is None:
            raise WorkflowAuthorityError(
                "validation request has no authoritative outcome"
            )
        return copy.deepcopy(admission.outcome.result_payload), admission.replayed

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
            try:
                return self._execute_read(call)
            except CommandRuleError as exc:
                return CommandResult(
                    False,
                    call.command_name,
                    exc.code,
                    exc.http_status,
                    dict(exc.data),
                )
        if call.request_id is None:
            raise CommandRuleError(
                "REQUEST_ID_REQUIRED", "mutation requires request_id", http_status=400
            )

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

        task: models.DishTask | None = None
        operation: wf.WorkflowOperation | None = None
        execution: wf.CommandExecution | None = None
        execution_id: uuid.UUID | None = None
        try:
            task, operation = self._resolve_targets(call)
            if (
                call.command_name == "start"
                and call.arguments.get("kind") == "planning"
                and not call.arguments.get("intent_challenge_id")
            ):
                if task is None:
                    raise CommandRuleError("TASK_REQUIRED", "planning start requires a task")
                self._validate_planning_intent_basis(call, initial=True)
                self._validate_planning_agent(
                    generation_id=generation.generation_id, call=call
                )
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
                return CommandResult(
                    False, call.command_name, "CONFIRMATION_REQUIRED", 409, data
                )

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
            if operation is not None and call.command_name in {
                "prepare",
                "discard",
                "hold-reject",
            }:
                operation = self._lock_operation_transition(operation.operation_id)
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
                return CommandResult(
                    False, call.command_name, plan.result_code, 409, data
                )

            with self.session.begin_nested():
                data = self._apply(
                    call=call,
                    generation=generation,
                    binding=binding,
                    execution=execution,
                    task=task,
                    operation=operation,
                )
                preconstruction_hold = bool(data.pop("_preconstruction_hold", False))
                self.session.flush()
                assert_committed_command_effects(
                    self.session,
                    command_name=call.command_name,
                    arguments=call.arguments,
                    now=call.now,
                    execution=execution,
                    task=task,
                    operation=operation,
                    expected=effect_spec_for(
                        call.command_name,
                        call.arguments,
                        verification_hold=bool(data.get("verification_hold")),
                        preconstruction_hold=preconstruction_hold,
                    ),
                    result_data=data,
                )
        except CanonicalDocumentError as exc:
            rule_error = CommandRuleError(
                "VALIDATION_FAILED",
                str(exc),
                http_status=400,
                data={"errors": list(exc.errors)},
            )
            return self._record_rule_failure(
                call, rule_error, execution_id, task, operation
            )
        except CommandRuleError as exc:
            return self._record_rule_failure(
                call, exc, execution_id, task, operation
            )
        except ContentionLost as exc:
            return self._record_rule_failure(
                call,
                CommandRuleError("AUTHORITY_CONTENTION", str(exc)),
                execution_id,
                task,
                operation,
            )
        except WorkflowAuthorityError as exc:
            return self._record_rule_failure(
                call,
                CommandRuleError("AUTHORITY_MISMATCH", str(exc)),
                execution_id,
                task,
                operation,
            )

        assert execution is not None and execution_id is not None
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

    def _record_rule_failure(
        self,
        call: CommandCall,
        exc: CommandRuleError,
        execution_id: uuid.UUID | None,
        task: models.DishTask | None,
        operation: wf.WorkflowOperation | None,
    ) -> CommandResult:
        data = {"message": str(exc), **exc.data}
        self._store_outcome(
            call=call,
            execution_id=execution_id,
            task_id=task.task_id if task else None,
            operation_id=operation.operation_id if operation else None,
            ok=False,
            code=exc.code,
            http_status=exc.http_status,
            data=data,
            audit_event_type=f"{call.command_name}_rejected",
        )
        return CommandResult(
            False, call.command_name, exc.code, exc.http_status, data
        )

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
            try:
                self.reads.resolve_section(str(reference))
            except ReadModelError as exc:
                raise CommandRuleError(
                    "SECTION_NOT_FOUND",
                    str(exc),
                    http_status=404,
                    data={"section_reference": str(reference)},
                ) from exc
            page = self.reads.section_tasks(
                section_reference=str(reference),
                cursor=call.arguments.get("cursor"),
                page_size=int(call.arguments.get("page_size", 50)),
            )
            data = {
                "tasks": [
                    asdict(item)
                    | {
                        "dish_id": str(item.task_id),
                        "task_id": str(item.task_id),
                        "task_gid": item.external_task_id,
                        "section_id": str(item.section_id),
                    }
                    for item in page.items
                ],
                "next_cursor": page.next_cursor,
                "registry_version_id": str(page.registry_version_id),
                "registry_revision": page.registry_revision,
            }
        elif call.command_name == "attention":
            if call.principal_class != "admin":
                raise CommandRuleError(
                    "PRINCIPAL_SCOPE_MISMATCH",
                    "attention is available only on the private admin surface",
                    http_status=403,
                )
            data = self._attention()
        elif call.command_name == "holds":
            if call.principal_class != "admin":
                raise CommandRuleError(
                    "PRINCIPAL_SCOPE_MISMATCH",
                    "holds is available only on the private admin surface",
                    http_status=403,
                )
            data = self._holds()
        elif call.command_name == "read":
            reference = (
                call.arguments.get("dish_id")
                or call.arguments.get("task_id")
                or call.arguments.get("task_gid")
            )
            if reference is None:
                raise CommandRuleError("TASK_REQUIRED", "task reference is required", http_status=400)
            try:
                task = self.reads.resolve_task(str(reference))
            except ReadModelError as exc:
                raise CommandRuleError(
                    "DISH_NOT_FOUND",
                    str(exc),
                    http_status=404,
                    data={"dish_reference": str(reference)},
                ) from exc
            view = self.reads.task_view(task.task_id)
            task_gid = self.session.scalar(
                select(models.TaskExternalAlias.external_id).where(
                    models.TaskExternalAlias.task_id == view.task_id,
                    models.TaskExternalAlias.external_system == "asana",
                    models.TaskExternalAlias.state == "active",
                )
            )
            freshness = dict(view.projection_freshness)
            freshness = dict(self.projection_recorder.task_freshness(view.task_id))
            data = asdict(view) | {
                "dish_id": str(view.task_id),
                "task_id": str(view.task_id),
                "content_version_id": str(view.content_version_id),
                "section_id": str(view.section_id),
                "operation_id": str(view.operation_id) if view.operation_id else None,
                "projection_freshness": freshness,
                "identity_binding": {
                    "dish_id": str(view.task_id),
                    "task_gid": task_gid,
                },
            }
        else:
            raise CommandRuleError("NOT_A_QUERY", "command is not a read query")
        return CommandResult(True, call.command_name, "OK", 200, data)


    def _attention(self) -> Mapping[str, Any]:
        """Return a conservative, read-only global workflow attention view."""

        generation = self.reads.active_generation()
        now = datetime.now().astimezone()
        operations = self.session.scalars(
            select(wf.WorkflowOperation)
            .where(
                wf.WorkflowOperation.generation_id == generation.generation_id,
                wf.WorkflowOperation.lifecycle == "open",
            )
            .order_by(wf.WorkflowOperation.created_at, wf.WorkflowOperation.operation_id)
        ).all()
        items: list[dict[str, Any]] = []
        healthy_count = 0
        counts = {"safe_cleanup": 0, "multi_step_safe": 0, "needs_marco": 0, "unsafe": 0}
        for operation in operations:
            lease = self.session.scalar(
                select(wf.ServiceLease)
                .where(
                    wf.ServiceLease.operation_id == operation.operation_id,
                    wf.ServiceLease.lease_kind == "actor",
                    wf.ServiceLease.state == "active",
                )
                .order_by(wf.ServiceLease.issued_at.desc())
                .limit(1)
            )
            abandonment = self.session.scalar(
                select(wf.AbandonmentAttempt)
                .where(
                    wf.AbandonmentAttempt.source_operation_id == operation.operation_id,
                    wf.AbandonmentAttempt.state.in_(("preparing", "published", "blocked", "reconciling")),
                )
                .order_by(wf.AbandonmentAttempt.created_at.desc())
                .limit(1)
            )
            uncertain_execution = self.session.scalar(
                select(wf.CommandExecution.execution_id)
                .join(
                    wf.OperationExecutionFence,
                    wf.OperationExecutionFence.execution_id == wf.CommandExecution.execution_id,
                )
                .where(
                    wf.OperationExecutionFence.operation_id == operation.operation_id,
                    wf.CommandExecution.status == "uncertain",
                )
                .limit(1)
            )
            category: str | None = None
            reason: str | None = None
            if uncertain_execution is not None:
                category, reason = "unsafe", "an execution outcome is uncertain"
            elif abandonment is not None:
                if abandonment.state == "blocked":
                    category, reason = "unsafe", "abandonment is blocked at an unsupported frontier"
                elif abandonment.state == "published":
                    category, reason = "multi_step_safe", "an abandonment successor is waiting for continuation"
                else:
                    category, reason = "multi_step_safe", "abandonment has a deterministic continuation"
            elif operation.phase in {"held_evidence", "held_human"}:
                category, reason = "needs_marco", "the workflow is waiting for Marco"
            elif lease is not None and lease.expires_at <= now:
                category, reason = "multi_step_safe", "the actor lease has expired"
            elif lease is not None:
                healthy_count += 1
                continue
            else:
                healthy_count += 1
                continue
            counts[category] += 1
            head = self.session.get(models.TaskAuthorityHead, (generation.generation_id, operation.task_id))
            activation = self.session.get(models.ContentActivation, head.current_content_activation_id) if head else None
            version = self.session.get(models.ContentVersion, activation.content_version_id) if activation else None
            items.append({
                "category": category,
                "category_reason": reason,
                "task_id": str(operation.task_id),
                "task_title": version.title if version is not None else None,
                "operation_id": str(operation.operation_id),
                "phase": operation.phase,
                "lease_id": str(lease.lease_id) if lease is not None else None,
                "lease_expires_at": lease.expires_at.isoformat() if lease is not None else None,
                "abandonment_id": str(abandonment.abandonment_id) if abandonment is not None else None,
            })
        return {
            "checked_count": len(operations),
            "attention_count": len(items),
            "healthy_count": healthy_count,
            "category_counts": counts,
            "attention_items": items,
            "read_only": True,
        }

    def _holds(self) -> Mapping[str, Any]:
        generation = self.reads.active_generation()
        operations = self.session.scalars(
            select(wf.WorkflowOperation)
            .where(
                wf.WorkflowOperation.generation_id == generation.generation_id,
                wf.WorkflowOperation.lifecycle == "open",
                wf.WorkflowOperation.phase.in_(("held_evidence", "held_human")),
            )
            .order_by(wf.WorkflowOperation.created_at, wf.WorkflowOperation.operation_id)
        ).all()
        rows: list[dict[str, Any]] = []
        for operation in operations:
            task = self.session.get(models.DishTask, operation.task_id)
            head = self.session.get(
                models.TaskAuthorityHead,
                (generation.generation_id, operation.task_id),
            )
            activation = (
                self.session.get(
                    models.ContentActivation, head.current_content_activation_id
                )
                if head is not None
                else None
            )
            version = (
                self.session.get(models.ContentVersion, activation.content_version_id)
                if activation is not None
                else None
            )
            cycle = self.session.scalar(
                select(wf.VerificationCycle)
                .where(wf.VerificationCycle.operation_id == operation.operation_id)
                .order_by(wf.VerificationCycle.cycle_sequence.desc())
                .limit(1)
            )
            hold = self.session.scalar(
                select(wf.EvidenceHold)
                .where(
                    wf.EvidenceHold.operation_id == operation.operation_id,
                    wf.EvidenceHold.state == "open",
                )
                .order_by(wf.EvidenceHold.opened_at.desc())
                .limit(1)
            )
            requirement = self.session.scalar(
                select(wf.HumanReviewRequirement)
                .where(wf.HumanReviewRequirement.operation_id == operation.operation_id)
                .order_by(wf.HumanReviewRequirement.opened_at.desc())
                .limit(1)
            )
            if cycle is not None and cycle.outcome == "verification-hold":
                hold_class = "verification_two_pass"
                required_action = "resolved"
            elif operation.phase == "held_evidence":
                hold_class = (
                    "research_preconstruction_evidence"
                    if hold is not None and hold.cycle_id is None
                    else "verification_evidence"
                )
                required_action = "supply-evidence"
            else:
                hold_class = (
                    "research_preconstruction_human"
                    if requirement is not None and requirement.cycle_id is None
                    else "verification_human"
                )
                required_action = (
                    "reopen"
                    if requirement is not None and requirement.state == "decided"
                    else "record-human-decision"
                )
            status_detail = None
            if version is not None:
                try:
                    parts = parse_canonical_document(
                        title=version.title, body=version.body
                    )
                    status_detail = parts.document.state.values.get("Status detail")
                except CanonicalDocumentError:
                    status_detail = None
            rows.append(
                {
                    "hold_class": hold_class,
                    "required_admin_action": required_action,
                    "task_id": str(operation.task_id),
                    "task_title": version.title if version is not None else None,
                    "operation_id": str(operation.operation_id),
                    "cycle_id": str(cycle.cycle_id) if cycle is not None else None,
                    "hold_id": str(hold.hold_id) if hold is not None else None,
                    "requirement_id": (
                        str(requirement.requirement_id)
                        if requirement is not None
                        else None
                    ),
                    "hold_identity": (
                        version.content_identity if version is not None else None
                    ),
                    "question": status_detail,
                    "phase": operation.phase,
                    "created_at": operation.created_at.isoformat(),
                    "task_exists": task is not None,
                }
            )
        return {"holds": rows, "count": len(rows)}

    def _binding_for(self, generation: models.AuthorityGeneration) -> models.HonestContractBinding:
        try:
            contract = RegistryRepository(self.session).active_release_contract(
                generation.generation_id
            )
        except CoreAuthorityError as exc:
            raise CommandRuleError(
                "CONTRACT_BINDING_MISSING", str(exc)
            ) from exc
        if contract.generation.generation_id != generation.generation_id:
            raise CommandRuleError(
                "CONTRACT_BINDING_MISSING",
                "active registry contract does not belong to runtime generation",
            )
        return contract.honest_binding

    def _resolve_targets(
        self, call: CommandCall
    ) -> tuple[models.DishTask | None, wf.WorkflowOperation | None]:
        definition = definition_for(call.command_name)
        verification_start = (
            call.command_name == "start"
            and str(call.arguments.get("kind", "")) == "verification"
        )
        operation = None
        operation_ref = call.arguments.get("operation_id") or call.arguments.get("submission_id")
        if verification_start and operation_ref is not None:
            raise CommandRuleError(
                "ARGUMENT_UNEXPECTED",
                "Verification start accepts only the exact target_operation_id/target_cycle_id pair returned by Dish, or neither for an ordinary start",
                http_status=400,
                data={"field": "operation_id" if "operation_id" in call.arguments else "submission_id"},
            )
        if operation_ref:
            try:
                operation = self.session.get(wf.WorkflowOperation, uuid.UUID(str(operation_ref)))
            except ValueError as exc:
                raise CommandRuleError("INVALID_OPERATION_ID", "operation identifier must be a UUID", http_status=400) from exc
            if operation is None:
                raise CommandRuleError("OPERATION_NOT_FOUND", "unknown workflow operation", http_status=404)
        task = None
        task_ref = (
            call.arguments.get("dish_id")
            or call.arguments.get("task_id")
            or call.arguments.get("task_gid")
        )
        if not task_ref:
            dish_ref = call.arguments.get("dish")
            if dish_ref:
                task_ref = _task_reference_from_dish(str(dish_ref))
        if task_ref:
            try:
                task = self.reads.resolve_task(str(task_ref))
            except ReadModelError as exc:
                raise CommandRuleError("TASK_NOT_FOUND", str(exc), http_status=404) from exc
        elif operation is not None:
            task = self.session.get(models.DishTask, operation.task_id)
        if definition.task_required and task is None:
            raise CommandRuleError("TASK_REQUIRED", "command requires a task", http_status=400)
        if verification_start:
            target_operation = call.arguments.get("target_operation_id")
            target_cycle = call.arguments.get("target_cycle_id")
            if (target_operation is None) != (target_cycle is None):
                missing = (
                    "target_cycle_id"
                    if target_operation is not None
                    else "target_operation_id"
                )
                raise CommandRuleError(
                    "VERIFICATION_TARGET_PAIR_REQUIRED",
                    "Verification targets are only for an exact Dish-returned abandonment continuation; supply both returned values together, or omit both for an ordinary Verification start",
                    http_status=400,
                    data={"field": missing},
                )
            if target_operation is not None:
                try:
                    operation = self.session.get(
                        wf.WorkflowOperation, uuid.UUID(str(target_operation))
                    )
                except ValueError as exc:
                    raise CommandRuleError(
                        "INVALID_OPERATION_ID",
                        "target operation identifier must be a UUID",
                        http_status=400,
                    ) from exc
                try:
                    uuid.UUID(str(target_cycle))
                except ValueError as exc:
                    raise CommandRuleError(
                        "INVALID_CYCLE_ID",
                        "target cycle identifier must be a UUID",
                        http_status=400,
                    ) from exc
                if operation is None or operation.task_id != task.task_id:
                    raise CommandRuleError(
                        "VERIFICATION_TARGET_STALE",
                        "Verification start target no longer belongs to this task",
                    )
            else:
                active_generation_id = self.reads.active_generation().generation_id
                operations = list(
                    self.session.scalars(
                        select(wf.WorkflowOperation)
                        .where(
                            wf.WorkflowOperation.generation_id
                            == active_generation_id,
                            wf.WorkflowOperation.task_id == task.task_id,
                            wf.WorkflowOperation.lifecycle == "open",
                        )
                        .order_by(
                            wf.WorkflowOperation.created_at.desc(),
                            wf.WorkflowOperation.operation_id.desc(),
                        )
                        .limit(2)
                    )
                )
                if not operations:
                    raise CommandRuleError(
                        "OPEN_OPERATION_REQUIRED",
                        "task has no open Verification operation",
                    )
                if len(operations) != 1:
                    raise CommandRuleError(
                        "VERIFICATION_OPERATION_AMBIGUOUS",
                        "task does not have one unique open Verification operation",
                    )
                operation = operations[0]
        if definition.operation_required and operation is None:
            raise CommandRuleError(
                "OPERATION_ID_REQUIRED",
                "command requires the exact operation identifier",
                http_status=400,
            )
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
        hold_reject_cycle_exists = False
        hold_reject_evidence_hold_exists = False
        hold_reject_human_review_exists = False
        hold_reject_baseline_matches = False
        hold_reject_candidate_activation_exists = False
        hold_reject_author_owner_id = None
        hold_reject_author_run_id = None
        hold_reject_author_lease_id = None
        hold_reject_author_lease_expires_at = None
        hold_reject_registered_agent_matches = False
        if operation is not None:
            workflow_snapshot = self.reads._workflow_snapshot(
                generation_id=generation_id,
                task_id=task.task_id,
                title=view.title,
                body=view.body,
                operation=operation,
            )
            hold_reject_cycle_exists = self.session.scalar(
                select(wf.VerificationCycle.cycle_id)
                .where(wf.VerificationCycle.operation_id == operation.operation_id)
                .limit(1)
            ) is not None
            hold_reject_evidence_hold_exists = self.session.scalar(
                select(wf.EvidenceHold.hold_id)
                .where(wf.EvidenceHold.operation_id == operation.operation_id)
                .limit(1)
            ) is not None
            hold_reject_human_review_exists = self.session.scalar(
                select(wf.HumanReviewRequirement.requirement_id)
                .where(wf.HumanReviewRequirement.operation_id == operation.operation_id)
                .limit(1)
            ) is not None
            creation_fence = self.session.get(
                wf.TaskExecutionFence, operation.creation_execution_id
            )
            if creation_fence is not None:
                hold_reject_baseline_matches = bool(
                    view.task_revision == creation_fence.expected_task_revision
                    and view.membership_revision
                    == creation_fence.expected_membership_revision
                    and view.placement_revision
                    == creation_fence.expected_placement_revision
                    and view.completion_revision
                    == creation_fence.expected_completion_revision
                )
            hold_reject_candidate_activation_exists = self.session.scalar(
                select(models.ContentActivation.content_activation_id)
                .join(
                    wf.CommandExecution,
                    wf.CommandExecution.execution_id
                    == models.ContentActivation.command_execution_id,
                )
                .where(wf.CommandExecution.operation_id == operation.operation_id)
                .limit(1)
            ) is not None
            authors = list(
                self.session.scalars(
                    select(wf.OperationActorFact)
                    .where(
                        wf.OperationActorFact.operation_id == operation.operation_id,
                        wf.OperationActorFact.actor_role == "author",
                    )
                    .order_by(wf.OperationActorFact.recorded_at, wf.OperationActorFact.actor_fact_id)
                    .limit(2)
                )
            )
            if len(authors) == 1:
                author = authors[0]
                hold_reject_author_owner_id = author.owner_id
                hold_reject_author_run_id = str(author.run_id)
                run = self.session.get(wf.ServiceRun, author.run_id)
                hold_reject_registered_agent_matches = bool(
                    run is not None
                    and run.generation_id == generation_id
                    and run.status == "active"
                    and run.owner_id == author.owner_id
                    and run.agent == author.agent
                )
                leases = list(
                    self.session.scalars(
                        select(wf.ServiceLease)
                        .where(
                            wf.ServiceLease.operation_id == operation.operation_id,
                            wf.ServiceLease.task_id == task.task_id,
                            wf.ServiceLease.lease_kind == "actor",
                            wf.ServiceLease.actor_role == "author",
                            wf.ServiceLease.actor_attempt_sequence
                            == author.actor_attempt_sequence,
                            wf.ServiceLease.owner_id == author.owner_id,
                            wf.ServiceLease.run_id == author.run_id,
                            wf.ServiceLease.state == "active",
                        )
                        .order_by(wf.ServiceLease.issued_at.desc())
                        .limit(2)
                    )
                )
                if len(leases) == 1:
                    hold_reject_author_lease_id = str(leases[0].lease_id)
                    hold_reject_author_lease_expires_at = leases[0].expires_at
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
            hold_reject_cycle_exists=hold_reject_cycle_exists,
            hold_reject_evidence_hold_exists=hold_reject_evidence_hold_exists,
            hold_reject_human_review_exists=hold_reject_human_review_exists,
            hold_reject_baseline_matches=hold_reject_baseline_matches,
            hold_reject_candidate_activation_exists=hold_reject_candidate_activation_exists,
            hold_reject_author_owner_id=hold_reject_author_owner_id,
            hold_reject_author_run_id=hold_reject_author_run_id,
            hold_reject_author_lease_id=hold_reject_author_lease_id,
            hold_reject_author_lease_expires_at=hold_reject_author_lease_expires_at,
            hold_reject_registered_agent_matches=hold_reject_registered_agent_matches,
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

    def _validate_planning_intent_basis(
        self, call: CommandCall, *, initial: bool
    ) -> None:
        basis = call.arguments.get("intent_basis")
        reason = call.arguments.get("override_reason")
        if initial:
            if basis is not None or reason is not None:
                raise CommandRuleError(
                    "PLANNING_CONFIRMATION_NOT_YET_ISSUED",
                    "the first Planning request cannot supply intent confirmation fields",
                    http_status=400,
                )
            return
        if basis not in {"user_requested", "agent_override"}:
            raise CommandRuleError(
                "PLANNING_INTENT_BASIS_REQUIRED",
                "Planning confirmation requires user_requested or agent_override",
                http_status=400,
            )
        if basis == "agent_override" and not str(reason or "").strip():
            raise CommandRuleError(
                "PLANNING_OVERRIDE_REASON_REQUIRED",
                "agent_override requires a non-blank override_reason",
                http_status=400,
            )
        if basis == "user_requested" and reason is not None:
            raise CommandRuleError(
                "PLANNING_OVERRIDE_REASON_NOT_ALLOWED",
                "override_reason is not allowed with user_requested",
                http_status=400,
            )

    def _validate_planning_agent(
        self, *, generation_id: uuid.UUID, call: CommandCall
    ) -> str:
        agent = str(call.arguments.get("agent", "")).strip()
        if not agent:
            raise CommandRuleError(
                "AGENT_REQUIRED",
                "Planning start requires the exact registered agent",
                http_status=400,
            )
        run = self.workflow.repo.require_active_run(
            generation_id=generation_id, run_id=call.run_id, owner_id=call.owner_id
        )
        if run.agent != agent:
            raise CommandRuleError(
                "PLANNING_AGENT_MISMATCH",
                "Planning start agent does not match the registered service run",
            )
        return agent

    def _assert_planning_challenge_target(
        self, *, challenge: wf.PlanningIntentChallenge, call: CommandCall
    ) -> None:
        issuing = self.session.get(wf.ServiceRequest, challenge.issuing_request_id)
        if issuing is None:
            raise CommandRuleError(
                "PLANNING_CHALLENGE_EVIDENCE_MISSING",
                "the challenge issuing request is missing",
            )
        issued_arguments = dict(issuing.canonical_payload.get("arguments") or {})
        confirming_arguments = dict(call.arguments)
        for key in ("intent_challenge_id", "intent_basis", "override_reason"):
            confirming_arguments.pop(key, None)
        if issued_arguments != confirming_arguments:
            raise CommandRuleError(
                "PLANNING_CHALLENGE_MISMATCH",
                "Planning confirmation does not match the exact issued target",
                data={
                    "issuing_request_id": str(challenge.issuing_request_id),
                    "challenge_id": str(challenge.challenge_id),
                },
            )

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
            "hold-reject": self._hold_reject,
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
            "resolved": self._resolved,
            "authorize-governed-change": self._authorize,
            "revise-section-registry": self._revise_section_registry,
            "recover-lease": self._release_lease,
            "expire-lease": self._release_lease,
            "migrate": self._migrate,
            "planning-intent-settlement": self._settle_planning,
        }
        handler = handlers.get(call.command_name)
        if handler is None:
            raise CommandRuleError("COMMAND_NOT_PORTED", "retained command has no PostgreSQL handler")
        return handler(call, generation, binding, execution, task, operation)

    def _revise_section_registry(
        self, call, generation, binding, execution, _task, _operation
    ) -> dict[str, Any]:
        def required_section_id(name: str) -> uuid.UUID:
            raw = call.arguments.get(name)
            if raw in {None, ""}:
                raise CommandRuleError(
                    "REGISTRY_ROLE_SECTION_REQUIRED",
                    f"{name} is required",
                    http_status=400,
                    data={"field": name},
                )
            try:
                return uuid.UUID(str(raw))
            except ValueError as exc:
                raise CommandRuleError(
                    "INVALID_SECTION_ID",
                    f"{name} must be a UUID",
                    http_status=400,
                    data={"field": name},
                ) from exc

        research_section_id = required_section_id("research_queue_section_id")
        verification_section_id = required_section_id("verification_queue_section_id")
        if research_section_id == verification_section_id:
            raise CommandRuleError(
                "REGISTRY_ROLE_COLLISION",
                "Research Queue and Verification Queue must be different sections",
                http_status=400,
            )

        statement = select(models.ActiveSectionRegistry).where(
            models.ActiveSectionRegistry.generation_id == generation.generation_id
        )
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        active = self.session.scalar(statement.execution_options(populate_existing=True))
        if active is None:
            raise CommandRuleError("REGISTRY_MISSING", "generation has no active section registry")
        current_version = self.session.get(
            models.SectionRegistryVersion, active.registry_version_id
        )
        if current_version is None:
            raise CommandRuleError("REGISTRY_MISSING", "active registry version is missing")
        current_entries = list(
            self.session.scalars(
                select(models.SectionRegistryEntry)
                .where(
                    models.SectionRegistryEntry.registry_version_id
                    == current_version.registry_version_id
                )
                .order_by(models.SectionRegistryEntry.ordinal)
            )
        )
        by_section = {entry.section_id: entry for entry in current_entries}
        if research_section_id not in by_section or verification_section_id not in by_section:
            raise CommandRuleError(
                "REGISTRY_SECTION_MISSING",
                "workflow-role targets must already be present in the active registry",
                http_status=400,
            )
        requested = {
            "research_queue": research_section_id,
            "verification_queue": verification_section_id,
        }
        existing_special = {
            entry.workflow_role: entry.section_id
            for entry in current_entries
            if entry.workflow_role in requested
        }
        conflicts = {
            role: str(section_id)
            for role, section_id in existing_special.items()
            if requested[role] != section_id
        }
        if conflicts:
            raise CommandRuleError(
                "REGISTRY_ROLE_ALREADY_ASSIGNED",
                "active registry already assigns a special workflow role to another section",
                data={"conflicts": conflicts},
            )
        if all(existing_special.get(role) == section_id for role, section_id in requested.items()):
            return {
                "registry_version_id": str(current_version.registry_version_id),
                "registry_revision": active.registry_revision,
                "changed": False,
            }

        source_import_run = registry_source_import_run(self.session, current_version)
        registry_version_id = self.uuid_factory()
        correction_import_run_id = self.uuid_factory()
        activation_id = self.uuid_factory()
        next_registry_revision = active.registry_revision + 1
        revised_entries = [
            models.SectionRegistryEntry(
                registry_version_id=registry_version_id,
                section_id=entry.section_id,
                ordinal=entry.ordinal,
                display_name=entry.display_name,
                workflow_role=(
                    "research_queue"
                    if entry.section_id == research_section_id
                    else "verification_queue"
                    if entry.section_id == verification_section_id
                    else entry.workflow_role
                ),
            )
            for entry in current_entries
        ]
        sections = {
            section_id: self.session.get(models.GovernedSection, section_id)
            for section_id in by_section
        }
        project_ids = {
            section.project_id for section in sections.values() if section is not None
        }
        if any(section is None for section in sections.values()) or len(project_ids) != 1:
            raise CommandRuleError(
                "REGISTRY_AUTHORITY_INVALID",
                "active registry does not resolve one complete governed project",
            )
        project_id = next(iter(project_ids))
        project = self.session.get(models.GovernedProject, project_id)
        project_aliases = list(
            self.session.scalars(
                select(models.ProjectExternalAlias).where(
                    models.ProjectExternalAlias.project_id == project_id,
                    models.ProjectExternalAlias.external_system == "asana",
                    models.ProjectExternalAlias.state == "active",
                )
            )
        )
        if project is None or len(project_aliases) != 1:
            raise CommandRuleError(
                "REGISTRY_AUTHORITY_INVALID",
                "active registry project does not have one active Asana identity",
            )
        section_aliases: dict[uuid.UUID, str] = {}
        for section_id in by_section:
            aliases = list(
                self.session.scalars(
                    select(models.SectionExternalAlias).where(
                        models.SectionExternalAlias.section_id == section_id,
                        models.SectionExternalAlias.external_system == "asana",
                        models.SectionExternalAlias.state == "active",
                    )
                )
            )
            if len(aliases) != 1:
                raise CommandRuleError(
                    "REGISTRY_AUTHORITY_INVALID",
                    "registered section does not have one active Asana identity",
                    data={"section_id": str(section_id)},
                )
            section_aliases[section_id] = aliases[0].external_id
        registry_payload = {
            "format": "dish-section-registry-v1",
            "project": {
                "project_id": str(project.project_id),
                "logical_name": project.logical_name,
                "external_system": "asana",
                "external_id": project_aliases[0].external_id,
            },
            "sections": [
                {
                    "section_id": str(entry.section_id),
                    "logical_name": sections[entry.section_id].logical_name,
                    "display_name": entry.display_name,
                    "workflow_role": entry.workflow_role,
                    "ordinal": entry.ordinal,
                    "external_system": "asana",
                    "external_id": section_aliases[entry.section_id],
                }
                for entry in revised_entries
            ],
        }
        registry_sha256 = hashlib.sha256(
            json.dumps(
                registry_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        ).hexdigest()
        correction_payload = {
            "format": "dish-registry-role-correction-v1",
            "generation_id": str(generation.generation_id),
            "predecessor_registry_version_id": str(
                current_version.registry_version_id
            ),
            "source_import_run_id": str(source_import_run.import_run_id),
            "command_execution_id": str(execution.execution_id),
            "requested_roles": {
                role: str(section_id) for role, section_id in requested.items()
            },
            "result_registry_sha256": registry_sha256,
        }
        correction_bundle_sha256 = hashlib.sha256(
            json.dumps(
                correction_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        correction_high_water_mark = (
            "registry-role-correction:"
            f"{current_version.registry_version_id}:{registry_sha256}"
        )
        AuthorityRepository(self.session).add_import_run(
            models.ImportRun(
                import_run_id=correction_import_run_id,
                source_commit=source_import_run.source_commit,
                source_release=REGISTRY_ROLE_CORRECTION_SOURCE_RELEASE,
                legacy_generation_id=source_import_run.legacy_generation_id,
                baseline_high_water_mark=correction_high_water_mark,
                source_bundle_sha256=correction_bundle_sha256,
                status="complete",
                started_at=call.now,
                completed_at=call.now,
                provenance={
                    "correction_kind": REGISTRY_ROLE_CORRECTION_KIND,
                    "correction_bundle_sha256": correction_bundle_sha256,
                    "source_import_run_id": str(source_import_run.import_run_id),
                    "predecessor_registry_version_id": str(
                        current_version.registry_version_id
                    ),
                    "command_execution_id": str(execution.execution_id),
                    "requested_roles": {
                        role: str(section_id)
                        for role, section_id in requested.items()
                    },
                    "result_registry_sha256": registry_sha256,
                    "source_record_count": 0,
                },
            )
        )
        registry = RegistryRepository(self.session)
        registry.add_registry_version(
            models.SectionRegistryVersion(
                registry_version_id=registry_version_id,
                generation_id=generation.generation_id,
                version_number=current_version.version_number + 1,
                import_run_id=correction_import_run_id,
                contract_binding_id=current_version.contract_binding_id,
                registry_sha256=registry_sha256,
                created_at=call.now,
            ),
            revised_entries,
        )
        registry.activate_registry(
            activation=models.SectionRegistryActivation(
                registry_activation_id=activation_id,
                generation_id=generation.generation_id,
                registry_version_id=registry_version_id,
                activation_route="import",
                import_run_id=correction_import_run_id,
                command_execution_id=None,
                registry_revision=next_registry_revision,
                activated_at=call.now,
            ),
            current=models.ActiveSectionRegistry(
                generation_id=generation.generation_id,
                registry_version_id=registry_version_id,
                registry_activation_id=activation_id,
                registry_revision=next_registry_revision,
                updated_at=call.now,
            ),
        )
        return {
            "correction_import_run_id": str(correction_import_run_id),
            "registry_version_id": str(registry_version_id),
            "registry_activation_id": str(activation_id),
            "registry_revision": next_registry_revision,
            "changed": True,
        }

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
        return {
            "dish_id": str(task_id),
            "task_id": str(task_id),
            "content_version_id": str(version_id),
            "section_id": str(section.section_id),
            "projection_event_id": projection_id,
        }

    def _start(self, call, generation, binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None
        kind = str(call.arguments.get("kind", ""))
        phases = {
            "planning": "prepare_required",
            "initial": "prepare_required",
            "change": "prepare_required",
            "verification": "await_verification",
        }
        if kind not in phases:
            raise CommandRuleError(
                "INVALID_OPERATION_KIND", "unsupported operation kind", http_status=400
            )

        agent = str(call.arguments.get("agent", "")).strip()
        if not agent:
            raise CommandRuleError(
                "AGENT_REQUIRED", "start requires an exact agent", http_status=400
            )

        challenge_id = call.arguments.get("intent_challenge_id")
        if kind == "planning" and challenge_id:
            self._validate_planning_intent_basis(call, initial=False)
            self._validate_planning_agent(
                generation_id=generation.generation_id, call=call
            )
            try:
                challenge_uuid = uuid.UUID(str(challenge_id))
            except ValueError as exc:
                raise CommandRuleError(
                    "INVALID_CHALLENGE_ID",
                    "intent challenge identifier must be a UUID",
                    http_status=400,
                ) from exc
            challenge = self.session.get(wf.PlanningIntentChallenge, challenge_uuid)
            if (
                challenge is None
                or challenge.task_id != task.task_id
                or challenge.agent != agent
                or challenge.target_kind != kind
            ):
                raise CommandRuleError(
                    "PLANNING_CHALLENGE_MISMATCH",
                    "Planning confirmation does not match the issued task, agent, and target",
                )
            self._assert_planning_challenge_target(challenge=challenge, call=call)
            self.workflow.claim_planning_challenge(
                challenge_id=challenge_uuid,
                claiming_request_id=call.request_id,
                intent_basis=str(call.arguments.get("intent_basis", "")),
                override_reason=call.arguments.get("override_reason"),
            )

        if kind == "verification":
            if operation is None or operation.lifecycle != "open":
                raise CommandRuleError(
                    "OPEN_OPERATION_REQUIRED",
                    "Verification start requires the existing open operation",
                )
            if operation.phase != "await_verification":
                raise CommandRuleError(
                    "VERIFICATION_NOT_READY",
                    "the existing operation is not awaiting Verification",
                )
            cycle = self._latest_cycle(operation.operation_id)
            self._assert_cycle_is_current(generation.generation_id, task.task_id, cycle)
            target_cycle = call.arguments.get("target_cycle_id")
            if target_cycle is not None:
                try:
                    target_cycle_uuid = uuid.UUID(str(target_cycle))
                except ValueError as exc:
                    raise CommandRuleError(
                        "INVALID_CYCLE_ID",
                        "target cycle identifier must be a UUID",
                        http_status=400,
                    ) from exc
                if target_cycle_uuid != cycle.cycle_id:
                    raise CommandRuleError(
                        "VERIFICATION_CYCLE_MISMATCH",
                        "Verification start does not target the current cycle",
                    )
            attestation = str(
                call.arguments.get("independence_attestation", "")
            ).strip()
            if not attestation:
                raise CommandRuleError(
                    "INDEPENDENCE_ATTESTATION_REQUIRED",
                    "Verification start requires independence_attestation",
                    http_status=400,
                )
            conflicting = self.session.scalar(
                select(wf.OperationActorFact)
                .where(
                    wf.OperationActorFact.operation_id == operation.operation_id,
                    wf.OperationActorFact.actor_role != "verification",
                    (wf.OperationActorFact.run_id == call.run_id)
                    | (wf.OperationActorFact.agent == agent),
                )
                .limit(1)
            )
            if conflicting is not None:
                raise CommandRuleError(
                    "VERIFIER_NOT_INDEPENDENT",
                    "the author or material editor cannot verify this candidate",
                    data={"conflicting_actor_fact_id": str(conflicting.actor_fact_id)},
                )
            sequence = self._next_actor_attempt_sequence(task.task_id)
            actor_fact = self.workflow.create_actor_fact(
                actor_fact_id=self.uuid_factory(),
                execution_id=execution.execution_id,
                operation_id=operation.operation_id,
                run_id=call.run_id,
                owner_id=call.owner_id,
                actor_role="verification",
                agent=agent,
                actor_attempt_sequence=sequence,
                recorded_at=call.now,
            )
            lease = self.workflow.acquire_actor_lease(
                lease_id=self.uuid_factory(),
                execution_id=execution.execution_id,
                operation_id=operation.operation_id,
                run_id=call.run_id,
                owner_id=call.owner_id,
                actor_role="verification",
                actor_attempt_sequence=sequence,
                issued_at=call.now,
                expires_at=call.now + self.lease_duration,
                verification_cycle_id=cycle.cycle_id,
            )
            operation.persisted_actions = ["inspect"]
            operation.operation_revision += 1
            step_sequence = self._next_step(operation.operation_id)
            self.session.add(
                wf.OperationStep(
                    step_id=self.uuid_factory(),
                    operation_id=operation.operation_id,
                    step_name=f"verification-start-{step_sequence}",
                    step_sequence=step_sequence,
                    outcome="complete",
                    command_execution_id=execution.execution_id,
                    evidence={
                        "cycle_id": str(cycle.cycle_id),
                        "actor_fact_id": str(actor_fact.actor_fact_id),
                        "lease_id": str(lease.lease_id),
                        "independence_attestation": attestation,
                    },
                    occurred_at=call.now,
                )
            )
            return {
                "operation_id": str(operation.operation_id),
                "cycle_id": str(cycle.cycle_id),
                "lease_id": str(lease.lease_id),
                "phase": operation.phase,
            }

        operation_id = self.uuid_factory()
        operation = self.workflow.create_operation(
            operation_id=operation_id,
            execution_id=execution.execution_id,
            task_id=task.task_id,
            kind=kind,
            phase=phases[kind],
            persisted_actions=["prepare"],
            created_at=call.now,
        )
        sequence = self._next_actor_attempt_sequence(task.task_id)
        actor_fact = self.workflow.create_actor_fact(
            actor_fact_id=self.uuid_factory(),
            execution_id=execution.execution_id,
            operation_id=operation_id,
            run_id=call.run_id,
            owner_id=call.owner_id,
            actor_role="author",
            agent=agent,
            actor_attempt_sequence=sequence,
            recorded_at=call.now,
        )
        lease = self.workflow.acquire_actor_lease(
            lease_id=self.uuid_factory(),
            execution_id=execution.execution_id,
            operation_id=operation_id,
            run_id=call.run_id,
            owner_id=call.owner_id,
            actor_role=actor_fact.actor_role,
            actor_attempt_sequence=sequence,
            issued_at=call.now,
            expires_at=call.now + self.lease_duration,
        )
        if kind == "planning" and challenge_id:
            self.workflow.consume_planning_challenge(
                challenge_id=uuid.UUID(str(challenge_id)),
                operation_id=operation_id,
                consumed_at=call.now,
            )
        return {
            "operation_id": str(operation_id),
            "lease_id": str(lease.lease_id),
            "phase": operation.phase,
        }

    def _lock_operation_transition(
        self, operation_id: uuid.UUID
    ) -> wf.WorkflowOperation:
        statement = select(wf.WorkflowOperation).where(
            wf.WorkflowOperation.operation_id == operation_id
        )
        if self.session.get_bind().dialect.name == "postgresql":
            statement = statement.with_for_update()
        operation = self.session.scalar(
            statement.execution_options(populate_existing=True)
        )
        if operation is None:
            raise CommandRuleError(
                "OPEN_OPERATION_REQUIRED", "workflow operation no longer exists"
            )
        return operation

    def _prepare(self, call, generation, binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        if operation.lifecycle != "open":
            raise CommandRuleError(
                "OPEN_OPERATION_REQUIRED", "prepare requires an open operation"
            )
        self.workflow.repo.assert_task_fence(execution.execution_id)
        self.workflow.repo.assert_operation_fence(execution.execution_id)
        head = self.session.get(
            models.TaskAuthorityHead, (generation.generation_id, task.task_id)
        )
        prior_activation = self.session.get(
            models.ContentActivation, head.current_content_activation_id
        )
        prior = self.session.get(
            models.ContentVersion, prior_activation.content_version_id
        )
        file_text = call.arguments.get("file_text")
        body_value = call.arguments.get("body")
        if operation.kind == "initial" and file_text is not None:
            parts = prepared_document(
                str(file_text),
                agent=str(call.arguments.get("agent")),
                model=str(call.arguments.get("model")),
                at=call.now,
                protocol_release=binding.protocol_release,
            )
        else:
            parts = parse_canonical_document(
                file_text=str(file_text) if file_text is not None else None,
                title=str(call.arguments.get("title", prior.title)),
                body=str(body_value) if body_value is not None else None,
                expected_status="pending-verification",
            )
        version_id = self._activate_document(
            generation_id=generation.generation_id,
            task_id=task.task_id,
            binding_id=binding.binding_id,
            execution_id=execution.execution_id,
            title=parts.title,
            body=parts.body,
            predecessor_content_version_id=prior.content_version_id,
            at=call.now,
        )
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
        author_lease = self.session.scalar(
            select(wf.ServiceLease).where(
                wf.ServiceLease.operation_id == operation.operation_id,
                wf.ServiceLease.state == "active",
                wf.ServiceLease.actor_role == "author",
            )
        )
        if author_lease is not None:
            self._terminalize_lease(
                author_lease,
                "released",
                execution,
                call.now,
                "candidate prepared for Verification",
            )
        operation.phase = "await_verification"
        operation.persisted_actions = ["inspect"]
        operation.operation_revision += 1
        self.session.add(
            wf.OperationStep(
                step_id=self.uuid_factory(),
                operation_id=operation.operation_id,
                step_name=f"prepare-{operation.operation_revision}",
                step_sequence=self._next_step(operation.operation_id),
                outcome="complete",
                command_execution_id=execution.execution_id,
                evidence={
                    "content_version_id": str(version_id),
                    "section_id": str(verification_section_id),
                },
                occurred_at=call.now,
            )
        )
        cycle = self.workflow.open_verification_cycle(
            cycle_id=self.uuid_factory(),
            execution_id=execution.execution_id,
            operation_id=operation.operation_id,
            reviewed_content_version_id=version_id,
            created_at=call.now,
        )
        self.session.flush()
        projection_id = self._project(
            generation.generation_id,
            execution.execution_id,
            task.task_id,
            "update_task_document",
            {"content_version_id": str(version_id)},
            call.now,
        )
        placement_projection_id = self._project(
            generation.generation_id,
            execution.execution_id,
            task.task_id,
            "move_task",
            {"section_id": str(verification_section_id)},
            call.now,
        )
        return {
            "content_version_id": str(version_id),
            "cycle_id": str(cycle.cycle_id),
            "projection_event_id": projection_id,
            "placement_projection_event_id": placement_projection_id,
        }

    def _inspect(self, call, generation, _binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        cycle = self._latest_cycle(operation.operation_id)
        self._assert_cycle_is_current(generation.generation_id, task.task_id, cycle)
        agent = str(call.arguments.get("agent", "")).strip()
        attestation = str(
            call.arguments.get(
                "attestation", call.arguments.get("independence_attestation", "")
            )
        ).strip()
        if not agent or not attestation:
            raise CommandRuleError(
                "VERIFIER_IDENTITY_REQUIRED",
                "inspect requires the exact verifier agent and independence attestation",
                http_status=400,
            )

        actor = self.session.scalar(
            select(wf.OperationActorFact)
            .where(
                wf.OperationActorFact.operation_id == operation.operation_id,
                wf.OperationActorFact.run_id == call.run_id,
                wf.OperationActorFact.owner_id == call.owner_id,
                wf.OperationActorFact.actor_role == "verification",
                wf.OperationActorFact.agent == agent,
            )
            .order_by(wf.OperationActorFact.recorded_at.desc())
            .limit(1)
        )
        if actor is None:
            raise CommandRuleError(
                "VERIFICATION_START_REQUIRED",
                "inspect requires the exact verifier occurrence created by Verification start",
            )
        lease = self.session.scalar(
            select(wf.ServiceLease)
            .where(
                wf.ServiceLease.operation_id == operation.operation_id,
                wf.ServiceLease.run_id == call.run_id,
                wf.ServiceLease.owner_id == call.owner_id,
                wf.ServiceLease.actor_role == "verification",
                wf.ServiceLease.actor_attempt_sequence
                == actor.actor_attempt_sequence,
                wf.ServiceLease.verification_cycle_id == cycle.cycle_id,
                wf.ServiceLease.state == "active",
            )
            .order_by(wf.ServiceLease.issued_at.desc())
            .limit(1)
        )
        lease_expiry = lease.expires_at if lease is not None else None
        if (
            lease_expiry is not None
            and lease_expiry.tzinfo is None
            and call.now.tzinfo is not None
        ):
            lease_expiry = lease_expiry.replace(tzinfo=call.now.tzinfo)
        if lease is None or lease_expiry is None or lease_expiry <= call.now:
            raise CommandRuleError(
                "VERIFICATION_LEASE_REQUIRED",
                "inspect requires the exact active Verification lease for this cycle",
            )

        start_attestation = None
        steps = self.session.scalars(
            select(wf.OperationStep)
            .where(wf.OperationStep.operation_id == operation.operation_id)
            .order_by(wf.OperationStep.step_sequence.desc())
        ).all()
        for step in steps:
            evidence = dict(step.evidence or {})
            if (
                evidence.get("cycle_id") == str(cycle.cycle_id)
                and evidence.get("actor_fact_id") == str(actor.actor_fact_id)
                and evidence.get("lease_id") == str(lease.lease_id)
            ):
                start_attestation = str(
                    evidence.get("independence_attestation") or ""
                ).strip()
                break
        if not start_attestation or attestation != start_attestation:
            raise CommandRuleError(
                "VERIFIER_ATTESTATION_MISMATCH",
                "inspect must repeat the exact attestation bound by Verification start",
            )

        inspection = self.workflow.record_inspection(
            inspection_id=self.uuid_factory(),
            execution_id=execution.execution_id,
            cycle_id=cycle.cycle_id,
            actor_fact_id=actor.actor_fact_id,
            verifier_run_id=call.run_id,
            attestation=attestation,
            inspected_at=call.now,
        )
        operation.persisted_actions = ["approve", "reject"]
        operation.operation_revision += 1
        return {
            "inspection_id": str(inspection.inspection_id),
            "cycle_id": str(cycle.cycle_id),
            "lease_id": str(lease.lease_id),
        }

    def _approve(self, call, generation, binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        cycle = self._latest_cycle(operation.operation_id)
        self._assert_cycle_is_current(generation.generation_id, task.task_id, cycle)
        inspection, actor = self._exact_verifier_inspection(call, cycle)

        agent = str(call.arguments.get("agent", "")).strip()
        model = str(call.arguments.get("model", "")).strip()
        correction = call.arguments.get("correction")
        reviewed_identity = call.arguments.get("reviewed_identity")
        if not agent or not model:
            raise CommandRuleError(
                "VERIFIER_MODEL_REQUIRED",
                "approve requires the exact verifier agent and self-reported model",
                http_status=400,
            )
        if correction not in {"none", "small"}:
            raise CommandRuleError(
                "INVALID_CORRECTION_CLASS",
                "correction is required and must be none or small",
                http_status=400,
            )
        if reviewed_identity is None:
            raise CommandRuleError(
                "REVIEWED_IDENTITY_REQUIRED",
                "approve requires the exact inspected content identity",
                http_status=400,
            )
        if call.arguments.get("semantic_review_complete") is not True:
            raise CommandRuleError(
                "SEMANTIC_REVIEW_REQUIRED",
                "semantic_review_complete must be true",
                http_status=400,
            )
        if call.arguments.get("provenance_complete") is not True:
            raise CommandRuleError(
                "PROVENANCE_REVIEW_REQUIRED",
                "provenance_complete must be true",
                http_status=400,
            )

        reviewed = self.session.get(models.ContentVersion, cycle.reviewed_content_version_id)
        if reviewed is None:
            raise CommandRuleError(
                "REVIEWED_CONTENT_MISSING", "reviewed content version is missing"
            )
        if str(reviewed_identity) != reviewed.content_identity:
            raise CommandRuleError(
                "REVIEWED_IDENTITY_MISMATCH",
                "the supplied reviewed identity does not match the inspected occurrence",
            )
        reviewed_parts = parse_canonical_document(
            title=reviewed.title,
            body=reviewed.body,
            expected_status="pending-verification",
        )

        source_parts = reviewed_parts
        if correction == "small":
            file_text = call.arguments.get("file_text")
            if file_text is None:
                raise CommandRuleError(
                    "SMALL_CORRECTION_CONTENT_REQUIRED",
                    "small correction requires a complete canonical file_text",
                    http_status=400,
                )
            source_parts = parse_canonical_document(
                file_text=str(file_text), expected_status="pending-verification"
            )
            candidate_identity = hashlib.sha256(
                (source_parts.title + "\0" + source_parts.body).encode()
            ).hexdigest()
            if candidate_identity == reviewed.content_identity:
                raise CommandRuleError(
                    "SMALL_CORRECTION_REQUIRED",
                    "small correction must create a distinct canonical candidate",
                )

        signed_parts = ready_document(
            source_parts.document,
            agent=agent,
            model=model,
            at=call.now,
        )
        signed_version_id = self._activate_document(
            generation_id=generation.generation_id,
            task_id=task.task_id,
            binding_id=binding.binding_id,
            execution_id=execution.execution_id,
            title=signed_parts.title,
            body=signed_parts.body,
            predecessor_content_version_id=reviewed.content_version_id,
            at=call.now,
        )
        if correction == "small":
            self.session.add(
                wf.VerificationCorrection(
                    correction_id=self.uuid_factory(),
                    cycle_id=cycle.cycle_id,
                    source_content_version_id=cycle.reviewed_content_version_id,
                    corrected_content_version_id=signed_version_id,
                    correction_class="small",
                    reason=str(
                        call.arguments.get("reason", "exact Small correction")
                    ),
                    command_execution_id=execution.execution_id,
                    recorded_at=call.now,
                )
            )
            self.session.flush()

        signoff = self.workflow.signoff_verification(
            signoff_id=self.uuid_factory(),
            execution_id=execution.execution_id,
            cycle_id=cycle.cycle_id,
            inspection_id=inspection.inspection_id,
            signed_content_version_id=signed_version_id,
            signoff_kind="direct",
            signed_at=call.now,
        )
        self._release_verifier_lease(
            call=call, execution=execution, cycle=cycle, actor=actor, reason="verification approved"
        )
        operation.phase = "await_submission"
        operation.persisted_actions = ["submit"]
        operation.operation_revision += 1
        projection_id = self._project(
            generation.generation_id,
            execution.execution_id,
            task.task_id,
            "update_task_document",
            {"content_version_id": str(signed_version_id)},
            call.now,
        )
        return {
            "signoff_id": str(signoff.signoff_id),
            "cycle_id": str(cycle.cycle_id),
            "signed_content_version_id": str(signed_version_id),
            "correction": correction,
            "projection_event_id": projection_id,
        }

    def _hold_reject(self, call, generation, _binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        route = str(call.arguments.get("route", "")).replace("_", "-").strip()
        if route != "evidence":
            raise CommandRuleError(
                "INVALID_REJECTION_ROUTE",
                "hold-reject supports only the pre-construction evidence route",
                http_status=400,
            )
        reason = str(call.arguments.get("reason", "")).strip()
        if not reason:
            raise CommandRuleError(
                "REJECTION_REASON_REQUIRED",
                "hold-reject requires a non-blank reason",
                http_status=400,
            )
        resume_status = str(call.arguments.get("resume_status", "")).strip()
        if resume_status != "pending-research":
            raise CommandRuleError(
                "INVALID_RESUME_STATUS",
                "pre-construction Evidence holds must resume to pending-research",
                http_status=400,
            )
        forbidden = sorted(
            key
            for key in (
                "file_text",
                "file_path",
                "model",
                "independence_attestation",
                "reviewed_identity",
                "correction",
            )
            if call.arguments.get(key) not in {None, ""}
        )
        if forbidden:
            raise CommandRuleError(
                "PRECONSTRUCTION_CANDIDATE_UNEXPECTED",
                "hold-reject cannot carry candidate or Verification-only fields",
                http_status=400,
                data={"unexpected": forbidden},
            )
        self.workflow.repo.assert_task_fence(execution.execution_id)
        self.workflow.repo.assert_operation_fence(execution.execution_id)
        baseline_content_version_id = self._current_content_version_id(
            generation.generation_id, task.task_id
        )
        hold = self.workflow.open_evidence_hold(
            hold_id=self.uuid_factory(),
            execution_id=execution.execution_id,
            operation_id=operation.operation_id,
            baseline_content_version_id=baseline_content_version_id,
            reason=reason,
            opened_at=call.now,
            cycle_id=None,
        )
        operation.phase = "held_evidence"
        operation.persisted_actions = ["supply-evidence"]
        operation.operation_revision += 1
        return {
            "operation_id": str(operation.operation_id),
            "route": "evidence",
            "resume_status": "pending-research",
            "hold_id": str(hold.hold_id),
            "baseline_content_version_id": str(baseline_content_version_id),
            "cycle_id": None,
        }

    def _reject(self, call, generation, binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        cycle = self._latest_cycle(operation.operation_id)
        self._assert_cycle_is_current(generation.generation_id, task.task_id, cycle)
        _inspection, actor = self._exact_verifier_inspection(call, cycle)
        route = str(call.arguments.get("route", "large")).replace("_", "-")
        if route not in {"large", "evidence", "human-review"}:
            raise CommandRuleError(
                "INVALID_REJECTION_ROUTE",
                "route must be large, evidence, or human-review",
                http_status=400,
            )
        reason = str(call.arguments.get("reason", "")).strip()
        if not reason:
            raise CommandRuleError(
                "REJECTION_REASON_REQUIRED",
                "reject requires a non-blank reason",
                http_status=400,
            )
        reviewed = self.session.get(models.ContentVersion, cycle.reviewed_content_version_id)
        if reviewed is None:
            raise CommandRuleError(
                "REVIEWED_CONTENT_MISSING", "reviewed content version is missing"
            )
        reviewed_parts = parse_canonical_document(
            title=reviewed.title,
            body=reviewed.body,
            expected_status="pending-verification",
        )

        prior_nonapproved_cycles = int(
            self.session.scalar(
                select(func.count())
                .select_from(wf.VerificationCycle)
                .where(
                    wf.VerificationCycle.operation_id == operation.operation_id,
                    wf.VerificationCycle.cycle_id != cycle.cycle_id,
                    wf.VerificationCycle.lifecycle != "open",
                    wf.VerificationCycle.outcome != "approved",
                )
            )
            or 0
        )
        verification_hold = route == "large" and prior_nonapproved_cycles + 1 >= 3
        cycle.lifecycle = "rejected"
        cycle.outcome = "verification-hold" if verification_hold else "rejected"
        cycle.terminal_at = call.now
        if route == "large":
            file_text = call.arguments.get("file_text")
            if file_text is None:
                raise CommandRuleError(
                    "LARGE_CORRECTION_CONTENT_REQUIRED",
                    "large rejection requires a complete canonical file_text",
                    http_status=400,
                )
            corrected = parse_canonical_document(
                file_text=str(file_text), expected_status="pending-verification"
            )
            identity = hashlib.sha256(
                (corrected.title + "\0" + corrected.body).encode()
            ).hexdigest()
            if identity == reviewed.content_identity:
                raise CommandRuleError(
                    "LARGE_CORRECTION_REQUIRED",
                    "large rejection must create a distinct canonical candidate",
                )
            target_parts = corrected
            if verification_hold:
                target_parts = held_document(
                    corrected.document,
                    target="pending-human-review",
                    detail=(
                        "Three consecutive Verification rounds ended without a "
                        f"signable task: {reason}"
                    ),
                )
            version_id = self._activate_document(
                generation_id=generation.generation_id,
                task_id=task.task_id,
                binding_id=binding.binding_id,
                execution_id=execution.execution_id,
                title=target_parts.title,
                body=target_parts.body,
                predecessor_content_version_id=reviewed.content_version_id,
                at=call.now,
            )
            self.session.add(
                wf.VerificationCorrection(
                    correction_id=self.uuid_factory(),
                    cycle_id=cycle.cycle_id,
                    source_content_version_id=cycle.reviewed_content_version_id,
                    corrected_content_version_id=version_id,
                    correction_class="large",
                    reason=reason,
                    command_execution_id=execution.execution_id,
                    recorded_at=call.now,
                )
            )
            self.session.flush()
            next_cycle = None
            if verification_hold:
                operation.phase = "held_human"
                operation.persisted_actions = ["resolved", "reopen"]
            else:
                next_cycle = self.workflow.open_verification_cycle(
                    cycle_id=self.uuid_factory(),
                    execution_id=execution.execution_id,
                    operation_id=operation.operation_id,
                    reviewed_content_version_id=version_id,
                    created_at=call.now,
                )
                operation.phase = "await_verification"
                operation.persisted_actions = ["inspect"]
            projection_id = self._project(
                generation.generation_id,
                execution.execution_id,
                task.task_id,
                "update_task_document",
                {"content_version_id": str(version_id)},
                call.now,
            )
            result = {
                "route": "large",
                "corrected_content_version_id": str(version_id),
                "new_cycle_id": str(next_cycle.cycle_id) if next_cycle else None,
                "verification_hold": verification_hold,
                "projection_event_id": projection_id,
            }
        else:
            target_status = (
                "pending-evidence" if route == "evidence" else "pending-human-review"
            )
            held_parts = held_document(
                reviewed_parts.document, target=target_status, detail=reason
            )
            held_version_id = self._activate_document(
                generation_id=generation.generation_id,
                task_id=task.task_id,
                binding_id=binding.binding_id,
                execution_id=execution.execution_id,
                title=held_parts.title,
                body=held_parts.body,
                predecessor_content_version_id=reviewed.content_version_id,
                at=call.now,
            )
            projection_id = self._project(
                generation.generation_id,
                execution.execution_id,
                task.task_id,
                "update_task_document",
                {"content_version_id": str(held_version_id)},
                call.now,
            )
            if route == "evidence":
                hold = self.workflow.open_evidence_hold(
                    hold_id=self.uuid_factory(),
                    execution_id=execution.execution_id,
                    operation_id=operation.operation_id,
                    baseline_content_version_id=held_version_id,
                    reason=reason,
                    opened_at=call.now,
                    cycle_id=cycle.cycle_id,
                )
                operation.phase = "held_evidence"
                operation.persisted_actions = ["supply-evidence"]
                result = {
                    "route": "evidence",
                    "hold_id": str(hold.hold_id),
                    "held_content_version_id": str(held_version_id),
                    "projection_event_id": projection_id,
                }
            else:
                requirement = self.workflow.open_human_review(
                    requirement_id=self.uuid_factory(),
                    execution_id=execution.execution_id,
                    operation_id=operation.operation_id,
                    route="human_review",
                    question=reason,
                    baseline_content_version_id=held_version_id,
                    opened_at=call.now,
                    cycle_id=cycle.cycle_id,
                )
                operation.phase = "held_human"
                operation.persisted_actions = ["record-human-decision"]
                result = {
                    "route": "human-review",
                    "requirement_id": str(requirement.requirement_id),
                    "held_content_version_id": str(held_version_id),
                    "projection_event_id": projection_id,
                }
        result["cycle_id"] = str(cycle.cycle_id)
        self._release_verifier_lease(
            call=call, execution=execution, cycle=cycle, actor=actor, reason=f"verification rejected: {route}"
        )
        operation.operation_revision += 1
        return result

    def _submit(self, call, generation, _binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        forbidden = {
            key: call.arguments[key]
            for key in ("destination_section_id", "destination_section_gid")
            if key in call.arguments
        }
        if forbidden:
            raise CommandRuleError(
                "UNEXPECTED_DESTINATION_ARGUMENT",
                "submit destination is derived exclusively from the signed document",
                http_status=400,
                data={"unexpected": sorted(forbidden)},
            )
        cycle = self._latest_cycle(operation.operation_id)
        signoff = self.session.scalar(
            select(wf.VerificationSignoff).where(
                wf.VerificationSignoff.cycle_id == cycle.cycle_id
            )
        )
        head = self.session.get(
            models.TaskAuthorityHead, (generation.generation_id, task.task_id)
        )
        activation = (
            self.session.get(models.ContentActivation, head.current_content_activation_id)
            if head
            else None
        )
        if (
            cycle.lifecycle != "approved"
            or signoff is None
            or activation is None
            or signoff.signed_content_version_id != activation.content_version_id
        ):
            raise CommandRuleError(
                "SIGNED_STATE_REQUIRED",
                "submit requires the exact approved current content occurrence",
            )
        inspection = self.session.get(
            wf.VerificationInspectionOccurrence, signoff.inspection_id
        )
        if (
            inspection is None
            or inspection.cycle_id != cycle.cycle_id
            or signoff.verifier_actor_fact_id != inspection.verifier_actor_fact_id
        ):
            raise CommandRuleError(
                "SIGNOFF_LINEAGE_INVALID", "submit signoff lineage is incomplete"
            )
        signed = self.session.get(models.ContentVersion, activation.content_version_id)
        if signed is None:
            raise CommandRuleError(
                "SIGNED_CONTENT_MISSING", "signed content occurrence is missing"
            )
        signed_parts = parse_canonical_document(
            title=signed.title, body=signed.body, expected_status="ready"
        )
        section = self.reads.resolve_section(destination_gid(signed_parts.document))
        self._set_placement(
            generation.generation_id,
            task.task_id,
            section.section_id,
            execution.execution_id,
            call.now,
        )
        operation.lifecycle = "completed"
        operation.phase = "completed"
        operation.terminal_outcome = "submitted"
        operation.terminal_at = call.now
        operation.operation_revision += 1
        projection_id = self._project(
            generation.generation_id,
            execution.execution_id,
            task.task_id,
            "move_task",
            {
                "destination_section_id": str(section.section_id),
                "signed_content_version_id": str(signed.content_version_id),
            },
            call.now,
        )
        return {
            "operation_id": str(operation.operation_id),
            "destination_section_id": str(section.section_id),
            "signed_content_version_id": str(signed.content_version_id),
            "projection_event_id": projection_id,
        }

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
        self.workflow.repo.assert_task_fence(execution.execution_id)
        self.workflow.repo.assert_operation_fence(execution.execution_id)
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

    def _reopen(self, call, generation, binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        if operation.phase != "held_human":
            raise CommandRuleError(
                "HUMAN_REVIEW_HOLD_REQUIRED",
                "reopen requires the exact Human Review hold",
            )
        cycle = self._latest_cycle(operation.operation_id)
        if cycle.outcome == "verification-hold":
            self._assert_exact_verification_hold_target(
                call=call, generation_id=generation.generation_id, task_id=task.task_id, cycle=cycle
            )
            return self._reopen_verification_hold(
                call=call,
                generation=generation,
                binding=binding,
                execution=execution,
                task=task,
                operation=operation,
                cycle=cycle,
            )
        requirement_id = call.arguments.get("requirement_id")
        if not requirement_id:
            raise CommandRuleError(
                "REQUIREMENT_ID_REQUIRED",
                "reopen requires requirement_id",
                http_status=400,
            )
        try:
            requirement_uuid = uuid.UUID(str(requirement_id))
        except ValueError as exc:
            raise CommandRuleError(
                "INVALID_REQUIREMENT_ID",
                "requirement identifier must be a UUID",
                http_status=400,
            ) from exc
        requirement = self.session.get(wf.HumanReviewRequirement, requirement_uuid)
        decision = (
            self.session.scalar(
                select(wf.HumanReviewDecision).where(
                    wf.HumanReviewDecision.requirement_id == requirement.requirement_id
                )
            )
            if requirement
            else None
        )
        if (
            requirement is None
            or requirement.operation_id != operation.operation_id
            or requirement.state != "decided"
            or decision is None
        ):
            raise CommandRuleError(
                "DECIDED_HUMAN_REVIEW_REQUIRED",
                "the exact Human Review requirement is not decided",
            )
        if requirement.cycle_id is None:
            raise CommandRuleError(
                "VERIFICATION_HOLD_REQUIRED",
                "reopen currently requires a Verification-cycle Human Review hold",
            )
        result = self._resume_held_document(
            generation_id=generation.generation_id,
            task_id=task.task_id,
            operation=operation,
            execution=execution,
            binding_id=binding.binding_id,
            baseline_content_version_id=requirement.baseline_content_version_id,
            expected_status="pending-human-review",
            decision_line=(
                f"Human — Marco: Human Review resolved for cycle "
                f"{requirement.cycle_id} — {decision.decision}"
            ),
            at=call.now,
        )
        result.update(
            {
                "requirement_id": str(requirement.requirement_id),
                "reopened_from": str(requirement.cycle_id),
            }
        )
        return result

    def _reopen_verification_hold(
        self,
        *,
        call: CommandCall,
        generation: models.AuthorityGeneration,
        binding: models.HonestContractBinding,
        execution: wf.CommandExecution,
        task: models.DishTask,
        operation: wf.WorkflowOperation,
        cycle: wf.VerificationCycle,
    ) -> dict[str, Any]:
        file_text = call.arguments.get("file_text")
        category = str(call.arguments.get("category", "")).strip()
        before = str(call.arguments.get("before", "")).strip()
        after = str(call.arguments.get("after", "")).strip()
        editor = str(call.arguments.get("editor", "")).strip()
        model = str(call.arguments.get("model", "")).strip()
        if (
            file_text is None
            or category not in {"evidence", "premise", "method", "scope"}
            or not before
            or not after
            or before == after
            or editor not in {"claude", "gpt", "codex"}
            or not model
        ):
            raise CommandRuleError(
                "SUBSTANTIVE_RESET_REQUIRED",
                "reopen requires a canonical corrected candidate and exact substantive reset proof",
                http_status=400,
            )
        baseline_id = self._current_content_version_id(
            generation.generation_id, task.task_id
        )
        baseline = self.session.get(models.ContentVersion, baseline_id)
        if baseline is None:
            raise CommandRuleError(
                "HOLD_BASELINE_MISSING", "Verification hold content is missing"
            )
        parse_canonical_document(
            title=baseline.title,
            body=baseline.body,
            expected_status="pending-human-review",
        )
        candidate = parse_canonical_document(
            file_text=str(file_text), expected_status="pending-verification"
        )
        baseline_text = f"{baseline.title}\n{baseline.body}"
        candidate_text = f"{candidate.title}\n{candidate.body}"
        if before not in baseline_text or after not in candidate_text:
            raise CommandRuleError(
                "SUBSTANTIVE_RESET_NOT_PROVED",
                "before must occur in the held document and after in the corrected candidate",
            )
        version_id = self._activate_document(
            generation_id=generation.generation_id,
            task_id=task.task_id,
            binding_id=binding.binding_id,
            execution_id=execution.execution_id,
            title=candidate.title,
            body=candidate.body,
            predecessor_content_version_id=baseline_id,
            at=call.now,
        )
        next_cycle = self.workflow.open_verification_cycle(
            cycle_id=self.uuid_factory(),
            execution_id=execution.execution_id,
            operation_id=operation.operation_id,
            reviewed_content_version_id=version_id,
            created_at=call.now,
        )
        operation.phase = "await_verification"
        operation.persisted_actions = ["inspect"]
        operation.operation_revision += 1
        projection_id = self._project(
            generation.generation_id,
            execution.execution_id,
            task.task_id,
            "update_task_document",
            {"content_version_id": str(version_id)},
            call.now,
        )
        return {
            "source_cycle_id": str(cycle.cycle_id),
            "new_cycle_id": str(next_cycle.cycle_id),
            "corrected_content_version_id": str(version_id),
            "category": category,
            "editor": editor,
            "model": model,
            "projection_event_id": projection_id,
        }

    def _supply_evidence(self, call, generation, binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        hold_id = call.arguments.get("hold_id")
        if hold_id not in {None, ""}:
            try:
                hold_uuid = uuid.UUID(str(hold_id))
            except ValueError as exc:
                raise CommandRuleError(
                    "INVALID_HOLD_ID",
                    "hold identifier must be a UUID",
                    http_status=400,
                ) from exc
            hold = self.session.get(wf.EvidenceHold, hold_uuid)
        else:
            holds = list(
                self.session.scalars(
                    select(wf.EvidenceHold)
                    .where(
                        wf.EvidenceHold.operation_id == operation.operation_id,
                        wf.EvidenceHold.state == "open",
                    )
                    .order_by(wf.EvidenceHold.opened_at.desc())
                    .limit(2)
                )
            )
            hold = holds[0] if len(holds) == 1 else None
        if (
            hold is None
            or hold.operation_id != operation.operation_id
            or hold.state != "open"
            or operation.phase != "held_evidence"
        ):
            raise CommandRuleError(
                "OPEN_EVIDENCE_HOLD_REQUIRED", "the exact Evidence hold is not open"
            )

        detail = str(call.arguments.get("detail", "")).strip()
        evidence = call.arguments.get("evidence")
        if not detail and isinstance(evidence, Mapping) and evidence:
            detail = str(evidence.get("detail") or evidence.get("finding") or "").strip()
        if not detail:
            raise CommandRuleError(
                "EVIDENCE_REQUIRED",
                "supply-evidence requires a non-blank detail or evidence finding",
                http_status=400,
            )
        if detail.startswith("<") and detail.endswith(">"):
            raise CommandRuleError(
                "EVIDENCE_PLACEHOLDER",
                "evidence detail still contains the unfilled command placeholder",
                http_status=400,
            )
        resume_status = str(
            call.arguments.get("resume_status", "pending-verification")
        ).strip()
        if resume_status not in {"pending-research", "pending-verification"}:
            raise CommandRuleError(
                "INVALID_RESUME_STATUS",
                "resume_status must be pending-research or pending-verification",
                http_status=400,
            )
        candidate_file_text = call.arguments.get("file_text")
        editor = call.arguments.get("editor")
        model = call.arguments.get("model")
        preconstruction_hold = hold.cycle_id is None
        if preconstruction_hold:
            if resume_status != "pending-research":
                raise CommandRuleError(
                    "INVALID_RESUME_STATUS",
                    "pre-construction Evidence holds must resume to pending-research",
                    http_status=400,
                )
            forbidden = sorted(
                key
                for key in ("file_text", "file_path", "editor", "model")
                if call.arguments.get(key) not in {None, ""}
            )
            if forbidden:
                raise CommandRuleError(
                    "PRECONSTRUCTION_CANDIDATE_UNEXPECTED",
                    "pre-construction Evidence hold resolution cannot install candidate content",
                    http_status=400,
                    data={"unexpected": forbidden},
                )
        else:
            if call.arguments.get("file_path") and candidate_file_text is None:
                raise CommandRuleError(
                    "MATERIAL_CONTENT_REQUIRED",
                    "shadow-safe hold resolution requires complete canonical file_text, not a filesystem path",
                    http_status=400,
                )
            if candidate_file_text is not None and (
                editor not in {"claude", "gpt", "codex"}
                or not str(model or "").strip()
            ):
                raise CommandRuleError(
                    "MATERIAL_EDITOR_REQUIRED",
                    "material hold resolution requires editor and model",
                    http_status=400,
                )
        self._assert_hold_resolution_target(
            call=call,
            generation_id=generation.generation_id,
            task=task,
            baseline_content_version_id=hold.baseline_content_version_id,
            cycle_id=hold.cycle_id,
        )
        evidence_payload = (
            dict(evidence) if isinstance(evidence, Mapping) and evidence else {"detail": detail}
        )
        evidence_payload.update(
            {
                "detail": detail,
                "resume_status": resume_status,
                "material": candidate_file_text is not None and not preconstruction_hold,
            }
        )
        if preconstruction_hold:
            baseline = self.session.get(
                models.ContentVersion, hold.baseline_content_version_id
            )
            if baseline is None:
                raise CommandRuleError(
                    "HOLD_BASELINE_MISSING",
                    "pre-construction Evidence hold baseline is missing",
                )
            parse_canonical_document(
                title=baseline.title,
                body=baseline.body,
                expected_status="pending-research",
            )
            self.workflow.repo.assert_task_fence(execution.execution_id)
            self.workflow.repo.assert_operation_fence(execution.execution_id)
            self.workflow.supply_evidence(
                hold_id=hold.hold_id,
                execution_id=execution.execution_id,
                evidence_payload=evidence_payload,
                supplied_at=call.now,
            )
            operation.phase = "prepare_required"
            operation.persisted_actions = ["prepare"]
            operation.operation_revision += 1
            return {
                "hold_id": str(hold.hold_id),
                "state": hold.state,
                "resume_status": "pending-research",
                "baseline_content_version_id": str(hold.baseline_content_version_id),
                "cycle_id": None,
                "projection_event_id": None,
                "phase": "prepare_required",
                "_preconstruction_hold": True,
            }
        self.workflow.supply_evidence(
            hold_id=hold.hold_id,
            execution_id=execution.execution_id,
            evidence_payload=evidence_payload,
            supplied_at=call.now,
        )
        result = self._resume_held_document(
            generation_id=generation.generation_id,
            task_id=task.task_id,
            operation=operation,
            execution=execution,
            binding_id=binding.binding_id,
            baseline_content_version_id=hold.baseline_content_version_id,
            expected_status="pending-evidence",
            decision_line=f"Human — Marco: evidence resolved — {detail}",
            at=call.now,
            resume_status=resume_status,
            candidate_file_text=(
                str(candidate_file_text) if candidate_file_text is not None else None
            ),
            editor=str(editor) if editor is not None else None,
            model=str(model) if model is not None else None,
            terminal_outcome_prefix="evidence",
        )
        result.update({"hold_id": str(hold.hold_id), "state": hold.state})
        return result

    def _record_human_decision(self, call, generation, binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        requirement_id = call.arguments.get("requirement_id")
        if requirement_id not in {None, ""}:
            try:
                requirement_uuid = uuid.UUID(str(requirement_id))
            except ValueError as exc:
                raise CommandRuleError(
                    "INVALID_REQUIREMENT_ID",
                    "requirement identifier must be a UUID",
                    http_status=400,
                ) from exc
            requirement = self.session.get(wf.HumanReviewRequirement, requirement_uuid)
        else:
            requirements = list(
                self.session.scalars(
                    select(wf.HumanReviewRequirement)
                    .where(
                        wf.HumanReviewRequirement.operation_id == operation.operation_id,
                        wf.HumanReviewRequirement.state == "open",
                    )
                    .order_by(wf.HumanReviewRequirement.opened_at.desc())
                    .limit(2)
                )
            )
            requirement = requirements[0] if len(requirements) == 1 else None
        if (
            requirement is None
            or requirement.operation_id != operation.operation_id
            or requirement.state != "open"
            or operation.phase != "held_human"
        ):
            raise CommandRuleError(
                "OPEN_HUMAN_REVIEW_REQUIRED",
                "the exact Human Review requirement is not open",
            )

        decision_value = str(
            call.arguments.get("detail", call.arguments.get("decision", ""))
        ).strip()
        rationale = str(call.arguments.get("rationale", decision_value)).strip()
        if not decision_value or not rationale:
            raise CommandRuleError(
                "HUMAN_DECISION_INCOMPLETE",
                "decision detail and rationale are required",
                http_status=400,
            )
        if decision_value.startswith("<") and decision_value.endswith(">"):
            raise CommandRuleError(
                "HUMAN_DECISION_PLACEHOLDER",
                "human decision still contains the unfilled command placeholder",
                http_status=400,
            )
        resume_status = str(
            call.arguments.get("resume_status", "pending-verification")
        ).strip()
        if resume_status not in {"pending-research", "pending-verification"}:
            raise CommandRuleError(
                "INVALID_RESUME_STATUS",
                "resume_status must be pending-research or pending-verification",
                http_status=400,
            )
        candidate_file_text = call.arguments.get("file_text")
        if call.arguments.get("file_path") and candidate_file_text is None:
            raise CommandRuleError(
                "MATERIAL_CONTENT_REQUIRED",
                "shadow-safe hold resolution requires complete canonical file_text, not a filesystem path",
                http_status=400,
            )
        editor = call.arguments.get("editor")
        model = call.arguments.get("model")
        if candidate_file_text is not None and (
            editor not in {"claude", "gpt", "codex"} or not str(model or "").strip()
        ):
            raise CommandRuleError(
                "MATERIAL_EDITOR_REQUIRED",
                "material hold resolution requires editor and model",
                http_status=400,
            )
        self._assert_hold_resolution_target(
            call=call,
            generation_id=generation.generation_id,
            task=task,
            baseline_content_version_id=requirement.baseline_content_version_id,
            cycle_id=requirement.cycle_id,
        )
        decision = self.workflow.record_human_decision(
            decision_id=self.uuid_factory(),
            requirement_id=requirement.requirement_id,
            execution_id=execution.execution_id,
            decision=decision_value,
            rationale=rationale,
            actor=call.owner_id,
            decided_at=call.now,
        )
        result = self._resume_held_document(
            generation_id=generation.generation_id,
            task_id=task.task_id,
            operation=operation,
            execution=execution,
            binding_id=binding.binding_id,
            baseline_content_version_id=requirement.baseline_content_version_id,
            expected_status="pending-human-review",
            decision_line=f"Human — Marco: human_review resolved — {decision_value}",
            at=call.now,
            resume_status=resume_status,
            candidate_file_text=(
                str(candidate_file_text) if candidate_file_text is not None else None
            ),
            editor=str(editor) if editor is not None else None,
            model=str(model) if model is not None else None,
            terminal_outcome_prefix="human_review",
        )
        result.update(
            {
                "decision_id": str(decision.decision_id),
                "requirement_id": str(requirement.requirement_id),
                "state": requirement.state,
            }
        )
        return result

    def _resolved(self, call, generation, binding, execution, task, operation) -> dict[str, Any]:
        assert task is not None and operation is not None
        if operation.phase != "held_human":
            raise CommandRuleError(
                "VERIFICATION_HOLD_REQUIRED",
                "resolved requires the exact current Verification hold",
            )
        cycle = self._latest_cycle(operation.operation_id)
        if cycle.outcome != "verification-hold":
            raise CommandRuleError(
                "VERIFICATION_HOLD_REQUIRED",
                "the latest Verification cycle is not a Verification hold",
            )
        self._assert_exact_verification_hold_target(
            call=call, generation_id=generation.generation_id, task_id=task.task_id, cycle=cycle
        )
        baseline_version_id = self._current_content_version_id(
            generation.generation_id, task.task_id
        )
        result = self._resume_held_document(
            generation_id=generation.generation_id,
            task_id=task.task_id,
            operation=operation,
            execution=execution,
            binding_id=binding.binding_id,
            baseline_content_version_id=baseline_version_id,
            expected_status="pending-human-review",
            decision_line=f"Human — Marco: Verification hold {cycle.cycle_id} resolved",
            at=call.now,
        )
        result.update(
            {
                "source_cycle_id": str(cycle.cycle_id),
                "approved": False,
                "signed_off": False,
            }
        )
        return result

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

    def _assert_exact_verification_hold_target(
        self,
        *,
        call: CommandCall,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        cycle: wf.VerificationCycle,
    ) -> None:
        cycle_id = call.arguments.get("cycle_id")
        hold_identity = str(call.arguments.get("hold_identity", "")).strip()
        if bool(cycle_id) != bool(hold_identity):
            raise CommandRuleError(
                "VERIFICATION_HOLD_TARGET_PAIR_REQUIRED",
                "cycle_id and hold_identity must be supplied together when an explicit hold target is used",
                http_status=400,
            )
        current_version_id = self._current_content_version_id(generation_id, task_id)
        current_version = self.session.get(models.ContentVersion, current_version_id)
        if current_version is None:
            raise CommandRuleError(
                "EXACT_VERIFICATION_HOLD_REQUIRED",
                "the current Verification hold content is missing",
            )
        parse_canonical_document(
            title=current_version.title,
            body=current_version.body,
            expected_status="pending-human-review",
        )
        if not cycle_id:
            return
        try:
            target_cycle_id = uuid.UUID(str(cycle_id))
        except ValueError as exc:
            raise CommandRuleError(
                "INVALID_CYCLE_ID",
                "cycle identifier must be a UUID",
                http_status=400,
            ) from exc
        if (
            target_cycle_id != cycle.cycle_id
            or current_version.content_identity != hold_identity
        ):
            raise CommandRuleError(
                "EXACT_VERIFICATION_HOLD_REQUIRED",
                "cycle_id or hold_identity does not match the current Verification hold",
            )

    def _assert_hold_resolution_target(
        self,
        *,
        call: CommandCall,
        generation_id: uuid.UUID,
        task: models.DishTask,
        baseline_content_version_id: uuid.UUID,
        cycle_id: uuid.UUID | None,
    ) -> None:
        expected_task_gid = call.arguments.get("expected_task_gid")
        if expected_task_gid not in {None, ""}:
            actual_task_gid = self.session.scalar(
                select(models.TaskExternalAlias.external_id).where(
                    models.TaskExternalAlias.task_id == task.task_id,
                    models.TaskExternalAlias.external_system == "asana",
                    models.TaskExternalAlias.state == "active",
                )
            )
            if str(expected_task_gid).strip() != str(actual_task_gid or ""):
                raise CommandRuleError(
                    "HOLD_TASK_MISMATCH",
                    "resolution command does not match the held task",
                )

        expected_cycle_id = call.arguments.get("expected_cycle_id")
        if expected_cycle_id not in {None, ""}:
            try:
                parsed_cycle_id = uuid.UUID(str(expected_cycle_id))
            except ValueError as exc:
                raise CommandRuleError(
                    "INVALID_CYCLE_ID",
                    "expected cycle identifier must be a UUID",
                    http_status=400,
                ) from exc
            if cycle_id is None or parsed_cycle_id != cycle_id:
                raise CommandRuleError(
                    "HOLD_CYCLE_MISMATCH",
                    "resolution command does not match the active hold cycle",
                )

        self._assert_baseline_content_current(
            generation_id, task.task_id, baseline_content_version_id
        )
        expected_identity = call.arguments.get("expected_hold_identity")
        if expected_identity not in {None, ""}:
            baseline = self.session.get(
                models.ContentVersion, baseline_content_version_id
            )
            if (
                baseline is None
                or str(expected_identity).strip() != baseline.content_identity
            ):
                raise CommandRuleError(
                    "HOLD_IDENTITY_MISMATCH",
                    "resolution command does not match the active hold identity",
                )

    def _resume_held_document(
        self,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        operation: wf.WorkflowOperation,
        execution: wf.CommandExecution,
        binding_id: uuid.UUID,
        baseline_content_version_id: uuid.UUID,
        expected_status: str,
        decision_line: str,
        at: datetime,
        resume_status: str | None = None,
        candidate_file_text: str | None = None,
        editor: str | None = None,
        model: str | None = None,
        terminal_outcome_prefix: str = "hold",
    ) -> dict[str, Any]:
        self._assert_baseline_content_current(
            generation_id, task_id, baseline_content_version_id
        )
        baseline = self.session.get(models.ContentVersion, baseline_content_version_id)
        if baseline is None:
            raise CommandRuleError(
                "HOLD_BASELINE_MISSING", "held content occurrence is missing"
            )
        held_parts = parse_canonical_document(
            title=baseline.title, body=baseline.body, expected_status=expected_status
        )
        candidate = (
            parse_canonical_document(file_text=candidate_file_text).document
            if candidate_file_text is not None
            else None
        )
        resumed_parts = resumed_document(
            held_parts.document,
            decision_line=decision_line,
            resume_status=resume_status,
            candidate=candidate,
            editor=editor,
            model=model,
            at=at,
        )
        resumed_status = resumed_parts.document.state.values["Status"]
        version_id = self._activate_document(
            generation_id=generation_id,
            task_id=task_id,
            binding_id=binding_id,
            execution_id=execution.execution_id,
            title=resumed_parts.title,
            body=resumed_parts.body,
            predecessor_content_version_id=baseline_content_version_id,
            at=at,
        )
        projection_id = self._project(
            generation_id,
            execution.execution_id,
            task_id,
            "update_task_document",
            {"content_version_id": str(version_id)},
            at,
        )
        if resumed_status == "pending-verification":
            cycle = self.workflow.open_verification_cycle(
                cycle_id=self.uuid_factory(),
                execution_id=execution.execution_id,
                operation_id=operation.operation_id,
                reviewed_content_version_id=version_id,
                created_at=at,
            )
            operation.phase = "await_verification"
            operation.persisted_actions = ["inspect"]
            cycle_id = str(cycle.cycle_id)
        elif resumed_status == "pending-research":
            operation.lifecycle = "completed"
            operation.phase = "terminal"
            operation.terminal_outcome = f"{terminal_outcome_prefix}_resolved_to_research"
            operation.terminal_at = at
            operation.persisted_actions = []
            cycle_id = None
        else:
            raise CommandRuleError(
                "HOLD_RESUME_STATUS_INVALID",
                "held document does not resume to a supported workflow status",
                data={"resume_status": resumed_status},
            )
        operation.operation_revision += 1
        return {
            "resumed_content_version_id": str(version_id),
            "resume_status": resumed_status,
            "cycle_id": cycle_id,
            "projection_event_id": projection_id,
        }

    def _activate_document(
        self,
        *,
        generation_id: uuid.UUID,
        task_id: uuid.UUID,
        binding_id: uuid.UUID,
        execution_id: uuid.UUID,
        title: str,
        body: str,
        predecessor_content_version_id: uuid.UUID,
        at: datetime,
    ) -> uuid.UUID:
        head = self.session.get(models.TaskAuthorityHead, (generation_id, task_id))
        if head is None:
            raise CommandRuleError(
                "CONTENT_AUTHORITY_MISSING", "task has no current authority head"
            )
        activation = self.session.get(
            models.ContentActivation, head.current_content_activation_id
        )
        if (
            activation is None
            or activation.content_version_id != predecessor_content_version_id
        ):
            raise CommandRuleError(
                "CONTENT_AUTHORITY_DRIFT",
                "the predecessor is not the exact current content occurrence",
            )
        identity = hashlib.sha256((title + "\0" + body).encode()).hexdigest()
        if self.session.scalar(
            select(models.ContentVersion.content_version_id).where(
                models.ContentVersion.generation_id == generation_id,
                models.ContentVersion.task_id == task_id,
                models.ContentVersion.identity_scheme == "sha256-title-body-v1",
                models.ContentVersion.content_identity == identity,
            )
        ) is not None:
            raise CommandRuleError(
                "CONTENT_OCCURRENCE_NOT_DISTINCT",
                "the candidate is identical to an existing task content occurrence",
            )
        version_id = self.uuid_factory()
        self.session.add(
            models.ContentVersion(
                content_version_id=version_id,
                generation_id=generation_id,
                task_id=task_id,
                representation_kind="document",
                title=title,
                body=body,
                identity_scheme="sha256-title-body-v1",
                content_identity=identity,
                creator_route="command_execution",
                import_run_id=None,
                command_execution_id=execution_id,
                predecessor_content_version_id=predecessor_content_version_id,
                contract_binding_id=binding_id,
                created_at=at,
            )
        )
        self.session.flush()
        activation_id = self.uuid_factory()
        revision = head.task_revision + 1
        self.session.add(
            models.ContentActivation(
                content_activation_id=activation_id,
                generation_id=generation_id,
                task_id=task_id,
                content_version_id=version_id,
                activation_route="command_execution",
                import_run_id=None,
                command_execution_id=execution_id,
                task_revision=revision,
                activated_at=at,
            )
        )
        self.session.flush()
        head.current_content_activation_id = activation_id
        head.task_revision = revision
        head.updated_at = at
        self.session.flush()
        return version_id

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
        attempt.state = "completed"
        attempt.successor_operation_id = successor.operation_id
        attempt.terminal_at = published_at
        return successor

    def _latest_cycle(self, operation_id: uuid.UUID) -> wf.VerificationCycle:
        cycle = self.session.scalar(
            select(wf.VerificationCycle)
            .where(wf.VerificationCycle.operation_id == operation_id)
            .order_by(wf.VerificationCycle.cycle_sequence.desc())
            .limit(1)
        )
        if cycle is None:
            raise CommandRuleError("VERIFICATION_CYCLE_REQUIRED", "operation has no Verification cycle")
        return cycle

    def _next_step(self, operation_id: uuid.UUID) -> int:
        return int(self.session.scalar(select(func.coalesce(func.max(wf.OperationStep.step_sequence), 0)).where(wf.OperationStep.operation_id == operation_id)) or 0) + 1

    def _next_actor_attempt_sequence(self, task_id: uuid.UUID) -> int:
        live_max = int(
            self.session.scalar(
                select(func.coalesce(func.max(wf.OperationActorFact.actor_attempt_sequence), 0)).where(
                    wf.OperationActorFact.task_id == task_id
                )
            )
            or 0
        )
        imported_lease_max = int(
            self.session.scalar(
                select(func.coalesce(func.max(wf.ServiceLease.actor_attempt_sequence), 0)).where(
                    wf.ServiceLease.task_id == task_id,
                    wf.ServiceLease.lease_kind == "actor",
                    wf.ServiceLease.import_run_id.is_not(None),
                )
            )
            or 0
        )
        return max(live_max, imported_lease_max) + 1

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

    def _release_verifier_lease(
        self,
        *,
        call: CommandCall,
        execution: wf.CommandExecution,
        cycle: wf.VerificationCycle,
        actor: wf.OperationActorFact,
        reason: str,
    ) -> None:
        lease = self.session.scalar(
            select(wf.ServiceLease)
            .where(
                wf.ServiceLease.operation_id == cycle.operation_id,
                wf.ServiceLease.run_id == actor.run_id,
                wf.ServiceLease.owner_id == actor.owner_id,
                wf.ServiceLease.actor_role == "verification",
                wf.ServiceLease.actor_attempt_sequence == actor.actor_attempt_sequence,
                wf.ServiceLease.verification_cycle_id == cycle.cycle_id,
                wf.ServiceLease.state == "active",
            )
            .order_by(wf.ServiceLease.issued_at.desc())
            .limit(1)
        )
        if lease is None:
            raise CommandRuleError(
                "VERIFICATION_LEASE_REQUIRED",
                f"{call.command_name} requires the exact active Verification lease",
            )
        self._terminalize_lease(lease, "released", execution, call.now, reason)

    def _terminalize_lease(self, lease, state, execution, at, reason) -> None:
        if state not in {"released", "expired", "recovered"}:
            raise ValueError(f"unsupported lease terminal state: {state}")
        if lease.state != "active":
            if lease.state == state:
                return
            raise CommandRuleError(
                "LEASE_ALREADY_TERMINAL",
                "lease is already terminal with a different durable state",
                data={"lease_id": str(lease.lease_id), "state": lease.state},
            )
        prior_revision, prior_expiry = lease.lease_revision, lease.expires_at
        lease.state = state
        lease.lease_revision = prior_revision + 1
        lease.terminal_at = at
        self.session.add(
            wf.LeaseEvent(
                lease_event_id=self.uuid_factory(),
                lease_id=lease.lease_id,
                event_kind=state,
                request_id=execution.request_id,
                command_execution_id=execution.execution_id,
                prior_revision=prior_revision,
                resulting_revision=prior_revision + 1,
                prior_expiry=prior_expiry,
                resulting_expiry=prior_expiry,
                reason=reason,
                occurred_at=at,
            )
        )

    def _project(self, generation_id, execution_id, task_id, event_type, payload, at) -> str:
        return record_projection_intent(
            self.projection_recorder,
            generation_id=generation_id,
            execution_id=execution_id,
            task_id=task_id,
            event_type=event_type,
            payload=payload,
            origin=self.projection_origin,
            created_at=at,
        )


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
