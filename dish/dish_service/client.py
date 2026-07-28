"""HTTP clients for the canonical shared-service result contract."""
from __future__ import annotations

import http.client
import json
import math
import socket
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope
from .identifiers import require_dish_uuid


class DishServiceClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        run_id: str,
        timeout: float = 65.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "service request timeout must be a finite positive number",
                rule="service_timeout_invalid",
            )
        self.timeout = float(timeout)
        self.token = str(token or "").strip()
        self.run_id = str(run_id or "").strip()
        if not self.token:
            raise DishRuleError("INVALID_ARGUMENT", "service bearer token is required", rule="service_token_required")
        if not self.run_id:
            raise DishRuleError("INVALID_ARGUMENT", "service run identity is required", rule="service_run_required")

    def _json_request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
    ):
        body = None if payload is None else json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}",
            data=body,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            with exc:
                try:
                    return json.loads(exc.read().decode("utf-8"))
                except Exception as parse_exc:
                    raise DishRuleError(
                        "INTERNAL_ERROR",
                        "dish service returned an unreadable error",
                        rule="service_response_invalid",
                    ) from parse_exc
        except (
            URLError,
            http.client.RemoteDisconnected,
            ConnectionResetError,
            TimeoutError,
            socket.timeout,
        ) as exc:
            raise DishRuleError(
                "BACKEND_REJECTED",
                "dish service is unavailable",
                rule="service_unavailable",
                retryable=True,
            ) from exc

    def _client(self, *, request_id: str | None = None) -> dict[str, str]:
        client = {"run_id": self.run_id}
        if request_id is not None:
            client["request_id"] = request_id
        return client

    def health(self) -> dict[str, Any]:
        return self._json_request("/health")

    @staticmethod
    def _transport_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
        prepared = dict(arguments)
        file_path = prepared.pop("file_path", None)
        if file_path:
            prepared["file_text"] = Path(str(file_path)).read_text(encoding="utf-8")
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
            raise TypeError("provide command arguments as a mapping or keywords, not both")
        prepared = dict(arguments or keyword_arguments)
        if command in {"create", "start", "prepare", "approve", "reject", "submit"} and request_id is None:
            request_id = str(uuid.uuid4())
        return self._json_request(
            f"/v1/commands/{command}",
            method="POST",
            payload={
                "arguments": self._transport_arguments(prepared),
                "client": self._client(request_id=request_id),
            },
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
        return self._json_request(
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
        if request_id is None:
            request_id = str(uuid.uuid4())
        return self._json_request(
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
        return self._json_request(
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
            raise TypeError("provide command arguments as a mapping or keywords, not both")
        prepared = dict(arguments or keyword_arguments)
        if request_id is None:
            request_id = str(uuid.uuid4())
        return self._json_request(
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
        return self._json_request(
            f"/v1/admin/leases/{operation_id}/recover",
            method="POST",
            payload={
                "reason": reason,
                "client": self._client(request_id=request_id),
            },
        )

    def create_backup(
        self, *, label: str = "manual", request_id: str | None = None
    ) -> dict[str, Any]:
        if request_id is None:
            request_id = str(uuid.uuid4())
        return self._json_request(
            "/v1/admin/backups/create",
            method="POST",
            payload={"label": label, "client": self._client(request_id=request_id)},
        )

    def restore_backup(
        self, backup_id: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        if request_id is None:
            request_id = str(uuid.uuid4())
        return self._json_request(
            "/v1/admin/backups/restore",
            method="POST",
            payload={"backup_id": backup_id, "client": self._client(request_id=request_id)},
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
            raise TypeError("provide command arguments as a mapping or keywords, not both")
        prepared = dict(arguments or keyword_arguments)
        if command in {"create", "start", "prepare", "approve", "reject", "submit"} and request_id is None:
            request_id = str(uuid.uuid4())
        return self._json_request(
            f"/v1/action/{command}",
            method="POST",
            payload={
                "arguments": self._transport_arguments(prepared),
                "client": self._client(request_id=request_id),
            },
        )

    def renew_lease(
        self, operation_id: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        if request_id is None:
            request_id = str(uuid.uuid4())
        return self._json_request(
            f"/v1/action/leases/{operation_id}/renew",
            method="POST",
            payload={"client": self._client(request_id=request_id)},
        )
