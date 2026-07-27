"""HTTP client for the canonical shared-service result contract."""
from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dish_tool.errors import DishRuleError


class DishServiceClient:
    def __init__(
        self, base_url: str, *, timeout: float = 65.0,
        owner_id: str | None = None, run_id: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.owner_id = owner_id
        self.run_id = run_id

    def _client_payload(self) -> dict[str, str] | None:
        if self.owner_id is None and self.run_id is None:
            return None
        if not self.owner_id or not self.run_id:
            raise DishRuleError("INVALID_ARGUMENT", "both service owner and run identities are required", rule="service_principal_incomplete")
        return {"owner_id": self.owner_id, "run_id": self.run_id}

    def _json_request(self, path: str, *, method: str = "GET", payload: Mapping[str, Any] | None = None):
        body = None if payload is None else json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
        request = Request(
            f"{self.base_url}{path}", data=body, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                return json.loads(exc.read().decode("utf-8"))
            except Exception as parse_exc:
                raise DishRuleError("INTERNAL_ERROR", "dish service returned an unreadable error", rule="service_response_invalid") from parse_exc
        except URLError as exc:
            raise DishRuleError("BACKEND_REJECTED", "dish service is unavailable", rule="service_unavailable", retryable=True) from exc

    def health(self) -> dict[str, Any]:
        return self._json_request("/health")

    def execute(self, command: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {"arguments": dict(arguments)}
        client = self._client_payload()
        if client is not None:
            payload["client"] = client
        return self._json_request(f"/v1/commands/{command}", method="POST", payload=payload)

    def renew_lease(self, operation_id: str) -> dict[str, Any]:
        client = self._client_payload()
        if client is None:
            raise DishRuleError("INVALID_ARGUMENT", "service client identity is required", rule="service_principal_required")
        return self._json_request(
            f"/v1/leases/{operation_id}/renew", method="POST", payload={"client": client}
        )
