"""Small stdlib HTTP transport for the shared dish service."""
from __future__ import annotations

import json
import logging
import socket
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope

from .application import DishService
from .auth import authenticate_bearer
from .identifiers import require_dish_uuid, validate_identifier_fields
from .leases import ServicePrincipal
from .command_spec import ACTION_COMMANDS, REPLAY_CAPABLE_COMMANDS, REPLAY_SAFE_COMMANDS, validate_action_request
from .openapi import action_openapi

LOG = logging.getLogger("dish.service")


class DishHTTPServer(ThreadingHTTPServer):
    # Request handlers may own a database transaction or an in-flight Asana
    # mutation.  They must be drained before process exit rather than abandoned
    # as daemon threads.
    daemon_threads = False
    block_on_close = True

    def __init__(self, address, service: DishService, *, surface_mode: str = "combined"):
        if surface_mode not in {"combined", "private", "action"}:
            raise ValueError("invalid HTTP surface mode")
        self.service = service
        self.surface_mode = surface_mode
        service.config.validate_runtime(require_action=surface_mode == "action" or (
            surface_mode == "combined" and service.config.action_token is not None
        ))
        super().__init__(address, DishRequestHandler)

    def serve_forever(self, poll_interval: float = 0.05) -> None:
        super().serve_forever(poll_interval=poll_interval)


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
        run_id = client.get("run_id")
        require_dish_uuid(run_id, field="client.run_id")
        return ServicePrincipal.from_values(credential.client_id, run_id)

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
        request = {}
        principal = None
        request_id = None
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
            if surface == "action":
                principal = self._principal(credential, request)
                _client, arguments = validate_action_request(command, request)
                request = {"client": _client, "arguments": arguments}
            elif surface == "action-lease":
                extras = sorted(set(request) - {"client"})
                if extras:
                    raise DishRuleError(
                        "INVALID_ARGUMENT",
                        "request contains an unexpected field",
                        rule="request_field_unexpected",
                        details={"field": extras[0]},
                    )
            if surface in {"lease", "action-lease", "admin-lease"}:
                operation_id = parts[2] if surface == "lease" else parts[3]
                require_dish_uuid(operation_id, field="operation_id")
            arguments = request.get("arguments")
            if isinstance(arguments, dict):
                validate_identifier_fields(arguments)
            context = request.get("context")
            if isinstance(context, dict):
                validate_identifier_fields(context, allow_null=True)
            if principal is None:
                principal = self._principal(credential, request)
            client_payload = request.get("client") if isinstance(request.get("client"), dict) else {}
            request_id = client_payload.get("request_id")
            if request_id is not None:
                require_dish_uuid(request_id, field="client.request_id")
            if surface == "agent" and command in REPLAY_SAFE_COMMANDS:
                if not isinstance(request_id, str) or not request_id.strip():
                    raise DishRuleError(
                        "INVALID_ARGUMENT",
                        "client.request_id is required for replay-sensitive mutations",
                        rule="request_field_required",
                        details={"field": "client.request_id"},
                    )
                require_dish_uuid(request_id, field="client.request_id")
            if surface == "lease":
                payload = self.server.service.renew_lease(parts[2], principal)
            elif surface == "action-lease":
                payload = self.server.service.renew_lease(parts[3], principal)
            elif surface == "admin-lease":
                reason = str(request.get("reason") or "").strip()
                if not reason:
                    raise DishRuleError("INVALID_ARGUMENT", "recovery reason is required", rule="recovery_reason_required")
                payload = self.server.service.recover_lease(parts[3], principal, reason=reason, request_id=request_id)
            elif surface == "admin-backup":
                if command == "backup-create":
                    payload = self.server.service.create_backup(label=str(request.get("label") or "manual"))
                else:
                    payload = self.server.service.restore_backup(str(request.get("backup_id") or ""))
            elif surface == "admin":
                if "arguments" not in request:
                    raise DishRuleError(
                        "INVALID_ARGUMENT",
                        "arguments are required",
                        rule="arguments_object_required",
                        details={"field": "arguments"},
                    )
                arguments = request.get("arguments")
                if not isinstance(arguments, dict):
                    raise DishRuleError("INVALID_ARGUMENT", "arguments must be a JSON object", rule="arguments_object_required")
                extras = sorted(set(request) - {"arguments", "client"})
                if extras:
                    raise DishRuleError(
                        "INVALID_ARGUMENT",
                        "request contains an unexpected field",
                        rule="request_field_unexpected",
                        details={"field": extras[0]},
                    )
                payload = self.server.service.execute_admin(command, arguments, principal=principal, request_id=request_id)
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
                payload = self.server.service.execute_agent(
                    command, arguments, principal=principal, request_id=request_id
                )
            self._write_json(HTTPStatus.OK, payload)
        except DishRuleError as exc:
            replay_payload = None
            if (
                (
                    (surface in {"action", "agent"} and command in REPLAY_CAPABLE_COMMANDS)
                    or surface in {"admin", "admin-lease"}
                )
                and principal is not None
                and isinstance(request, dict)
            ):
                client_payload = request.get("client")
                raw_arguments = request.get("arguments")
                candidate_request_id = (
                    client_payload.get("request_id")
                    if isinstance(client_payload, dict)
                    else None
                )
                if isinstance(candidate_request_id, str):
                    try:
                        require_dish_uuid(candidate_request_id, field="client.request_id")
                    except DishRuleError:
                        pass
                    else:
                        replay_payload = self.server.service.record_replay_validation_failure(
                            command,
                            raw_arguments if isinstance(raw_arguments, dict) else {},
                            principal=principal,
                            request_id=candidate_request_id,
                            error=exc,
                        )
            if replay_payload is not None:
                self._write_json(HTTPStatus.OK, replay_payload)
                return
            if exc.rule in {"service_auth_required", "service_auth_invalid"}:
                status = HTTPStatus.UNAUTHORIZED
            elif exc.rule == "service_scope_forbidden":
                status = HTTPStatus.FORBIDDEN
            elif surface in {"action", "action-lease"}:
                # GPT Actions classify non-2xx responses as transport failures. Expected
                # Dish rule outcomes must remain readable canonical workflow envelopes.
                status = HTTPStatus.OK
            else:
                status = HTTPStatus.BAD_REQUEST
            self._write_json(
                status,
                error_envelope(command, exc),
                close_connection=not self._request_body_consumed,
            )
        except Exception:
            request_id = str(uuid.uuid4())
            LOG.exception(
                "unhandled_request_error surface=%s command=%s request_id=%s",
                surface,
                command,
                request_id,
            )
            error = DishRuleError(
                "INTERNAL_ERROR",
                "unexpected internal failure",
                rule="unexpected_internal_failure",
                details={"request_id": request_id},
            )
            try:
                self._write_json(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    error_envelope(command, error),
                    close_connection=True,
                )
            except Exception:
                LOG.exception(
                    "failed_to_write_error_response request_id=%s", request_id
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
