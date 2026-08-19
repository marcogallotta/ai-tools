"""Request-lifecycle coordinators for the shared Dish service boundary."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, ContextManager, Mapping, Protocol

from dish_tool.commands import DishApplication
from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope, result_envelope
from dish_tool.task_store import LiveTask, TaskBackend, read_complete_task

from .leases import LeaseManager, ServicePrincipal
from .planning_intent import planning_start_may_resume
from .request_replay import (
    FunctionalRequestReplay,
    RequestReplayPort,
    begin_request,
    complete_request,
    lookup_request,
    pending_error,
    stored_result,
)


def _default_request_replay() -> RequestReplayPort:
    return FunctionalRequestReplay(
        begin_fn=begin_request,
        stored_fn=stored_result,
        complete_fn=complete_request,
        pending_fn=pending_error,
        lookup_fn=lookup_request,
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
# Consumes upstream authority; does not derive lifecycle or design lineage.
# Nothing below is wired into DishService, HTTP, OpenAPI, config, or deployment.

LEGACY_DIRECT = "LEGACY_DIRECT"
MEDIATED_ACTION = "MEDIATED_ACTION"
ASANA_MUTATION_REPLAY_COMMAND = "development-workflow-asana-mutation"

# The V1 write contract is deliberately closed to fields the exact task reread
# observes. Each action has exactly one completion-state payload.
_SUPPORTED_COMPLETION_ACTIONS: dict[str, bool] = {
    "task.complete": True,
    "task.reopen": False,
}


@dataclass(frozen=True)
class AdmissionAuthorityReference:
    source: str
    identity: str
    revision: str


@dataclass(frozen=True)
class UpstreamMutationDecision:
    target_task_gid: str
    action_class: str
    status: str
    decision_ref: AdmissionAuthorityReference
    supporting_refs: tuple[AdmissionAuthorityReference, ...] = ()
    design_generation_ref: AdmissionAuthorityReference | None = None


@dataclass(frozen=True)
class TaskStateFingerprint:
    content_identity: str
    section_gid: str | None
    completed: bool

    @classmethod
    def from_live(cls, live: LiveTask) -> "TaskStateFingerprint":
        return cls(live.identity, live.section_gid, live.completed)


@dataclass(frozen=True)
class AsanaMutationObservation:
    target: TaskStateFingerprint
    target_modified_at: str | None
    upstream: UpstreamMutationDecision
    transport_mode: str


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
    transport_mode: str = MEDIATED_ACTION

    @property
    def mutation(self) -> dict[str, Any]:
        return json.loads(self.mutation_json)

    def replay_arguments(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "action_class": self.action_class,
            "target_task_gid": self.target_task_gid,
            "expected_before": asdict(self.expected_before),
            "expected_after": asdict(self.expected_after),
            "upstream": asdict(self.upstream),
            "mutation": self.mutation,
            "expires_at": self.expires_at.isoformat(),
            "design_bearing": self.design_bearing,
            "transport_mode": self.transport_mode,
            "readback_contract": "direct-exact-task-reread",
            "recovery_contract": "same-request-id-replay-never-blind-repeat",
        }


@dataclass(frozen=True)
class AsanaMutationAdmission:
    status: str
    observation: AsanaMutationObservation | None
    proposal: AsanaMutationProposal | None = None
    reason: str | None = None


class UpstreamMutationAuthorityPort(Protocol):
    def read_mutation_decision(
        self, *, target_task_gid: str, action_class: str
    ) -> UpstreamMutationDecision: ...


def _canonical_mutation_for_action(action_class: str) -> dict[str, Any]:
    if action_class not in _SUPPORTED_COMPLETION_ACTIONS:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "unsupported mediated Asana mutation action",
            rule="asana_mutation_action_unsupported",
            retryable=False,
            details={"action_class": action_class},
        )
    return {"completed": _SUPPORTED_COMPLETION_ACTIONS[action_class]}


def _derived_expected_after(
    action_class: str,
    before: TaskStateFingerprint,
) -> TaskStateFingerprint:
    mutation = _canonical_mutation_for_action(action_class)
    return TaskStateFingerprint(
        before.content_identity,
        before.section_gid,
        bool(mutation["completed"]),
    )


def _validate_mutation_contract(
    *,
    action_class: str,
    mutation: Mapping[str, Any],
    expected_before: TaskStateFingerprint,
    expected_after: TaskStateFingerprint,
) -> dict[str, Any]:
    canonical = _canonical_mutation_for_action(action_class)
    if dict(mutation) != canonical:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "mutation payload does not match the authorized action class",
            rule="asana_mutation_action_payload_mismatch",
            retryable=False,
            details={"action_class": action_class},
        )
    derived_after = _derived_expected_after(action_class, expected_before)
    if expected_after != derived_after:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "expected readback does not match the authorized mutation",
            rule="asana_mutation_expected_after_mismatch",
            retryable=False,
            details={"action_class": action_class},
        )
    return canonical


class AsanaMutationAdmissionCoordinator:
    """Thin observe/propose/shadow seam with an explicitly inactive write path."""

    def __init__(
        self,
        *,
        backend: TaskBackend,
        project_gid: str,
        authority: UpstreamMutationAuthorityPort,
        replay: RequestReplayPort | None = None,
        writes_enabled: bool = False,
    ) -> None:
        self.backend = backend
        self.project_gid = project_gid
        self.authority = authority
        self.replay = replay or _default_request_replay()
        self.writes_enabled = writes_enabled

    def observe(
        self,
        *,
        target_task_gid: str,
        action_class: str,
        transport_mode: str = MEDIATED_ACTION,
    ) -> AsanaMutationObservation:
        if transport_mode not in {LEGACY_DIRECT, MEDIATED_ACTION}:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "unsupported Asana mutation transport mode",
                rule="asana_mutation_transport_mode_invalid",
            )
        upstream = self.authority.read_mutation_decision(
            target_task_gid=target_task_gid, action_class=action_class
        )
        if (upstream.target_task_gid, upstream.action_class) != (
            target_task_gid,
            action_class,
        ):
            raise DishRuleError(
                "CONFLICT",
                "upstream mutation decision does not match target/action",
                rule="asana_mutation_upstream_identity_mismatch",
            )
        live = read_complete_task(
            self.backend, task_gid=target_task_gid, project_gid=self.project_gid
        )
        return AsanaMutationObservation(
            TaskStateFingerprint.from_live(live),
            live.modified_at,
            upstream,
            transport_mode,
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
            target_task_gid=target_task_gid, action_class=action_class
        )
        if observed.upstream.status != "PERMITTED":
            return AsanaMutationAdmission(
                "BLOCKED_UPSTREAM", observed, reason=observed.upstream.status
            )
        if design_bearing and observed.upstream.design_generation_ref is None:
            return AsanaMutationAdmission(
                "BLOCKED_UPSTREAM",
                observed,
                reason="missing-design-generation-reference",
            )
        canonical_mutation = _validate_mutation_contract(
            action_class=action_class,
            mutation=mutation,
            expected_before=observed.target,
            expected_after=expected_after,
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
        mutation_json = json.dumps(
            canonical_mutation,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        proposal = AsanaMutationProposal(
            proposal_id,
            action_class,
            target_task_gid,
            observed.target,
            expected_after,
            observed.upstream,
            mutation_json,
            created.astimezone(timezone.utc) + timedelta(seconds=ttl_seconds),
            design_bearing,
        )
        return AsanaMutationAdmission("PROPOSED", observed, proposal)

    def shadow_admit(
        self, proposal: AsanaMutationProposal, *, now: datetime | None = None
    ) -> AsanaMutationAdmission:
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None or current.utcoffset() is None:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "admission time must be timezone-aware",
                rule="asana_mutation_proposal_time_invalid",
            )
        if current.astimezone(timezone.utc) >= proposal.expires_at:
            return AsanaMutationAdmission("STALE", None, proposal, "expired")
        observed = self.observe(
            target_task_gid=proposal.target_task_gid,
            action_class=proposal.action_class,
        )
        if observed.upstream.status != "PERMITTED":
            return AsanaMutationAdmission(
                "BLOCKED_UPSTREAM", observed, proposal, observed.upstream.status
            )
        if observed.upstream != proposal.upstream:
            return AsanaMutationAdmission(
                "STALE", observed, proposal, "upstream-authority-changed"
            )
        if observed.target != proposal.expected_before:
            return AsanaMutationAdmission(
                "STALE", observed, proposal, "task-precondition-changed"
            )
        _validate_mutation_contract(
            action_class=proposal.action_class,
            mutation=proposal.mutation,
            expected_before=proposal.expected_before,
            expected_after=proposal.expected_after,
        )
        return AsanaMutationAdmission("WOULD_ADMIT", observed, proposal)

    @staticmethod
    def _error(
        proposal: AsanaMutationProposal, error: DishRuleError
    ) -> dict[str, Any]:
        result = error_envelope(
            ASANA_MUTATION_REPLAY_COMMAND,
            error,
            task_gid=proposal.target_task_gid,
        )
        result["data"].update(
            {"proposal_id": proposal.proposal_id, "transport_mode": MEDIATED_ACTION}
        )
        return result

    def _settle_error(
        self,
        conn: sqlite3.Connection,
        proposal: AsanaMutationProposal,
        request_id: str,
        error: DishRuleError,
    ) -> dict[str, Any]:
        return self.replay.complete(
            conn, request_id=request_id, result=self._error(proposal, error)
        )

    def _existing_replay(
        self,
        conn: sqlite3.Connection,
        *,
        principal: ServicePrincipal,
        request_id: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        lookup = getattr(self.replay, "lookup", None)
        if not callable(lookup):
            return None
        row = lookup(
            conn,
            request_id=request_id,
            owner_id=principal.owner_id,
            run_id=principal.run_id,
            command=ASANA_MUTATION_REPLAY_COMMAND,
            arguments=arguments,
        )
        if row is None:
            return None
        stored = self.replay.stored(row)
        if stored is not None:
            return stored
        raise self.replay.pending(ASANA_MUTATION_REPLAY_COMMAND, request_id)

    def execute(
        self,
        conn: sqlite3.Connection,
        proposal: AsanaMutationProposal,
        *,
        principal: ServicePrincipal,
        request_id: str,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        replay_arguments = proposal.replay_arguments()
        replayed = self._existing_replay(
            conn,
            principal=principal,
            request_id=request_id,
            arguments=replay_arguments,
        )
        if replayed is not None:
            return replayed

        # Rollback may disable fresh writes, but it cannot hide an already
        # admitted result. The existing replay check above always runs first.
        if not self.writes_enabled:
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
            arguments=replay_arguments,
        )
        stored = self.replay.stored(row)
        if stored is not None:
            return stored
        if not fresh:
            raise self.replay.pending(ASANA_MUTATION_REPLAY_COMMAND, request_id)

        try:
            admission = self.shadow_admit(proposal, now=now)
        except DishRuleError as exc:
            return self._settle_error(conn, proposal, request_id, exc)
        if admission.status != "WOULD_ADMIT":
            return self._settle_error(
                conn,
                proposal,
                request_id,
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

        canonical_mutation = _validate_mutation_contract(
            action_class=proposal.action_class,
            mutation=proposal.mutation,
            expected_before=proposal.expected_before,
            expected_after=proposal.expected_after,
        )
        try:
            self.backend.update_task_completed(
                task_gid=proposal.target_task_gid,
                completed=bool(canonical_mutation["completed"]),
            )
        except DishRuleError as exc:
            return self._settle_error(conn, proposal, request_id, exc)
        except Exception as exc:
            return self._settle_error(
                conn,
                proposal,
                request_id,
                DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "mediated Asana mutation outcome is uncertain",
                    rule="asana_mutation_effect_uncertain",
                    retryable=False,
                    details={"error_type": type(exc).__name__},
                ),
            )

        try:
            live_after = read_complete_task(
                self.backend,
                task_gid=proposal.target_task_gid,
                project_gid=self.project_gid,
            )
        except Exception as exc:
            return self._settle_error(
                conn,
                proposal,
                request_id,
                DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "mediated Asana mutation could not be confirmed by reread",
                    rule="asana_mutation_readback_unavailable",
                    retryable=False,
                    details={"error_type": type(exc).__name__},
                ),
            )

        observed_after = TaskStateFingerprint.from_live(live_after)
        if observed_after != proposal.expected_after:
            return self._settle_error(
                conn,
                proposal,
                request_id,
                DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "mediated Asana mutation readback mismatched the proposal",
                    rule="asana_mutation_readback_mismatch",
                    retryable=False,
                ),
            )

        return self.replay.complete(
            conn,
            request_id=request_id,
            result=result_envelope(
                command=ASANA_MUTATION_REPLAY_COMMAND,
                task_gid=proposal.target_task_gid,
                data={
                    "proposal_id": proposal.proposal_id,
                    "transport_mode": MEDIATED_ACTION,
                    "admission": "CONFIRMED",
                    "observed_after": asdict(observed_after),
                },
            ),
        )
