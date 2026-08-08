"""HTTP clients for the canonical shared-service result contract."""
from __future__ import annotations

import http.client
import json
import math
import uuid
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from dish_tool.constants import EXIT_STATUS_BY_CODE
from dish_tool.errors import DishRuleError
from dish_tool.identifiers import require_asana_gid, require_dish_uuid
from dish_tool.results import RESULT_ENVELOPE_FIELD_SET, result_envelope

from .command_spec import REPLAY_SAFE_COMMANDS


_AMBIGUOUS_RESPONSE_REPLAY_COMMANDS = frozenset({"inspect", "apply-proposal", "safe-reclaim"})


class _AmbiguousResponseError(Exception):
    """The request may have been sent, but no trustworthy response was received."""


def _request_id_for_command(command: str, request_id: str | None) -> str | None:
    if request_id is None and command in REPLAY_SAFE_COMMANDS:
        return str(uuid.uuid4())
    return request_id


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
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
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
            raise DishRuleError("INVALID_ARGUMENT", "service bearer token is required", rule="service_token_required")
        if not self.run_id:
            raise DishRuleError("INVALID_ARGUMENT", "service run identity is required", rule="service_run_required")

    def _json_request(
        self,
        path: str,
        *,
        method: str = "GET",
        payload: Mapping[str, Any] | None = None,
        ambiguous_after_dispatch: bool = False,
    ):
        body = None if payload is None else json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
        url = urlsplit(f"{self.base_url}{path}")
        target = url.path or "/"
        if url.query:
            target = f"{target}?{url.query}"
        connection_cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        # The connect phase and the response wait need independent bounds: a
        # command may legitimately perform several sequential upstream calls
        # before responding, so the response wait must be long, but a dead or
        # unreachable service should fail fast on connect.
        connection = connection_cls(url.hostname, url.port, timeout=self.connect_timeout)
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
                status = response.status
                raw = response.read()
            except (OSError, http.client.HTTPException) as exc:
                if ambiguous_after_dispatch:
                    raise _AmbiguousResponseError from exc
                raise DishRuleError(
                    "BACKEND_REJECTED",
                    "dish service is unavailable",
                    rule="service_unavailable",
                    retryable=True,
                ) from exc
        finally:
            connection.close()
        if status >= 400:
            try:
                return json.loads(raw.decode("utf-8"))
            except Exception as parse_exc:
                raise DishRuleError(
                    "INTERNAL_ERROR",
                    "dish service returned an unreadable error",
                    rule="service_response_invalid",
                ) from parse_exc
        return json.loads(raw.decode("utf-8"))

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
        result = self._json_request(
            path,
            method=method,
            payload=payload,
            ambiguous_after_dispatch=ambiguous_after_dispatch,
        )
        if not isinstance(result, dict):
            raise DishRuleError(
                "INTERNAL_ERROR",
                (
                    "dish service returned a noncanonical command result; "
                    "verify DISH_SERVICE_URL points to the correct listener"
                ),
                rule="service_response_invalid",
                details={"result_type": type(result).__name__},
            )
        present = set(result)
        missing = sorted(RESULT_ENVELOPE_FIELD_SET - present)
        if missing:
            raise DishRuleError(
                "INTERNAL_ERROR",
                (
                    "dish service returned a noncanonical command result; "
                    "verify DISH_SERVICE_URL points to the correct listener"
                ),
                rule="service_response_invalid",
                details={"missing_fields": missing},
            )
        return result

    def _command_result_request(
        self,
        command: str,
        path: str,
        *,
        request_id: str | None,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        ambiguous_response_requires_replay = (
            command in _AMBIGUOUS_RESPONSE_REPLAY_COMMANDS
            and request_id is not None
        )
        try:
            result = self._result_request(
                path,
                method="POST",
                payload=payload,
                ambiguous_after_dispatch=ambiguous_response_requires_replay,
            )
        except _AmbiguousResponseError:
            return self._ambiguous_command_result(
                command=command, request_id=request_id
            )
        except DishRuleError as exc:
            if (
                ambiguous_response_requires_replay
                and exc.rule == "service_response_invalid"
            ):
                return self._ambiguous_command_result(
                    command=command, request_id=request_id
                )
            raise
        except (UnicodeDecodeError, json.JSONDecodeError):
            if ambiguous_response_requires_replay:
                return self._ambiguous_command_result(
                    command=command, request_id=request_id
                )
            raise
        if not ambiguous_response_requires_replay:
            return result
        try:
            return self._validate_canonical_result(result, expected_command=command)
        except (ValueError, TypeError):
            return self._ambiguous_command_result(
                command=command, request_id=request_id
            )

    def _ambiguous_command_result(
        self, *, command: str, request_id: str
    ) -> dict[str, Any]:
        return result_envelope(
            command=command,
            ok=False,
            code="BACKEND_UNCERTAIN",
            retryable=False,
            allowed_actions=[],
            data={
                "message": "the request may have reached the service, but no authoritative response was received",
                "request_id": request_id,
                "run_id": self.run_id,
                "request_replay_required": True,
                "required_next_action": "retry_exact_request",
                "safe_to_retry": False,
            },
            errors=[{"rule": "service_response_ambiguous"}],
        )

    @staticmethod
    def _validate_canonical_result(
        result: Any, *, expected_command: str
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise ValueError("command result is not a JSON object")
        present = set(result)
        missing = sorted(RESULT_ENVELOPE_FIELD_SET - present)
        extras = sorted(present - RESULT_ENVELOPE_FIELD_SET)
        if missing or extras:
            raise ValueError(
                f"command result fields are invalid: missing={missing}, extras={extras}"
            )
        if not isinstance(result["ok"], bool):
            raise ValueError("ok must be boolean")
        if result["command"] != expected_command:
            raise ValueError("command does not match the request")
        if result["code"] not in EXIT_STATUS_BY_CODE:
            raise ValueError("code is not recognized")
        if result["ok"] != (result["code"] == "OK"):
            raise ValueError("ok and code are inconsistent")
        for field in ("task_gid", "submission_id", "state"):
            if result[field] is not None and not isinstance(result[field], str):
                raise ValueError(f"{field} must be a string or null")
        if not isinstance(result["retryable"], bool):
            raise ValueError("retryable must be boolean")
        if not isinstance(result["allowed_actions"], list) or not all(
            isinstance(item, str) for item in result["allowed_actions"]
        ):
            raise ValueError("allowed_actions must be a string array")
        if not isinstance(result["data"], dict):
            raise ValueError("data must be an object")
        if not isinstance(result["errors"], list) or not all(
            isinstance(item, dict) for item in result["errors"]
        ):
            raise ValueError("errors must be an object array")
        return result

    def _ambiguous_expire_lease_result(
        self, *, request_id: str, task_gid: str | None
    ) -> dict[str, Any]:
        return result_envelope(
            command="expire-lease",
            ok=False,
            code="BACKEND_UNCERTAIN",
            task_gid=task_gid,
            retryable=False,
            allowed_actions=[],
            data={
                "message": "the service may have processed the lease-expiry request",
                "request_id": request_id,
                "run_id": self.run_id,
                "request_replay_required": True,
                "required_next_action": "retry_exact_request",
            },
            errors=[{"rule": "service_response_ambiguous"}],
        )

    def _expire_lease_request(
        self,
        *,
        payload: Mapping[str, Any],
        request_id: str,
        task_gid: str | None,
    ) -> dict[str, Any]:
        body = json.dumps(dict(payload), separators=(",", ":")).encode("utf-8")
        url = urlsplit(f"{self.base_url}/v1/admin/leases/expire")
        target = url.path or "/"
        if url.query:
            target = f"{target}?{url.query}"
        connection_cls = (
            http.client.HTTPSConnection
            if url.scheme == "https"
            else http.client.HTTPConnection
        )
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
                # From this point onward, request bytes may have reached the
                # service. Every failure therefore requires exact replay.
                connection.request(
                    "POST",
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
            except (OSError, http.client.HTTPException):
                return self._ambiguous_expire_lease_result(
                    request_id=request_id, task_gid=task_gid
                )
        finally:
            connection.close()

        try:
            decoded = json.loads(raw.decode("utf-8"))
            return self._validate_canonical_result(
                decoded, expected_command="expire-lease"
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            return self._ambiguous_expire_lease_result(
                request_id=request_id, task_gid=task_gid
            )

    def health(self) -> dict[str, Any]:
        return self._json_request("/health")

    @staticmethod
    def _transport_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
        prepared = dict(arguments)
        file_path = prepared.pop("file_path", None)
        if file_path:
            prepared["file_text"] = Path(str(file_path)).read_text(encoding="utf-8")
        for field in ("intent_challenge_id", "intent_basis", "override_reason", "lease_id"):
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
            raise TypeError("provide command arguments as a mapping or keywords, not both")
        prepared = dict(arguments or keyword_arguments)
        request_id = _request_id_for_command(command, request_id)
        return self._command_result_request(
            command,
            f"/v1/commands/{command}",
            request_id=request_id,
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
        request_id = _request_id_for_command("renew-lease", request_id)
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
            raise TypeError("provide command arguments as a mapping or keywords, not both")
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
        return self._expire_lease_request(
            payload=payload,
            request_id=request_id,
            task_gid=task_gid,
        )

    def create_backup(
        self, *, label: str = "manual", request_id: str | None = None
    ) -> dict[str, Any]:
        if request_id is None:
            request_id = str(uuid.uuid4())
        return self._result_request(
            "/v1/admin/backups/create",
            method="POST",
            payload={"label": label, "client": self._client(request_id=request_id)},
        )

    def restore_backup(
        self, backup_id: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        if request_id is None:
            request_id = str(uuid.uuid4())
        return self._result_request(
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
        request_id = _request_id_for_command(command, request_id)
        return self._command_result_request(
            command,
            f"/v1/action/{command}",
            request_id=request_id,
            payload={
                "arguments": self._transport_arguments(prepared),
                "client": self._client(request_id=request_id),
            },
        )

    def renew_lease(
        self, operation_id: str, *, request_id: str | None = None
    ) -> dict[str, Any]:
        request_id = _request_id_for_command("renew-lease", request_id)
        return self._result_request(
            "/v1/action/renew-lease",
            method="POST",
            payload={
                "arguments": {"operation_id": operation_id},
                "client": self._client(request_id=request_id),
            },
        )
