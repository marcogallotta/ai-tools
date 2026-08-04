"""Typed lease request lifecycle seam used by :class:`DishService`."""
from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, ContextManager, Protocol

from dish_tool.errors import DishRuleError
from dish_tool.operation_execution import live_operation_execution_claim
from dish_tool.results import error_envelope, result_envelope
from dish_tool.transactions import immediate_transaction

from .identifiers import require_asana_gid, require_dish_uuid
from .leases import LeaseManager, ServicePrincipal
from .request_replay import RequestReplayPort


class RequestGate(Protocol):
    def request(self) -> ContextManager[None]: ...


class ShadowCapturePort(Protocol):
    def execute(
        self,
        *,
        command: str,
        arguments: Mapping[str, Any],
        principal: ServicePrincipal,
        request_id: str | None,
        call: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]: ...


class LeaseRequestServicePort(Protocol):
    """Only the facade capabilities needed by lease request orchestration."""

    _maintenance_gate: RequestGate
    _shadow_capture: ShadowCapturePort

    def _initialize_database(self, **kwargs: Any) -> sqlite3.Connection: ...
    def _lease_manager(self, conn: sqlite3.Connection) -> LeaseManager: ...
    def _exposed_operation_view(
        self, conn: sqlite3.Connection, operation_id: str, **kwargs: Any
    ) -> dict[str, Any] | None: ...
    def _apply_expired_lease_guidance(
        self,
        result: dict[str, Any],
        *,
        operation_id: str,
        after_recovery_actions: list[str],
        authoritative_view: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]: ...
    def _lease_payload(self, row: sqlite3.Row | None) -> dict[str, Any] | None: ...
    def _lease_expiry_payload(self, row: sqlite3.Row | None) -> dict[str, Any] | None: ...


class LeaseRequestCoordinator:
    """Own renew, recovery, and expiry request/replay transaction lifecycles."""

    def __init__(
        self,
        service: LeaseRequestServicePort,
        *,
        replay: RequestReplayPort,
        initialization_error: Callable[[BaseException], DishRuleError],
        preserve_error: Callable[..., DishRuleError],
    ) -> None:
        self.service = service
        self.replay = replay
        self.initialization_error = initialization_error
        self.preserve_error = preserve_error

    def renew(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        service = self.service
        return service._shadow_capture.execute(
            command="renew-lease",
            arguments={"operation_id": operation_id},
            principal=principal,
            request_id=request_id,
            call=lambda: self._renew_locked(
                operation_id, principal, request_id=request_id
            ),
        )

    def _renew_locked(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        request_id: str | None,
    ) -> dict[str, Any]:
        service = self.service
        with service._maintenance_gate.request():
            try:
                conn = service._initialize_database(
                    surface="lease",
                    command="renew-lease",
                    request_id=request_id,
                    principal=principal,
                    operation_id=operation_id,
                )
            except Exception as exc:
                return error_envelope(
                    "renew-lease",
                    self.initialization_error(exc),
                    submission_id=operation_id,
                )
            replay_started = False
            try:
                if request_id:
                    row, replay_started = self.replay.begin(
                        conn,
                        request_id=request_id,
                        owner_id=principal.owner_id,
                        run_id=principal.run_id,
                        command="renew-lease",
                        arguments={"operation_id": operation_id},
                    )
                    prior = self.replay.stored(row)
                    if prior is not None:
                        return prior
                    if not replay_started:
                        raise self.replay.pending(
                            "renew-lease", request_id, operation_id=operation_id
                        )
                operation = conn.execute(
                    "SELECT task_gid, status FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if operation is not None and operation["status"] != "open":
                    raise DishRuleError(
                        "WRONG_STATE",
                        "operation is not open",
                        rule="operation_not_open",
                        details={"actual": operation["status"]},
                    )
                leases = service._lease_manager(conn)
                transaction = (
                    immediate_transaction(conn, "renew_service_lease_request")
                    if request_id
                    else contextlib.nullcontext()
                )
                with transaction:
                    row = leases.renew(
                        operation_id,
                        principal,
                        manage_transaction=not bool(request_id),
                    )
                    result = result_envelope(
                        command="renew-lease",
                        task_gid=None if operation is None else operation["task_gid"],
                        submission_id=operation_id,
                        state=None if operation is None else operation["status"],
                        data={"service_lease": service._lease_payload(row)},
                    )
                    if request_id:
                        result.setdefault("data", {})["request_id"] = request_id
                        self.replay.complete(
                            conn, request_id=request_id, result=result
                        )
                    return result
            except DishRuleError as exc:
                exc = self.preserve_error(
                    exc,
                    execution_occurred=True,
                    request_id_consumed=bool(request_id and replay_started),
                )
                operation = conn.execute(
                    "SELECT task_gid, status FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                result = error_envelope(
                    "renew-lease",
                    exc,
                    task_gid=None if operation is None else operation["task_gid"],
                    submission_id=operation_id,
                    state=None if operation is None else operation["status"],
                )
                if exc.rule == "service_lease_expired":
                    view = service._exposed_operation_view(conn, operation_id)
                    result = service._apply_expired_lease_guidance(
                        result,
                        operation_id=operation_id,
                        after_recovery_actions=(
                            list(view.get("legal_actions") or [])
                            if view is not None
                            else []
                        ),
                        authoritative_view=view,
                    )
                if request_id and replay_started:
                    result.setdefault("data", {})["request_id"] = request_id
                    self.replay.complete(conn, request_id=request_id, result=result)
                return result
            finally:
                conn.close()

    def recover(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        reason: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        service = self.service
        return service._shadow_capture.execute(
            command="recover-lease",
            arguments={"operation_id": operation_id, "reason": reason},
            principal=principal,
            request_id=request_id,
            call=lambda: self._recover_locked(
                operation_id,
                principal,
                reason=reason,
                request_id=request_id,
            ),
        )

    def _recover_locked(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        reason: str,
        request_id: str | None,
    ) -> dict[str, Any]:
        service = self.service
        with service._maintenance_gate.request():
            try:
                conn = service._initialize_database(
                    surface="lease",
                    command="recover-lease",
                    request_id=request_id,
                    principal=principal,
                    operation_id=operation_id,
                )
            except Exception as exc:
                return error_envelope(
                    "recover-lease",
                    self.initialization_error(exc),
                    submission_id=operation_id,
                )
            replay_started = False
            try:
                if request_id:
                    row, replay_started = self.replay.begin(
                        conn,
                        request_id=request_id,
                        owner_id=principal.owner_id,
                        run_id=principal.run_id,
                        command="recover-lease",
                        arguments={"operation_id": operation_id, "reason": reason},
                    )
                    prior = self.replay.stored(row)
                    if prior is not None:
                        return prior
                    if not replay_started:
                        raise self.replay.pending("recover-lease", request_id)
                operation = conn.execute(
                    "SELECT task_gid, status FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if operation is None:
                    raise DishRuleError(
                        "NOT_FOUND", "operation not found", rule="operation_not_found"
                    )
                if operation["status"] != "open":
                    raise DishRuleError(
                        "WRONG_STATE",
                        "operation is not open",
                        rule="operation_not_open",
                        details={"actual": operation["status"]},
                    )
                leases = service._lease_manager(conn)
                transaction = (
                    immediate_transaction(conn, "recover_service_lease_request")
                    if request_id
                    else contextlib.nullcontext()
                )
                with transaction:
                    released = leases.admin_recover(
                        operation_id,
                        principal,
                        reason=reason,
                        manage_transaction=not bool(request_id),
                    )
                    if released is None:
                        raise DishRuleError(
                            "CONFLICT",
                            "operation has no active service lease",
                            rule="service_lease_missing",
                        )
                    result = result_envelope(
                        command="recover-lease",
                        task_gid=operation["task_gid"],
                        submission_id=operation_id,
                        state=operation["status"],
                        data={
                            "service_lease": None,
                            "released_lease_id": released["lease_id"],
                            "ownership_transferred": False,
                        },
                    )
                    if request_id:
                        result.setdefault("data", {})["request_id"] = request_id
                        self.replay.complete(
                            conn, request_id=request_id, result=result
                        )
                    return result
            except DishRuleError as exc:
                exc = self.preserve_error(
                    exc,
                    execution_occurred=True,
                    request_id_consumed=bool(request_id and replay_started),
                )
                row = conn.execute(
                    "SELECT task_gid FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                result = error_envelope(
                    "recover-lease",
                    exc,
                    task_gid=None if row is None else row["task_gid"],
                    submission_id=operation_id,
                )
                if request_id and replay_started:
                    result.setdefault("data", {})["request_id"] = request_id
                    self.replay.complete(conn, request_id=request_id, result=result)
                return result
            finally:
                conn.close()

    def expire(
        self,
        principal: ServicePrincipal,
        *,
        lease_id: str | None = None,
        task_gid: str | None = None,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        arguments = {
            **({"lease_id": lease_id} if lease_id is not None else {}),
            **({"task_gid": task_gid} if task_gid is not None else {}),
            "reason": reason,
        }
        service = self.service
        return service._shadow_capture.execute(
            command="expire-lease",
            arguments=arguments,
            principal=principal,
            request_id=request_id,
            call=lambda: self._expire_locked(
                principal,
                lease_id=lease_id,
                task_gid=task_gid,
                reason=reason,
                request_id=request_id,
            ),
        )

    def _expire_locked(
        self,
        principal: ServicePrincipal,
        *,
        lease_id: str | None,
        task_gid: str | None,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        """Release one exact or task-selected lease without changing workflow state."""

        command = "expire-lease"
        if (lease_id is None) == (task_gid is None):
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "exactly one of lease_id and task_gid is required",
                rule="lease_expiry_target_invalid",
                details={"fields": ["lease_id", "task_gid"]},
            )
        request_id = require_dish_uuid(request_id, field="client.request_id")
        if lease_id is not None:
            lease_id = require_dish_uuid(lease_id, field="lease_id")
        if task_gid is not None:
            task_gid = require_asana_gid(task_gid, field="task_gid")
        if not isinstance(reason, str):
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "reason must be a JSON string",
                rule="lease_expiry_reason_invalid",
                details={"field": "reason"},
            )
        reason = reason.strip()
        if not reason:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "lease expiry reason is required",
                rule="lease_expiry_reason_required",
                details={"field": "reason"},
            )
        arguments = {
            **({"lease_id": lease_id} if lease_id is not None else {}),
            **({"task_gid": task_gid} if task_gid is not None else {}),
            "reason": reason,
        }
        service = self.service
        with service._maintenance_gate.request():
            try:
                conn = service._initialize_database(
                    surface="admin-lease-expiry",
                    command=command,
                    request_id=request_id,
                    principal=principal,
                    task_gid=task_gid,
                )
            except Exception as exc:
                result = error_envelope(
                    command, self.initialization_error(exc), task_gid=task_gid
                )
                result["allowed_actions"] = []
                result.setdefault("data", {})["request_id"] = request_id
                return result

            try:
                try:
                    request_row, _started = self.replay.begin(
                        conn,
                        request_id=request_id,
                        owner_id=principal.owner_id,
                        run_id=principal.run_id,
                        command=command,
                        arguments=arguments,
                    )
                except DishRuleError as exc:
                    result = error_envelope(command, exc, task_gid=task_gid)
                    result["allowed_actions"] = []
                    result.setdefault("data", {})["request_id"] = request_id
                    return result

                prior = self.replay.stored(request_row)
                if prior is not None:
                    prior["allowed_actions"] = []
                    return prior

                with immediate_transaction(conn, "expire_service_lease_request"):
                    current_request = conn.execute(
                        "SELECT * FROM service_requests WHERE request_id=?",
                        (request_id,),
                    ).fetchone()
                    prior = self.replay.stored(current_request)
                    if prior is not None:
                        prior["allowed_actions"] = []
                        return prior

                    leases = service._lease_manager(conn)
                    selected = (
                        leases.by_id(lease_id)
                        if lease_id is not None
                        else leases.active_for_task(str(task_gid))
                    )

                    operation = None
                    if selected is not None:
                        operation = conn.execute(
                            "SELECT task_gid,status FROM operations WHERE operation_id=?",
                            (selected["operation_id"],),
                        ).fetchone()
                        if operation is None:
                            raise DishRuleError(
                                "INTERNAL_ERROR",
                                "service lease lost its operation",
                                rule="service_lease_operation_missing",
                                details={"lease_id": selected["lease_id"]},
                            )

                    if selected is None and lease_id is not None:
                        result = error_envelope(
                            command,
                            DishRuleError(
                                "NOT_FOUND",
                                "service lease not found",
                                rule="service_lease_not_found",
                                details={"lease_id": lease_id},
                            ),
                        )
                    elif selected is None:
                        result = result_envelope(
                            command=command,
                            task_gid=task_gid,
                            allowed_actions=[],
                            data={
                                "outcome": "no_active_lease",
                                "released": False,
                                "lease": None,
                                "ownership_transferred": False,
                                "request_id": request_id,
                            },
                        )
                    elif selected["released_at"] is not None:
                        result = result_envelope(
                            command=command,
                            task_gid=selected["task_gid"],
                            submission_id=selected["operation_id"],
                            state=operation["status"],
                            allowed_actions=[],
                            data={
                                "outcome": "already_released",
                                "released": False,
                                "lease": service._lease_expiry_payload(selected),
                                "ownership_transferred": False,
                                "request_id": request_id,
                            },
                        )
                    else:
                        claim = live_operation_execution_claim(
                            conn, operation_id=selected["operation_id"]
                        )
                        if claim is not None:
                            result = error_envelope(
                                command,
                                DishRuleError(
                                    "CONFLICT",
                                    "another mutation is already executing for this operation",
                                    rule="operation_mutation_in_progress",
                                    retryable=True,
                                    details={
                                        "operation_id": selected["operation_id"],
                                        "command": claim["command"],
                                        "acquired_at": claim["acquired_at"],
                                    },
                                ),
                                task_gid=selected["task_gid"],
                                submission_id=selected["operation_id"],
                                state=operation["status"],
                            )
                        else:
                            released, changed = leases.admin_expire_selected(
                                selected["lease_id"],
                                reason=f"admin expiry: {reason}",
                                manage_transaction=False,
                            )
                            result = result_envelope(
                                command=command,
                                task_gid=released["task_gid"],
                                submission_id=released["operation_id"],
                                state=operation["status"],
                                allowed_actions=[],
                                data={
                                    "outcome": (
                                        "released" if changed else "already_released"
                                    ),
                                    "released": changed,
                                    "lease": service._lease_expiry_payload(released),
                                    "ownership_transferred": False,
                                    "request_id": request_id,
                                },
                            )

                    result["allowed_actions"] = []
                    result.setdefault("data", {})["request_id"] = request_id
                    self.replay.complete(conn, request_id=request_id, result=result)
                    return result
            finally:
                conn.close()
