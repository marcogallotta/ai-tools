"""HTTP client façades for the canonical shared-service result contract."""
from __future__ import annotations

import math
import uuid
from pathlib import Path
from typing import Any, Mapping

from dish_tool.errors import DishRuleError
from dish_tool.identifiers import require_asana_gid, require_dish_uuid

from . import _client_transport
from ._client_ambiguity import (
    command_result_request,
    expire_lease_result_request,
    request_id_for_command,
)
from ._client_results import require_result_envelope


class DishServiceClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        run_id: str,
        connect_timeout: float = 10.0,
        response_timeout: float = 600.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        for label, value in (
            ("connect", connect_timeout),
            ("response", response_timeout),
        ):
            if (
                not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    f"service {label} timeout must be a finite positive number",
                    rule="service_timeout_invalid",
                )
        self.connect_timeout = float(connect_timeout)
        self.response_timeout = float(response_timeout)
        self.token = str(token or "").strip()
        self.run_id = str(run_id or "").strip()
        if not self.token:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "service bearer token is required",
                rule="service_token_required",
            )
        if not self.run_id:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "service run identity is required",
                rule="service_run_required",
            )
        self._transport = _client_transport.HTTPJSONTransport(
            self.base_url,
            token=self.token,
            connect_timeout=self.connect_timeout,
            response_timeout=self.response_timeout,
        )

    def _client(self, *, request_id: str | None = None) -> dict[str, str]:
        client = {"run_id": self.run_id}
        if request_id is not None:
            client["request_id"] = request_id
        return client

    def _result_request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
        ambiguous_after_dispatch: bool = False,
    ) -> dict[str, Any]:
        return require_result_envelope(
            self._transport.request_json(
                path,
                method=method,
                payload=payload,
                ambiguous_after_dispatch=ambiguous_after_dispatch,
            )
        )

    def health(self) -> dict[str, Any]:
        return self._transport.request_json("/health")

    @staticmethod
    def _transport_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
        prepared = dict(arguments)
        file_path = prepared.pop("file_path", None)
        if file_path:
            prepared["file_text"] = Path(str(file_path)).read_text(encoding="utf-8")
        for field in (
            "intent_challenge_id",
            "intent_basis",
            "override_reason",
            "lease_id",
        ):
            if prepared.get(field) is None:
                prepared.pop(field, None)
        return prepared

    def execute(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
        **keyword_arguments: Any,
    ) -> dict[str, Any]:
        if arguments is not None and keyword_arguments:
            raise TypeError(
                "provide command arguments as a mapping or keywords, not both"
            )
        prepared = dict(arguments or keyword_arguments)
        request_id = request_id_for_command(command, request_id)
        return command_result_request(
            command=command,
            path=f"/v1/commands/{command}",
            request_id=request_id,
            payload={
                "arguments": self._transport_arguments(prepared),
                "client": self._client(request_id=request_id),
            },
            run_id=self.run_id,
            result_request=self._result_request,
        )

    def record_argument_failure(
        self,
        command: str,
        error: DishRuleError,
        *,
        agent: str | None = None,
        task_gid: str | None = None,
        submission_id: str | None = None,
    ) -> dict[str, Any]:
        return self._result_request(
            f"/v1/argument-failures/{command}",
            method="POST",
            payload={
                "client": self._client(),
                "error": {
                    "code": error.code,
                    "message": str(error),
                    "rule": error.rule,
                    "retryable": error.retryable,
                    "details": dict(error.details),
                    "errors": [dict(item) for item in error.errors],
                },
                "context": {
                    "agent": agent,
                    "task_gid": task_gid,
                    "submission_id": submission_id,
                },
            },
        )

    def renew_lease(
        self, operation_id: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        request_id = request_id_for_command("renew-lease", request_id)
        return self._result_request(
            f"/v1/leases/{operation_id}/renew",
            method="POST",
            payload={"client": self._client(request_id=request_id)},
        )


class DishAdminServiceClient(DishServiceClient):
    def record_argument_failure(
        self,
        command: str,
        error: DishRuleError,
        *,
        agent: str | None = None,
        task_gid: str | None = None,
        submission_id: str | None = None,
    ) -> dict[str, Any]:
        return self._result_request(
            f"/v1/admin/argument-failures/{command}",
            method="POST",
            payload={
                "client": self._client(),
                "error": {
                    "code": error.code,
                    "message": str(error),
                    "rule": error.rule,
                    "retryable": error.retryable,
                    "details": dict(error.details),
                    "errors": [dict(item) for item in error.errors],
                },
                "context": {"submission_id": submission_id},
            },
        )

    def execute(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
        **keyword_arguments: Any,
    ) -> dict[str, Any]:
        if arguments is not None and keyword_arguments:
            raise TypeError(
                "provide command arguments as a mapping or keywords, not both"
            )
        prepared = dict(arguments or keyword_arguments)
        if request_id is None:
            request_id = str(uuid.uuid4())
        return self._result_request(
            f"/v1/admin/{command}",
            method="POST",
            payload={
                "arguments": self._transport_arguments(prepared),
                "client": self._client(request_id=request_id),
            },
        )

    def recover_lease(
        self,
        operation_id: str,
        *,
        reason: str,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        operation_id = require_dish_uuid(operation_id, field="operation_id")
        if request_id is None:
            request_id = str(uuid.uuid4())
        return self._result_request(
            f"/v1/admin/leases/{operation_id}/recover",
            method="POST",
            payload={
                "reason": reason,
                "client": self._client(request_id=request_id),
            },
        )

    def expire_lease(
        self,
        *,
        lease_id: str | None = None,
        task_gid: str | None = None,
        reason: str,
        request_id: str,
    ) -> dict[str, Any]:
        if (lease_id is None) == (task_gid is None):
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "exactly one of lease_id and task_gid is required",
                rule="lease_expiry_target_invalid",
            )
        if lease_id is not None:
            lease_id = require_dish_uuid(lease_id, field="lease_id")
        if task_gid is not None:
            task_gid = require_asana_gid(task_gid, field="task_gid")
        request_id = require_dish_uuid(request_id, field="client.request_id")
        if not isinstance(reason, str):
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "reason must be a string",
                rule="lease_expiry_reason_invalid",
                details={"field": "reason"},
            )
        clean_reason = reason.strip()
        if not clean_reason:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "lease expiry reason is required",
                rule="lease_expiry_reason_required",
                details={"field": "reason"},
            )
        payload = {
            **({"lease_id": lease_id} if lease_id is not None else {}),
            **({"task_gid": task_gid} if task_gid is not None else {}),
            "reason": clean_reason,
            "client": self._client(request_id=request_id),
        }
        return expire_lease_result_request(
            payload=payload,
            request_id=request_id,
            task_gid=task_gid,
            run_id=self.run_id,
            request_json=self._transport.request_json_ignoring_status,
        )

    def create_backup(
        self, *, label: str = "manual", request_id: str | None = None
    ) -> dict[str, Any]:
        if request_id is None:
            request_id = str(uuid.uuid4())
        return self._result_request(
            "/v1/admin/backups/create",
            method="POST",
            payload={
                "label": label,
                "client": self._client(request_id=request_id),
            },
        )

    def restore_backup(
        self, backup_id: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        if request_id is None:
            request_id = str(uuid.uuid4())
        return self._result_request(
            "/v1/admin/backups/restore",
            method="POST",
            payload={
                "backup_id": backup_id,
                "client": self._client(request_id=request_id),
            },
        )


class DishActionClient(DishServiceClient):
    def execute(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        request_id: str | None = None,
        **keyword_arguments: Any,
    ) -> dict[str, Any]:
        if arguments is not None and keyword_arguments:
            raise TypeError(
                "provide command arguments as a mapping or keywords, not both"
            )
        prepared = dict(arguments or keyword_arguments)
        request_id = request_id_for_command(command, request_id)
        return command_result_request(
            command=command,
            path=f"/v1/action/{command}",
            request_id=request_id,
            payload={
                "arguments": self._transport_arguments(prepared),
                "client": self._client(request_id=request_id),
            },
            run_id=self.run_id,
            result_request=self._result_request,
        )

    def renew_lease(
        self, operation_id: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        request_id = request_id_for_command("renew-lease", request_id)
        return self._result_request(
            "/v1/action/renew-lease",
            method="POST",
            payload={
                "arguments": {"operation_id": operation_id},
                "client": self._client(request_id=request_id),
            },
        )
