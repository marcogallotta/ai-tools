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


# Backward-compatible re-exports for the source-only admission seam. Keeping
# these names here avoids changing existing callers/tests while the implementation
# itself stays isolated from the service request coordinator lifecycle.
from .asana_mutation_admission import (  # noqa: E402,F401
    ASANA_MUTATION_REPLAY_COMMAND,
    LEGACY_DIRECT,
    MEDIATED_ACTION,
    AdmissionAuthorityReference,
    AsanaMutationAdmission,
    AsanaMutationAdmissionCoordinator,
    AsanaMutationEffectPort,
    AsanaMutationObservation,
    AsanaMutationProposal,
    TaskStateFingerprint,
    UpstreamMutationAuthorityPort,
    UpstreamMutationDecision,
)
