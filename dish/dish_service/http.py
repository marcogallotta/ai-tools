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
from .auth import authenticate_bearer
from .leases import ServicePrincipal
from .openapi import ACTION_COMMANDS, action_openapi

LOG = logging.getLogger("dish.service")


class DishHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service: DishService, *, surface_mode: str = "combined"):
        if surface_mode not in {"combined", "private", "action"}:
            raise ValueError("invalid HTTP surface mode")
        self.service = service
        self.surface_mode = surface_mode
        super().__init__(address, DishRequestHandler)


class DishRequestHandler(BaseHTTPRequestHandler):
    server: DishHTTPServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.server.service.config.request_timeout_seconds)

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.info("http_request remote=%s message=%s", self.client_address[0], fmt % args)

    def _write_json(
        self, status: int, payload: Any, *, close_connection: bool = False
    ) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if close_connection:
            self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if close_connection:
            self.send_header("Connection", "close")
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
                "INVALID_ARGUMENT", "request body exceeds the service limit",
                rule="request_too_large", details={"max_body_bytes": limit},
            )
        try:
            body = self.rfile.read(length)
        except socket.timeout as exc:
            raise DishRuleError("BACKEND_REJECTED", "request body timed out", rule="request_timeout", retryable=True) from exc
        self._request_body_consumed = len(body) == length
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DishRuleError("INVALID_ARGUMENT", "request body must be UTF-8 JSON", rule="request_json_invalid") from exc
        if not isinstance(value, dict):
            raise DishRuleError("INVALID_ARGUMENT", "request body must be a JSON object", rule="request_object_required")
        return value

    def _tokens(self) -> dict[str, tuple[str, str]]:
        config = self.server.service.config
        result: dict[str, tuple[str, str]] = {}
        if config.agent_token:
            result[config.agent_token] = ("cli", "agent")
        if config.admin_token:
            result[config.admin_token] = ("marco-admin", "admin")
        if config.action_token:
            result[config.action_token] = ("gpt-action", "action")
        return result

    def _credential(self, *scopes: str):
        return authenticate_bearer(
            self.headers.get("Authorization"), tokens=self._tokens(), allowed_scopes=scopes
        )

    @staticmethod
    def _principal(credential, request: dict[str, Any]) -> ServicePrincipal:
        client = request.get("client")
        if not isinstance(client, dict):
            raise DishRuleError(
                "INVALID_ARGUMENT", "client run identity is required", rule="service_client_required"
            )
        return ServicePrincipal.from_values(credential.client_id, client.get("run_id"))

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/health" and self.server.surface_mode != "action":
            payload = self.server.service.health()
            self._write_json(HTTPStatus.OK if payload["ok"] else HTTPStatus.SERVICE_UNAVAILABLE, payload)
            return
        if path == "/openapi/action.json":
            host = self.headers.get("Host") or "dish.example.invalid"
            self._write_json(HTTPStatus.OK, action_openapi(server_url=f"https://{host}"))
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        started = time.monotonic()
        self._request_body_consumed = False
        path = urlsplit(self.path).path
        parts = [part for part in path.split("/") if part]
        command = "unknown"
        surface = "unknown"
        if len(parts) == 3 and parts[:2] == ["v1", "commands"]:
            surface, command = "agent", parts[2]
        elif len(parts) == 3 and parts[:2] == ["v1", "action"]:
            surface, command = "action", parts[2]
        elif len(parts) == 5 and parts[:3] == ["v1", "action", "leases"] and parts[4] == "renew":
            surface, command = "action-lease", "renew-lease"
        elif len(parts) == 4 and parts[:2] == ["v1", "leases"] and parts[3] == "renew":
            surface, command = "lease", "renew-lease"
        elif len(parts) == 3 and parts[:2] == ["v1", "admin"]:
            surface, command = "admin", parts[2]
        elif len(parts) == 5 and parts[:3] == ["v1", "admin", "leases"] and parts[4] == "recover":
            surface, command = "admin-lease", "recover-lease"
        elif parts == ["v1", "admin", "backups", "create"]:
            surface, command = "admin-backup", "backup-create"
        elif parts == ["v1", "admin", "backups", "restore"]:
            surface, command = "admin-backup", "backup-restore"
        elif len(parts) == 4 and parts[:3] == ["v1", "admin", "argument-failures"]:
            surface, command = "admin-argument-failure", parts[3]
        elif len(parts) == 3 and parts[:2] == ["v1", "argument-failures"]:
            surface, command = "argument-failure", parts[2]
        try:
            if command == "unknown":
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": "not_found"},
                    close_connection=True,
                )
                return
            if self.server.surface_mode == "private" and surface in {"action", "action-lease"}:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": "not_found"},
                    close_connection=True,
                )
                return
            if self.server.surface_mode == "action" and surface not in {"action", "action-lease"}:
                self._write_json(
                    HTTPStatus.NOT_FOUND,
                    {"ok": False, "error": "not_found"},
                    close_connection=True,
                )
                return
            if surface in {"agent", "lease", "argument-failure"}:
                credential = self._credential("agent")
            elif surface in {"action", "action-lease"}:
                credential = self._credential("action")
                if surface == "action" and command not in ACTION_COMMANDS:
                    raise DishRuleError("INVALID_ARGUMENT", "command is not exposed to the GPT Action", rule="action_command_forbidden")
            else:
                credential = self._credential("admin")
            request = self._read_json()
            principal = self._principal(credential, request)
            if surface == "lease":
                payload = self.server.service.renew_lease(parts[2], principal)
            elif surface == "action-lease":
                payload = self.server.service.renew_lease(parts[3], principal)
            elif surface == "admin-lease":
                reason = str(request.get("reason") or "").strip()
                if not reason:
                    raise DishRuleError("INVALID_ARGUMENT", "recovery reason is required", rule="recovery_reason_required")
                payload = self.server.service.recover_lease(parts[3], principal, reason=reason)
            elif surface == "admin-backup":
                if command == "backup-create":
                    payload = self.server.service.create_backup(label=str(request.get("label") or "manual"))
                else:
                    payload = self.server.service.restore_backup(str(request.get("backup_id") or ""))
            elif surface == "admin":
                arguments = request.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise DishRuleError("INVALID_ARGUMENT", "arguments must be a JSON object", rule="arguments_object_required")
                payload = self.server.service.execute_admin(command, arguments, principal=principal)
            elif surface in {"argument-failure", "admin-argument-failure"}:
                error = request.get("error")
                context = request.get("context", {})
                if not isinstance(error, dict) or not isinstance(context, dict):
                    raise DishRuleError("INVALID_ARGUMENT", "argument failure payload is invalid", rule="argument_failure_invalid")
                if surface == "admin-argument-failure":
                    payload = self.server.service.record_admin_argument_failure(command, error, context)
                else:
                    payload = self.server.service.record_agent_argument_failure(command, error, context)
            else:
                arguments = request.get("arguments", {})
                if not isinstance(arguments, dict):
                    raise DishRuleError("INVALID_ARGUMENT", "arguments must be a JSON object", rule="arguments_object_required")
                payload = self.server.service.execute_agent(command, arguments, principal=principal)
            self._write_json(HTTPStatus.OK, payload)
        except DishRuleError as exc:
            status = HTTPStatus.UNAUTHORIZED if exc.rule in {"service_auth_required", "service_auth_invalid"} else (
                HTTPStatus.FORBIDDEN if exc.rule == "service_scope_forbidden" else HTTPStatus.BAD_REQUEST
            )
            self._write_json(
                status,
                error_envelope(command, exc),
                close_connection=not self._request_body_consumed,
            )
        finally:
            LOG.info("command_complete surface=%s command=%s elapsed_ms=%d", surface, command, int((time.monotonic() - started) * 1000))


def build_server(service: DishService) -> DishHTTPServer:
    """Combined listener retained for hermetic tests and local development."""
    config = service.config
    return DishHTTPServer((config.bind_host, config.port), service, surface_mode="combined")


def build_private_server(service: DishService) -> DishHTTPServer:
    config = service.config
    return DishHTTPServer((config.bind_host, config.port), service, surface_mode="private")


def build_action_server(service: DishService) -> DishHTTPServer:
    config = service.config
    return DishHTTPServer(
        (config.action_bind_host, config.action_port), service, surface_mode="action"
    )
