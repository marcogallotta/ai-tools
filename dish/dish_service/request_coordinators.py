"""Request-lifecycle coordinators for the shared Dish service boundary."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Mapping, Protocol

from dish_tool.commands import DishApplication
from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope

from .leases import LeaseManager, ServicePrincipal
from .planning_intent import planning_start_may_resume
from .request_replay import (
    FunctionalRequestReplay,
    RequestReplayPort,
    begin_request,
    complete_request,
    pending_error,
    stored_result,
)


def _default_request_replay() -> RequestReplayPort:
    return FunctionalRequestReplay(
        begin_fn=begin_request,
        stored_fn=stored_result,
        complete_fn=complete_request,
        pending_fn=pending_error,
    )


class RequestGatePort(Protocol):
    def request(self) -> ContextManager[None]: ...


class ShadowCapturePort(Protocol):
    def execute(self, **kwargs: Any) -> dict[str, Any]: ...


class AgentRequestServicePort(Protocol):
    _maintenance_gate: RequestGatePort
    _shadow_capture: ShadowCapturePort
    _request_replay: RequestReplayPort

    def _planning_intent_execution_lock(
        self, command: str, arguments: Mapping[str, Any]
    ) -> ContextManager[None]: ...
    def _default_principal(
        self, arguments: Mapping[str, Any], *, admin: bool = False
    ) -> ServicePrincipal: ...
    def _initialize_database(self, **kwargs: Any) -> sqlite3.Connection: ...
    def _lease_manager(self, conn: sqlite3.Connection) -> LeaseManager: ...
    def _arguments_for_principal(self, command: str, arguments: Mapping[str, Any], *, run_id: str | None) -> dict[str, Any]: ...
    def _begin_agent_execution(self, state: "AgentExecutionState", *, command: str, request_id: str | None) -> dict[str, Any] | None: ...
    def _build_agent_application(self, state: "AgentExecutionState", *, command: str, request_id: str | None) -> None: ...
    def _reconcile_pending_start(self, **kwargs: Any) -> dict[str, Any]: ...
    def _resolve_agent_operation(self, state: "AgentExecutionState", *, command: str) -> None: ...
    def _acquire_agent_lease(self, state: "AgentExecutionState", *, command: str) -> None: ...
    def _dispatch_agent_command(self, state: "AgentExecutionState", *, command: str) -> dict[str, Any]: ...
    def _finish_agent_result(self, state: "AgentExecutionState", *, command: str, request_id: str | None, result: dict[str, Any]) -> dict[str, Any]: ...
    def _agent_rule_error_result(self, state: "AgentExecutionState", *, command: str, arguments: Mapping[str, Any], request_id: str | None, error: DishRuleError) -> dict[str, Any]: ...
    def _close_backend(self, backend: object | None) -> None: ...


class AdminRequestServicePort(Protocol):
    _maintenance_gate: RequestGatePort
    _shadow_capture: ShadowCapturePort
    _request_replay: RequestReplayPort

    def _default_principal(
        self, arguments: Mapping[str, Any], *, admin: bool = False
    ) -> ServicePrincipal: ...
    def _initialize_database(self, **kwargs: Any) -> sqlite3.Connection: ...
    def _prepare_admin_execution_state(self, conn: sqlite3.Connection, **kwargs: Any) -> "AdminExecutionState": ...
    def _begin_admin_execution(self, state: "AdminExecutionState", *, command: str, request_id: str | None) -> dict[str, Any] | None: ...
    def _build_admin_backend(self, state: "AdminExecutionState", *, command: str, request_id: str | None) -> dict[str, Any] | None: ...
    def _acquire_admin_execution_lease(self, state: "AdminExecutionState", *, command: str) -> None: ...
    def _dispatch_admin_command(self, state: "AdminExecutionState", *, command: str, request_id: str | None) -> dict[str, Any]: ...
    def _finish_admin_result(self, state: "AdminExecutionState", *, command: str, request_id: str | None, result: dict[str, Any]) -> dict[str, Any]: ...
    def _admin_rule_error_result(self, state: "AdminExecutionState", *, command: str, request_id: str | None, error: DishRuleError) -> dict[str, Any]: ...
    def _close_backend(self, backend: object | None) -> None: ...


@dataclass
class AgentExecutionState:
    conn: sqlite3.Connection
    principal: ServicePrincipal
    leases: LeaseManager
    invocation_run_id: str | None
    prepared_arguments: dict[str, Any]
    replay: RequestReplayPort = field(default_factory=_default_request_replay)
    backend: object | None = None
    app: DishApplication | None = None
    request_row: sqlite3.Row | None = None
    operation_id: str | None = None
    verification_start_cycle_id: str | None = None
    replay_started: bool = False
    acquired_for_request: bool = False
    completed_submit: bool = False


@dataclass
class AdminExecutionState:
    conn: sqlite3.Connection
    principal: ServicePrincipal
    leases: LeaseManager
    prepared_arguments: dict[str, Any]
    operation_id: str | None
    supplied_run_id: str
    replay: RequestReplayPort = field(default_factory=_default_request_replay)
    backend: object | None = None
    replay_started: bool = False
    acquired_for_request: bool = False
    exact_recovery_execution_id: str | None = None
    exact_recovery_lease_id: str | None = None


class AgentRequestCoordinator:
    """Own the top-level lifecycle of one connected-agent request."""

    def __init__(
        self,
        service: AgentRequestServicePort,
        *,
        initialization_error: Callable[[BaseException], DishRuleError],
    ) -> None:
        self.service = service
        self.initialization_error = initialization_error

    def execute(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        service = self.service
        with service._maintenance_gate.request(), service._planning_intent_execution_lock(
            command, arguments
        ):
            explicit_principal = principal is not None
            effective_principal = principal or service._default_principal(arguments)
            return service._shadow_capture.execute(
                command=command,
                arguments=arguments,
                principal=effective_principal,
                principal_class="agent",
                request_id=request_id,
                call=lambda: self._execute_locked(
                    command,
                    arguments,
                    principal=effective_principal,
                    request_id=request_id,
                    explicit_principal=explicit_principal,
                ),
            )

    def _execute_locked(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal,
        request_id: str | None,
        explicit_principal: bool,
    ) -> dict[str, Any]:
        service = self.service
        task_gid = str(arguments.get("task_gid") or "").strip() or None
        requested_operation_id = (
            str(arguments.get("submission_id") or "").strip() or None
        )
        try:
            conn = service._initialize_database(
                surface="agent",
                command=command,
                request_id=request_id,
                principal=principal,
                task_gid=task_gid,
                operation_id=requested_operation_id,
            )
        except Exception as exc:
            return error_envelope(
                command,
                self.initialization_error(exc),
                task_gid=task_gid,
                submission_id=requested_operation_id,
            )
        invocation_run_id = (
            principal.run_id
            if explicit_principal
            else str(arguments.get("run_id") or "").strip() or None
        )
        state = AgentExecutionState(
            conn=conn,
            principal=principal,
            leases=service._lease_manager(conn),
            invocation_run_id=invocation_run_id,
            prepared_arguments={},
            replay=service._request_replay,
        )
        try:
            state.prepared_arguments = service._arguments_for_principal(
                command, arguments, run_id=invocation_run_id
            )
            early = service._begin_agent_execution(
                state, command=command, request_id=request_id
            )
            if early is not None:
                return early
            service._build_agent_application(
                state, command=command, request_id=request_id
            )
            if (
                state.request_row is not None
                and not state.replay_started
                and command == "start"
            ):
                resumable_planning = bool(
                    state.prepared_arguments.get("kind") == "planning"
                    and planning_start_may_resume(
                        state.conn,
                        request_id=str(request_id),
                        arguments=state.prepared_arguments,
                    )
                )
                if not resumable_planning:
                    return service._reconcile_pending_start(
                        conn=state.conn,
                        backend=state.backend,
                        app=state.app,
                        leases=state.leases,
                        principal=state.principal,
                        arguments=state.prepared_arguments,
                        request_id=str(request_id),
                    )
            service._resolve_agent_operation(state, command=command)
            service._acquire_agent_lease(state, command=command)
            result = service._dispatch_agent_command(state, command=command)
            return service._finish_agent_result(
                state, command=command, request_id=request_id, result=result
            )
        except DishRuleError as exc:
            return service._agent_rule_error_result(
                state,
                command=command,
                arguments=arguments,
                request_id=request_id,
                error=exc,
            )
        finally:
            service._close_backend(state.backend)
            conn.close()


class AdminRequestCoordinator:
    """Own the top-level lifecycle of one Marco-admin request."""

    def __init__(
        self,
        service: AdminRequestServicePort,
        *,
        initialization_error: Callable[[BaseException], DishRuleError],
    ) -> None:
        self.service = service
        self.initialization_error = initialization_error

    def execute(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        service = self.service
        with service._maintenance_gate.request():
            effective_principal = principal or service._default_principal(
                arguments, admin=True
            )
            return service._shadow_capture.execute(
                command=command,
                arguments=arguments,
                principal=effective_principal,
                principal_class="admin",
                request_id=request_id,
                call=lambda: self._execute_locked(
                    command,
                    arguments,
                    principal=effective_principal,
                    request_id=request_id,
                ),
            )

    def _execute_locked(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal,
        request_id: str | None,
    ) -> dict[str, Any]:
        service = self.service
        requested_operation_id = (
            str(arguments.get("submission_id") or "").strip() or None
        )
        try:
            conn = service._initialize_database(
                surface="admin",
                command=command,
                request_id=request_id,
                principal=principal,
                operation_id=requested_operation_id,
            )
        except Exception as exc:
            return error_envelope(
                command,
                self.initialization_error(exc),
                submission_id=requested_operation_id,
            )
        try:
            state = service._prepare_admin_execution_state(
                conn,
                command=command,
                arguments=arguments,
                principal=principal,
                requested_operation_id=requested_operation_id,
            )
        except DishRuleError as exc:
            conn.close()
            return error_envelope(
                command, exc, submission_id=requested_operation_id
            )
        try:
            early = service._begin_admin_execution(
                state, command=command, request_id=request_id
            )
            if early is not None:
                return early
            early = service._build_admin_backend(
                state, command=command, request_id=request_id
            )
            if early is not None:
                return early
            service._acquire_admin_execution_lease(state, command=command)
            result = service._dispatch_admin_command(
                state, command=command, request_id=request_id
            )
            return service._finish_admin_result(
                state, command=command, request_id=request_id, result=result
            )
        except DishRuleError as exc:
            return service._admin_rule_error_result(
                state,
                command=command,
                request_id=request_id,
                error=exc,
            )
        finally:
            service._close_backend(state.backend)
            conn.close()


# ---------------------------------------------------------------------------
# Development Workflow Asana mutation admission / transport (source-only V1)
# ---------------------------------------------------------------------------
# This seam consumes upstream authority. It does not derive lifecycle state or
# design lineage, and it is not wired into DishService/HTTP/OpenAPI/deployment.

from datetime import datetime, timedelta, timezone
import json
from typing import Literal

from dish_tool.results import result_envelope
from dish_tool.task_store import LiveTask, TaskBackend, read_complete_task


LEGACY_DIRECT = "LEGACY_DIRECT"
MEDIATED_ACTION = "MEDIATED_ACTION"
ASANA_MUTATION_REPLAY_COMMAND = "development-workflow-asana-mutation"

UpstreamAdmissionStatus = Literal[
    "PERMITTED", "BLOCKED", "CONTRADICTORY", "UNAVAILABLE"
]
AdmissionStatus = Literal["PROPOSED", "BLOCKED_UPSTREAM", "WOULD_ADMIT", "STALE"]


@dataclass(frozen=True, order=True)
class AdmissionAuthorityReference:
    """Opaque upstream identity; this layer never interprets the referenced state."""

    source: str
    identity: str
    revision: str

    def data(self) -> dict[str, str]:
        return {
            "source": self.source,
            "identity": self.identity,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class UpstreamMutationDecision:
    """Upstream answer already bound to one exact target and action class."""

    target_task_gid: str
    action_class: str
    status: UpstreamAdmissionStatus
    decision_ref: AdmissionAuthorityReference
    supporting_refs: tuple[AdmissionAuthorityReference, ...] = ()
    design_generation_ref: AdmissionAuthorityReference | None = None

    def data(self) -> dict[str, Any]:
        return {
            "target_task_gid": self.target_task_gid,
            "action_class": self.action_class,
            "status": self.status,
            "decision_ref": self.decision_ref.data(),
            "supporting_refs": [
                ref.data() for ref in sorted(self.supporting_refs)
            ],
            "design_generation_ref": (
                None
                if self.design_generation_ref is None
                else self.design_generation_ref.data()
            ),
        }


@dataclass(frozen=True)
class TaskStateFingerprint:
    content_identity: str
    section_gid: str | None
    completed: bool

    @classmethod
    def from_live(cls, live: LiveTask) -> "TaskStateFingerprint":
        return cls(live.identity, live.section_gid, live.completed)

    def data(self) -> dict[str, Any]:
        return {
            "content_identity": self.content_identity,
            "section_gid": self.section_gid,
            "completed": self.completed,
        }


@dataclass(frozen=True)
class AsanaMutationObservation:
    target: TaskStateFingerprint
    target_modified_at: str | None
    upstream: UpstreamMutationDecision
    transport_mode: Literal["LEGACY_DIRECT", "MEDIATED_ACTION"]


@dataclass(frozen=True)
class AsanaMutationProposal:
    proposal_id: str
    action_class: str
    target_task_gid: str
    expected_before: TaskStateFingerprint
    expected_after: TaskStateFingerprint
    upstream: UpstreamMutationDecision
    mutation_json: str
    expires_at: datetime
    design_bearing: bool
    transport_mode: Literal["MEDIATED_ACTION"] = MEDIATED_ACTION
    readback_contract: str = "direct-exact-task-reread"
    recovery_contract: str = "same-request-id-replay-never-blind-repeat"

    @property
    def mutation(self) -> dict[str, Any]:
        return json.loads(self.mutation_json)

    def replay_arguments(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "action_class": self.action_class,
            "target_task_gid": self.target_task_gid,
            "expected_before": self.expected_before.data(),
            "expected_after": self.expected_after.data(),
            "upstream": self.upstream.data(),
            "mutation": self.mutation,
            "expires_at": self.expires_at.isoformat(),
            "design_bearing": self.design_bearing,
            "transport_mode": self.transport_mode,
            "readback_contract": self.readback_contract,
            "recovery_contract": self.recovery_contract,
        }


@dataclass(frozen=True)
class AsanaMutationAdmission:
    status: AdmissionStatus
    observation: AsanaMutationObservation | None
    proposal: AsanaMutationProposal | None = None
    reason: str | None = None


class UpstreamMutationAuthorityPort(Protocol):
    def read_mutation_decision(
        self, *, target_task_gid: str, action_class: str
    ) -> UpstreamMutationDecision: ...


class AsanaMutationEffectPort(Protocol):
    def apply(self, *, proposal: AsanaMutationProposal) -> None: ...


class AsanaMutationAdmissionCoordinator:
    """Observe, propose, dry-run, and optionally transport one Asana mutation.

    No runtime surface constructs this coordinator. ``writes_enabled`` defaults
    false and there is no environment/config switch in this change.
    """

    def __init__(
        self,
        *,
        backend: TaskBackend,
        project_gid: str,
        authority: UpstreamMutationAuthorityPort,
        replay: RequestReplayPort | None = None,
        effect: AsanaMutationEffectPort | None = None,
        writes_enabled: bool = False,
    ) -> None:
        self.backend = backend
        self.project_gid = project_gid
        self.authority = authority
        self.replay = replay or _default_request_replay()
        self.effect = effect
        self.writes_enabled = writes_enabled

    def observe(
        self,
        *,
        target_task_gid: str,
        action_class: str,
        transport_mode: Literal[
            "LEGACY_DIRECT", "MEDIATED_ACTION"
        ] = MEDIATED_ACTION,
    ) -> AsanaMutationObservation:
        if transport_mode not in {LEGACY_DIRECT, MEDIATED_ACTION}:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "unsupported Asana mutation transport mode",
                rule="asana_mutation_transport_mode_invalid",
            )
        upstream = self.authority.read_mutation_decision(
            target_task_gid=target_task_gid,
            action_class=action_class,
        )
        if (
            upstream.target_task_gid != target_task_gid
            or upstream.action_class != action_class
        ):
            raise DishRuleError(
                "CONFLICT",
                "upstream mutation decision does not match the requested target/action",
                rule="asana_mutation_upstream_identity_mismatch",
            )
        live = read_complete_task(
            self.backend,
            task_gid=target_task_gid,
            project_gid=self.project_gid,
        )
        return AsanaMutationObservation(
            target=TaskStateFingerprint.from_live(live),
            target_modified_at=live.modified_at,
            upstream=upstream,
            transport_mode=transport_mode,
        )

    def propose(
        self,
        *,
        proposal_id: str,
        target_task_gid: str,
        action_class: str,
        mutation: Mapping[str, Any],
        expected_after: TaskStateFingerprint,
        design_bearing: bool = False,
        ttl_seconds: int = 300,
        now: datetime | None = None,
    ) -> AsanaMutationAdmission:
        observed = self.observe(
            target_task_gid=target_task_gid,
            action_class=action_class,
        )
        if observed.upstream.status != "PERMITTED":
            return AsanaMutationAdmission(
                "BLOCKED_UPSTREAM",
                observed,
                reason=f"upstream status is {observed.upstream.status}",
            )
        if design_bearing and observed.upstream.design_generation_ref is None:
            return AsanaMutationAdmission(
                "BLOCKED_UPSTREAM",
                observed,
                reason="design-bearing mutation lacks an exact design-generation reference",
            )
        if ttl_seconds <= 0:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "proposal ttl must be positive",
                rule="asana_mutation_proposal_ttl_invalid",
            )
        created = now or datetime.now(timezone.utc)
        if created.tzinfo is None or created.utcoffset() is None:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "proposal time must be timezone-aware",
                rule="asana_mutation_proposal_time_invalid",
            )
        try:
            mutation_json = json.dumps(
                dict(mutation),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "mutation payload must be canonical JSON data",
                rule="asana_mutation_payload_not_json",
            ) from exc
        proposal = AsanaMutationProposal(
            proposal_id=proposal_id,
            action_class=action_class,
            target_task_gid=target_task_gid,
            expected_before=observed.target,
            expected_after=expected_after,
            upstream=observed.upstream,
            mutation_json=mutation_json,
            expires_at=created.astimezone(timezone.utc)
            + timedelta(seconds=ttl_seconds),
            design_bearing=design_bearing,
        )
        return AsanaMutationAdmission("PROPOSED", observed, proposal=proposal)

    def shadow_admit(
        self,
        proposal: AsanaMutationProposal,
        *,
        now: datetime | None = None,
    ) -> AsanaMutationAdmission:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "admission time must be timezone-aware",
                rule="asana_mutation_proposal_time_invalid",
            )
        if current.astimezone(timezone.utc) >= proposal.expires_at:
            return AsanaMutationAdmission(
                "STALE", None, proposal=proposal, reason="proposal expired"
            )
        observed = self.observe(
            target_task_gid=proposal.target_task_gid,
            action_class=proposal.action_class,
        )
        if observed.upstream.status != "PERMITTED":
            return AsanaMutationAdmission(
                "BLOCKED_UPSTREAM",
                observed,
                proposal=proposal,
                reason=f"upstream status is {observed.upstream.status}",
            )
        if observed.upstream != proposal.upstream:
            return AsanaMutationAdmission(
                "STALE",
                observed,
                proposal=proposal,
                reason="upstream authority reference changed",
            )
        if observed.target != proposal.expected_before:
            return AsanaMutationAdmission(
                "STALE",
                observed,
                proposal=proposal,
                reason="exact Asana task precondition changed",
            )
        return AsanaMutationAdmission(
            "WOULD_ADMIT", observed, proposal=proposal
        )

    @staticmethod
    def _error(
        proposal: AsanaMutationProposal,
        error: DishRuleError,
    ) -> dict[str, Any]:
        result = error_envelope(
            ASANA_MUTATION_REPLAY_COMMAND,
            error,
            task_gid=proposal.target_task_gid,
        )
        result["data"].update(
            {
                "proposal_id": proposal.proposal_id,
                "transport_mode": MEDIATED_ACTION,
            }
        )
        return result

    def execute(
        self,
        conn: sqlite3.Connection,
        proposal: AsanaMutationProposal,
        *,
        principal: ServicePrincipal,
        request_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if not self.writes_enabled or self.effect is None:
            return self._error(
                proposal,
                DishRuleError(
                    "WRONG_STATE",
                    "mediated Asana writes are inactive",
                    rule="asana_mutation_writes_inactive",
                    retryable=False,
                ),
            )

        row, fresh = self.replay.begin(
            conn,
            request_id=request_id,
            owner_id=principal.owner_id,
            run_id=principal.run_id,
            command=ASANA_MUTATION_REPLAY_COMMAND,
            arguments=proposal.replay_arguments(),
        )
        stored = self.replay.stored(row)
        if stored is not None:
            return stored
        if not fresh:
            raise self.replay.pending(ASANA_MUTATION_REPLAY_COMMAND, request_id)

        admission = self.shadow_admit(proposal, now=now)
        if admission.status != "WOULD_ADMIT":
            result = self._error(
                proposal,
                DishRuleError(
                    "WRONG_STATE"
                    if admission.status == "BLOCKED_UPSTREAM"
                    else "CONFLICT",
                    admission.reason or "proposal is not admissible",
                    rule=(
                        "asana_mutation_upstream_blocked"
                        if admission.status == "BLOCKED_UPSTREAM"
                        else "asana_mutation_proposal_stale"
                    ),
                    retryable=False,
                ),
            )
            return self.replay.complete(
                conn, request_id=request_id, result=result
            )

        try:
            self.effect.apply(proposal=proposal)
            live_after = read_complete_task(
                self.backend,
                task_gid=proposal.target_task_gid,
                project_gid=self.project_gid,
            )
        except DishRuleError as exc:
            result = self._error(proposal, exc)
            return self.replay.complete(
                conn, request_id=request_id, result=result
            )
        except BaseException as exc:
            result = self._error(
                proposal,
                DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "mediated Asana mutation outcome is uncertain",
                    rule="asana_mutation_effect_uncertain",
                    retryable=False,
                    details={"error_type": type(exc).__name__},
                ),
            )
            return self.replay.complete(
                conn, request_id=request_id, result=result
            )

        observed_after = TaskStateFingerprint.from_live(live_after)
        if observed_after != proposal.expected_after:
            result = self._error(
                proposal,
                DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "mediated Asana mutation readback did not match the proposal",
                    rule="asana_mutation_readback_mismatch",
                    retryable=False,
                    details={
                        "expected_after": proposal.expected_after.data(),
                        "observed_after": observed_after.data(),
                    },
                ),
            )
            return self.replay.complete(
                conn, request_id=request_id, result=result
            )

        result = result_envelope(
            command=ASANA_MUTATION_REPLAY_COMMAND,
            task_gid=proposal.target_task_gid,
            data={
                "proposal_id": proposal.proposal_id,
                "transport_mode": MEDIATED_ACTION,
                "admission": "CONFIRMED",
                "observed_after": observed_after.data(),
                "readback_contract": proposal.readback_contract,
                "recovery_contract": proposal.recovery_contract,
            },
        )
        return self.replay.complete(
            conn, request_id=request_id, result=result
        )
