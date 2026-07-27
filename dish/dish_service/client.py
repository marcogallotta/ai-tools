"""HTTP clients for the canonical shared-service result contract."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope


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
        self.timeout = timeout
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
            try:
                return json.loads(exc.read().decode("utf-8"))
            except Exception as parse_exc:
                raise DishRuleError(
                    "INTERNAL_ERROR",
                    "dish service returned an unreadable error",
                    rule="service_response_invalid",
                ) from parse_exc
        except URLError as exc:
            raise DishRuleError(
                "BACKEND_REJECTED",
                "dish service is unavailable",
                rule="service_unavailable",
                retryable=True,
            ) from exc

    def _client(self) -> dict[str, str]:
        return {"run_id": self.run_id}

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
        **keyword_arguments: Any,
    ) -> dict[str, Any]:
        if arguments is not None and keyword_arguments:
            raise TypeError("provide command arguments as a mapping or keywords, not both")
        prepared = dict(arguments or keyword_arguments)
        return self._json_request(
            f"/v1/commands/{command}",
            method="POST",
            payload={"arguments": self._transport_arguments(prepared), "client": self._client()},
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

    def renew_lease(self, operation_id: str) -> dict[str, Any]:
        return self._json_request(
            f"/v1/leases/{operation_id}/renew",
            method="POST",
            payload={"client": self._client()},
        )


class DishAdminServiceClient(DishServiceClient):
    def execute(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
        **keyword_arguments: Any,
    ) -> dict[str, Any]:
        if arguments is not None and keyword_arguments:
            raise TypeError("provide command arguments as a mapping or keywords, not both")
        prepared = dict(arguments or keyword_arguments)
        return self._json_request(
            f"/v1/admin/{command}",
            method="POST",
            payload={"arguments": self._transport_arguments(prepared), "client": self._client()},
        )

    def recover_lease(self, operation_id: str, *, reason: str) -> dict[str, Any]:
        return self._json_request(
            f"/v1/admin/leases/{operation_id}/recover",
            method="POST",
            payload={"reason": reason, "client": self._client()},
        )


class DishActionClient(DishServiceClient):
    def execute(
        self,
        command: str,
        arguments: Mapping[str, Any] | None = None,
        **keyword_arguments: Any,
    ) -> dict[str, Any]:
        if arguments is not None and keyword_arguments:
            raise TypeError("provide command arguments as a mapping or keywords, not both")
        prepared = dict(arguments or keyword_arguments)
        return self._json_request(
            f"/v1/action/{command}",
            method="POST",
            payload={"arguments": self._transport_arguments(prepared), "client": self._client()},
        )

    def renew_lease(self, operation_id: str) -> dict[str, Any]:
        return self._json_request(
            f"/v1/action/leases/{operation_id}/renew",
            method="POST",
            payload={"client": self._client()},
        )
