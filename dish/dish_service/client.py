"""HTTP client for the canonical shared-service result contract."""
from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dish_tool.errors import DishRuleError


class DishServiceClient:
    def __init__(self, base_url: str, *, timeout: float = 65.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

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
        return self._json_request(f"/v1/commands/{command}", method="POST", payload={"arguments": dict(arguments)})
