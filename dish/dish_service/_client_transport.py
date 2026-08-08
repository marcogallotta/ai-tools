"""Private HTTP/JSON transport for Dish service clients."""
from __future__ import annotations

import http.client
import json
from typing import Any, Mapping
from urllib.parse import urlsplit

from dish_tool.errors import DishRuleError


class AmbiguousResponseError(Exception):
    """The request may have been sent, but no trustworthy response was received."""


class HTTPJSONTransport:
    """Narrow HTTP transport shared by the service-client façades."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str,
        connect_timeout: float,
        response_timeout: float,
    ) -> None:
        self.base_url = base_url
        self.token = token
        self.connect_timeout = connect_timeout
        self.response_timeout = response_timeout

    def _request_bytes(
        self,
        path: str,
        *,
        method: str,
        payload: Mapping[str, Any] | None,
        ambiguous_after_dispatch: bool,
    ) -> tuple[http.client.HTTPResponse, bytes]:
        body = (
            None
            if payload is None
            else json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
        )
        url = urlsplit(f"{self.base_url}{path}")
        target = url.path or "/"
        if url.query:
            target = f"{target}?{url.query}"
        connection_cls = (
            http.client.HTTPSConnection
            if url.scheme == "https"
            else http.client.HTTPConnection
        )
        # The connect phase and the response wait need independent bounds: a
        # command may legitimately perform several sequential upstream calls
        # before responding, so the response wait must be long, but a dead or
        # unreachable service should fail fast on connect.
        connection = connection_cls(
            url.hostname, url.port, timeout=self.connect_timeout
        )
        try:
            try:
                connection.connect()
                connection.sock.settimeout(self.response_timeout)
            except (OSError, http.client.HTTPException) as exc:
                raise DishRuleError(
                    "BACKEND_REJECTED",
                    "dish service is unavailable",
                    rule="service_unavailable",
                    retryable=True,
                ) from exc
            try:
                connection.request(
                    method,
                    target,
                    body=body,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self.token}",
                    },
                )
                response = connection.getresponse()
                raw = response.read()
            except (OSError, http.client.HTTPException) as exc:
                if ambiguous_after_dispatch:
                    raise AmbiguousResponseError from exc
                raise DishRuleError(
                    "BACKEND_REJECTED",
                    "dish service is unavailable",
                    rule="service_unavailable",
                    retryable=True,
                ) from exc
        finally:
            connection.close()
        return response, raw

    def request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
        ambiguous_after_dispatch: bool = False,
    ) -> Any:
        response, raw = self._request_bytes(
            path,
            method=method,
            payload=payload,
            ambiguous_after_dispatch=ambiguous_after_dispatch,
        )
        if response.status >= 400:
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception as parse_exc:
                raise DishRuleError(
                    "INTERNAL_ERROR",
                    "dish service returned an unreadable error",
                    rule="service_response_invalid",
                ) from parse_exc
        return json.loads(raw.decode("utf-8"))

    def request_json_ignoring_status(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
        ambiguous_after_dispatch: bool = False,
    ) -> Any:
        _response, raw = self._request_bytes(
            path,
            method=method,
            payload=payload,
            ambiguous_after_dispatch=ambiguous_after_dispatch,
        )
        return json.loads(raw.decode("utf-8"))
