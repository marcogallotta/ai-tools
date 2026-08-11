"""PostgreSQL authority adapter for the existing Dish HTTP service.

This module deliberately reuses ``dish-service`` and ``dish_service.http``. It
does not introduce another listener, routing table, authentication model, or
command framework. TEST rehearsal and a future cutover runtime therefore share
this service composition; environment/startup policy decides where it may run.
"""
from __future__ import annotations

import copy
import json
import os
import socket
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Mapping

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from dish_service.leases import ServicePrincipal
from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope

from . import models
from .command_contract import ACTION_COMMANDS
from .command_port import CommandCall, CommandPortError, PostgresCommandPort
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .openapi import postgres_action_openapi
from .repositories import CoreAuthorityError, RegistryRepository
from .workflow import (
    VALIDATION_FAILURE_REQUEST_KIND,
    RequestIdentityConflict,
    RequestSpec,
    StoredOutcome,
    WorkflowAuthorityError,
    WorkflowAuthorityService,
)


_SECTION4_CONTROL_POINTS_FIRED: set[tuple[str, str]] = set()


def _section4_control_point(
    *, point: str, request_id: uuid.UUID | None, command: str
) -> None:
    """Reach an explicit Section 4 barrier in PostgreSQL TEST runtime only.

    Fires at most once per (point, request_id) per process: a retried request
    reusing the same request_id (idempotent-replay scenarios) must not hit an
    already-torn-down barrier on its second pass through this code path.
    """

    configured = os.environ.get("DISH_SECTION4_SERVICE_CONTROL_POINT", "").strip()
    if configured != point or request_id is None:
        return
    expected_request = os.environ.get("DISH_SECTION4_SERVICE_REQUEST_ID", "").strip()
    if expected_request != str(request_id):
        return
    fired_key = (point, str(request_id))
    if fired_key in _SECTION4_CONTROL_POINTS_FIRED:
        return
    _SECTION4_CONTROL_POINTS_FIRED.add(fired_key)
    socket_path = os.environ.get("DISH_SECTION4_SERVICE_BARRIER_SOCKET", "").strip()
    if not socket_path:
        raise RuntimeError("Section 4 service control point omitted its barrier socket")
    label = (
        "service_after_execute_before_commit"
        if point == "after_execute_before_commit"
        else "service_after_commit_before_response"
    )
    message = {
        "schema": "dish-section4-barrier-event-v1",
        "label": label,
        "pid": os.getpid(),
        "payload": {"command": command, "command_request_id": str(request_id)},
    }
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall(
            json.dumps(message, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        received = bytearray()
        while not received.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                raise RuntimeError(f"Section 4 barrier {label!r} closed without release")
            received.extend(chunk)
    response = json.loads(received.decode("utf-8"))
    expected = {
        "schema": "dish-section4-barrier-event-v1",
        "action": "continue",
        "label": label,
    }
    if response != expected:
        raise RuntimeError(f"Section 4 barrier {label!r} returned invalid release")


class PostgresRuntimeService:
    """Expose PostgreSQL command authority through the established HTTP service."""

    _SUPPORTED_HTTP_SURFACES = frozenset({"agent", "action"})

    def __init__(
        self,
        config,
        *,
        database_url: str,
        cursor_secret: bytes,
        expected_database: str,
        expected_schema_head: str,
        expected_release: str,
        expected_generation_id: uuid.UUID,
    ) -> None:
        self.config = config
        self._engine = create_database_engine(DatabaseSettings(url=database_url))
        self._session_maker = session_factory(self._engine)
        self._cursor_secret = cursor_secret
        self._expected_database = expected_database
        self._expected_schema_head = expected_schema_head
        self._expected_release = expected_release
        self._expected_generation_id = expected_generation_id

    def close(self) -> None:
        self._engine.dispose()

    def supports_http_route(self, surface: str, command: str) -> bool:
        """Expose only routes implemented by the PostgreSQL authority adapter.

        The shared transport owns more private/admin routes than this adapter.
        The public Action surface is intentionally limited to the already
        implemented PostgreSQL command contract; unsupported legacy Action
        commands remain hidden rather than falling through to another backend.
        """

        if surface == "agent":
            return True
        if surface == "action":
            return command in ACTION_COMMANDS
        return False

    def action_openapi(self, *, server_url: str) -> dict[str, Any]:
        """Return the PostgreSQL Action contract served by the shared listener."""

        return postgres_action_openapi(server_url=server_url)

    def _identity(self) -> dict[str, Any]:
        try:
            with session_scope(self._session_maker) as session:
                database_name = str(session.scalar(text("SELECT current_database()")))
                schema_head = str(session.scalar(text("SELECT version_num FROM alembic_version")))
                generation = session.scalar(
                    select(models.AuthorityGeneration).where(
                        models.AuthorityGeneration.status == "active"
                    )
                )
                if generation is None:
                    raise DishRuleError(
                        "BACKEND_REJECTED",
                        "PostgreSQL authority has no active generation",
                        rule="postgresql_generation_missing",
                    )
        except DishRuleError:
            raise
        except SQLAlchemyError as exc:
            raise DishRuleError(
                "BACKEND_REJECTED",
                "PostgreSQL authority is unavailable",
                rule="postgresql_authority_unavailable",
                retryable=True,
                details={"error_type": type(exc).__name__},
            ) from exc

        observed = {
            "database": database_name,
            "schema_head": schema_head,
            "dish_release": generation.dish_release,
            "generation_id": str(generation.generation_id),
            "generation_status": generation.status,
        }
        expected = {
            "database": self._expected_database,
            "schema_head": self._expected_schema_head,
            "dish_release": self._expected_release,
            "generation_id": str(self._expected_generation_id),
            "generation_status": "active",
        }
        if observed != expected:
            raise DishRuleError(
                "BACKEND_REJECTED",
                "PostgreSQL runtime identity does not match the rehearsal binding",
                rule="postgresql_runtime_identity_mismatch",
                details={"expected": expected, "observed": observed},
            )
        return observed

    def startup_check(self) -> dict[str, Any]:
        identity = self._identity()
        return {
            "ok": True,
            "startup_ready": True,
            "backend": "postgresql",
            "profile": "test",
            "pid": os.getpid(),
            "identity": identity,
            "isolation": {
                "asana_environment_keys": sorted(
                    key for key, value in os.environ.items() if "ASANA" in key.upper() and value
                ),
                "bind_host": self.config.bind_host,
                "action_bind_host": self.config.action_bind_host,
                "legacy_writer_fence_path": None,
                "supported_http_surfaces": sorted(self._SUPPORTED_HTTP_SURFACES),
            },
        }

    def health(self) -> dict[str, Any]:
        try:
            return self.startup_check()
        except DishRuleError as exc:
            return {
                "ok": False,
                "startup_ready": False,
                "backend": "postgresql",
                "profile": "test",
                "pid": os.getpid(),
                "code": exc.code,
                "rule": exc.rule,
                "retryable": exc.retryable,
            }

    def record_replay_validation_failure(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal,
        request_id: str,
        error: DishRuleError,
    ) -> dict[str, Any]:
        """Persist a pre-execution failure without opening mutation admission."""

        try:
            run_id = uuid.UUID(principal.run_id)
            parsed_request_id = uuid.UUID(request_id)
        except ValueError as exc:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "service command identifiers are invalid",
                rule="service_identifier_invalid",
            ) from exc

        envelope = error_envelope(command, error)
        envelope["data"]["request_id"] = request_id
        validation_error = {
            "code": envelope["code"],
            "retryable": envelope["retryable"],
            "message": envelope["data"]["message"],
            "errors": [dict(item) for item in envelope["errors"]],
        }
        recorded_at = datetime.now(timezone.utc)
        try:
            with session_scope(self._session_maker) as session:
                generation = session.scalar(
                    select(models.AuthorityGeneration).where(
                        models.AuthorityGeneration.status == "active"
                    )
                )
                if generation is None:
                    raise WorkflowAuthorityError("no active authority generation")
                try:
                    binding = RegistryRepository(session).active_release_contract(
                        generation.generation_id
                    ).honest_binding
                except CoreAuthorityError as exc:
                    raise WorkflowAuthorityError(str(exc)) from exc
                admission = WorkflowAuthorityService(session).record_validation_failure(
                    spec=RequestSpec(
                        request_id=parsed_request_id,
                        generation_id=generation.generation_id,
                        run_id=run_id,
                        owner_id=principal.owner_id,
                        principal_class="agent",
                        command_name=command,
                        canonical_payload={
                            "request_kind": VALIDATION_FAILURE_REQUEST_KIND,
                            "command": command,
                            "arguments": dict(arguments),
                            "owner_id": principal.owner_id,
                            "run_id": str(run_id),
                            "validation_error": validation_error,
                        },
                        protocol_release=binding.protocol_release,
                        dish_release=generation.dish_release,
                        admitted_at=recorded_at,
                    ),
                    outcome=StoredOutcome(
                        outcome_id=uuid.uuid4(),
                        outcome_class="rule_error",
                        result_code=error.code,
                        http_status=400,
                        result_payload=envelope,
                        immutable_success=False,
                        recorded_at=recorded_at,
                    ),
                    audit_event_id=uuid.uuid4(),
                    audit_event_type=f"{command}_validation_rejected",
                    actor=f"{principal.owner_id}:{run_id}",
                    audit_payload={
                        "code": error.code,
                        "data": dict(envelope["data"]),
                        "errors": validation_error["errors"],
                    },
                    obligation_id=uuid.uuid4(),
                    invocation_metadata={
                        "surface": "postgresql-http-validation",
                        "protocol_release": binding.protocol_release,
                    },
                )
                session.flush()
                if admission.outcome is None:
                    raise WorkflowAuthorityError(
                        "validation request has no authoritative outcome"
                    )
                authoritative = copy.deepcopy(admission.outcome.result_payload)
            if admission.replayed:
                authoritative.setdefault("data", {})["request_replayed"] = True
                authoritative["data"]["request_id"] = request_id
            return authoritative
        except RequestIdentityConflict as exc:
            raise DishRuleError(
                "CONFLICT",
                "request ID was already used for different work",
                rule="service_request_identity_conflict",
                details={"request_id": request_id},
            ) from exc
        except WorkflowAuthorityError as exc:
            raise DishRuleError(
                "CONFLICT",
                str(exc),
                rule="postgresql_command_rejected",
            ) from exc
        except SQLAlchemyError as exc:
            raise DishRuleError(
                "BACKEND_REJECTED",
                "PostgreSQL authority is unavailable; validation failure was not recorded",
                rule="postgresql_authority_unavailable",
                retryable=True,
                details={"error_type": type(exc).__name__},
            ) from exc


    def renew_lease(
        self,
        operation_id: str,
        principal: ServicePrincipal,
        *,
        request_id: str,
    ) -> dict[str, Any]:
        """Bridge the shared lease route into the canonical PostgreSQL command."""

        return self.execute_agent(
            "renew-lease",
            {"operation_id": operation_id},
            principal=principal,
            request_id=request_id,
        )

    def execute_agent(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if principal is None:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "service principal is required",
                rule="service_principal_required",
            )
        try:
            run_id = uuid.UUID(principal.run_id)
            parsed_request_id = uuid.UUID(request_id) if request_id is not None else None
        except ValueError as exc:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "service command identifiers are invalid",
                rule="service_identifier_invalid",
            ) from exc

        try:
            with session_scope(self._session_maker) as session:
                result = PostgresCommandPort(
                    session,
                    cursor_secret=self._cursor_secret,
                ).execute(
                    CommandCall(
                        command_name=command,
                        arguments=dict(arguments),
                        owner_id=principal.owner_id,
                        principal_class="agent",
                        run_id=run_id,
                        request_id=parsed_request_id,
                        now=datetime.now(timezone.utc),
                    )
                )
                session.flush()
                _section4_control_point(
                    point="after_execute_before_commit",
                    request_id=parsed_request_id,
                    command=command,
                )
            _section4_control_point(
                point="after_commit_before_response",
                request_id=parsed_request_id,
                command=command,
            )
            payload = asdict(result)
            data = dict(payload.pop("data"))
            data["request_replayed"] = payload.pop("request_replayed")
            payload["data"] = data
            return payload
        except CommandPortError as exc:
            raise DishRuleError(
                "CONFLICT",
                str(exc),
                rule="postgresql_command_rejected",
            ) from exc
        except SQLAlchemyError as exc:
            raise DishRuleError(
                "BACKEND_REJECTED",
                "PostgreSQL authority is unavailable; governed mutation was not admitted",
                rule="postgresql_authority_unavailable",
                retryable=True,
                details={"error_type": type(exc).__name__},
            ) from exc
