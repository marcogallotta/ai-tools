"""Request-lifecycle coordinators for the shared Dish service boundary."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from dish_tool.commands import DishApplication
from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope

from .leases import LeaseManager, ServicePrincipal
from .planning_intent import planning_start_may_resume


@dataclass
class AgentExecutionState:
    conn: sqlite3.Connection
    principal: ServicePrincipal
    leases: LeaseManager
    invocation_run_id: str | None
    prepared_arguments: dict[str, Any]
    backend: Any = None
    app: DishApplication | None = None
    request_row: Any = None
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
    backend: Any = None
    replay_started: bool = False
    acquired_for_request: bool = False
    exact_recovery_execution_id: str | None = None
    exact_recovery_lease_id: str | None = None


class AgentRequestCoordinator:
    """Own the top-level lifecycle of one connected-agent request."""

    def __init__(
        self,
        service: Any,
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
            principal = principal or service._default_principal(arguments)
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
        service: Any,
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
            principal = principal or service._default_principal(arguments, admin=True)
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
