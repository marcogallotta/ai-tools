"""Small stdlib HTTP transport for the shared dish service."""
from __future__ import annotations

import json
import logging
import socket
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope

from .application import DishService

LOG = logging.getLogger("dish.service")


class DishHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service: DishService):
        self.service = service
        super().__init__(address, DishRequestHandler)


class DishRequestHandler(BaseHTTPRequestHandler):
    server: DishHTTPServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.server.service.config.request_timeout_seconds)

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("http_request remote=%s message=%s", self.client_address[0], fmt % args)

    def _write_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise DishRuleError("INVALID_ARGUMENT", "Content-Length is required", rule="content_length_required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise DishRuleError("INVALID_ARGUMENT", "Content-Length is invalid", rule="content_length_invalid") from exc
        limit = self.server.service.config.max_body_bytes
        if length < 0 or length > limit:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "request body exceeds the service limit",
                rule="request_too_large",
                details={"max_body_bytes": limit},
            )
        try:
            body = self.rfile.read(length)
        except socket.timeout as exc:
            raise DishRuleError("BACKEND_REJECTED", "request body timed out", rule="request_timeout", retryable=True) from exc
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DishRuleError("INVALID_ARGUMENT", "request body must be UTF-8 JSON", rule="request_json_invalid") from exc
        if not isinstance(value, dict):
            raise DishRuleError("INVALID_ARGUMENT", "request body must be a JSON object", rule="request_object_required")
        return value

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health":
            payload = self.server.service.health()
            self._write_json(HTTPStatus.OK if payload["ok"] else HTTPStatus.SERVICE_UNAVAILABLE, payload)
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        started = time.monotonic()
        path = urlsplit(self.path).path
        parts = [part for part in path.split("/") if part]
        command = parts[2] if len(parts) == 3 and parts[:2] == ["v1", "commands"] else "unknown"
        try:
            if command == "unknown":
                self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})
                return
            request = self._read_json()
            arguments = request.get("arguments", {})
            if not isinstance(arguments, dict):
                raise DishRuleError("INVALID_ARGUMENT", "arguments must be a JSON object", rule="arguments_object_required")
            payload = self.server.service.execute_agent(command, arguments)
            self._write_json(HTTPStatus.OK, payload)
        except DishRuleError as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, error_envelope(command, exc))
        finally:
            LOG.info("command_complete command=%s elapsed_ms=%d", command, int((time.monotonic() - started) * 1000))


def build_server(service: DishService) -> DishHTTPServer:
    config = service.config
    return DishHTTPServer((config.bind_host, config.port), service)
