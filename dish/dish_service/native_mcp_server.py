#!/usr/bin/env python3
"""Direct PostgreSQL backend for the authenticated Dish MCP shell."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Mapping
import uuid

from sqlalchemy.exc import SQLAlchemyError

from dish_pg.command_contract import COMMAND_DEFINITIONS
from dish_pg.command_port import CommandCall, CommandPortError, PostgresCommandPort
from dish_pg.connected_command_spec import TOOL_COMMANDS, definition_for
from dish_pg.database import session_scope
from dish_pg.postgres_service import PostgresRuntimeService
from dish_pg.release import ALEMBIC_HEAD
from dish_pg.workflow import RequestIdentityConflict, WorkflowAuthorityError
from dish_service.action_guidance import attach_connected_agent_guidance
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope

NATIVE_VALIDATION_SURFACE = "mcp-native-validation"


class NativeMCPRuntimeError(RuntimeError):
    """Transport-level native MCP failure; canonical Dish failures stay structured."""


def _canonical_error(command: str, error: DishRuleError) -> dict[str, Any]:
    payload = error_envelope(command, error)
    payload["http_status"] = (
        503 if error.rule == "postgresql_authority_unavailable" else 400
    )
    return attach_connected_agent_guidance(payload)


def _minimal_content(payload: Mapping[str, Any]) -> str:
    command = str(payload.get("command") or "unknown")
    code = str(payload.get("code") or "UNKNOWN")
    replayed = " (replayed)" if payload.get("request_replayed") else ""
    return f"Dish {command}: {code}{replayed}"


class NativeValidationRecorder:
    """Record native-MCP validation failures through canonical PostgreSQL replay authority.

    PostgresRuntimeService's existing public wrapper remains HTTP-specific and records
    ``postgresql-http-validation``. This bounded adapter uses the same command-port
    replay authority while preserving the real native-MCP invocation provenance.
    """

    def __init__(self, service: PostgresRuntimeService) -> None:
        self.service = service

    def record(
        self,
        command: str,
        arguments: Mapping[str, Any],
        *,
        principal: ServicePrincipal,
        request_id: str,
        error: DishRuleError,
        invocation_surface: str,
    ) -> dict[str, Any]:
        try:
            run_id = uuid.UUID(principal.run_id)
            parsed_request_id = uuid.UUID(request_id)
        except ValueError as exc:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "service command identifiers are invalid",
                rule="service_identifier_invalid",
            ) from exc

        definition = COMMAND_DEFINITIONS.get(command)
        if definition is None or definition.principal not in {"agent", "admin"}:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "command is not eligible for replay-bound validation",
                rule="postgresql_validation_principal_invalid",
                details={"command": command},
            )

        envelope = error_envelope(command, error)
        envelope["data"]["request_id"] = request_id
        recorded_at = datetime.now(timezone.utc)
        try:
            with session_scope(self.service._session_maker) as session:
                authoritative, replayed = PostgresCommandPort(
                    session,
                    cursor_secret=self.service._cursor_secret,
                ).record_validation_failure(
                    CommandCall(
                        command_name=command,
                        arguments=dict(arguments),
                        owner_id=principal.owner_id,
                        principal_class=definition.principal,
                        run_id=run_id,
                        request_id=parsed_request_id,
                        now=recorded_at,
                    ),
                    result_payload=envelope,
                    invocation_surface=invocation_surface,
                )
                session.flush()
            if replayed:
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
        except (CommandPortError, WorkflowAuthorityError) as exc:
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


class NativeMCPAdapter:
    """Validate authenticated MCP calls then dispatch directly to PostgreSQL authority."""

    action_token = ""

    def __init__(
        self,
        service: PostgresRuntimeService,
        *,
        owner_id: str,
        validation_recorder: NativeValidationRecorder | Any | None = None,
    ) -> None:
        owner = owner_id.strip()
        if not owner:
            raise ValueError("authenticated connected-agent owner identity is required")
        self.service = service
        self.owner_id = owner
        self.validation_recorder = validation_recorder or NativeValidationRecorder(service)

    def close(self) -> None:
        self.service.close()

    @staticmethod
    def content_text(payload: Mapping[str, Any]) -> str:
        return _minimal_content(payload)

    def _principal(
        self,
        run_id: str,
        *,
        caller: Mapping[str, str | None] | None,
    ) -> ServicePrincipal:
        subject = None if caller is None else caller.get("subject")
        if subject != self.owner_id:
            raise NativeMCPRuntimeError(
                "authenticated MCP caller identity is missing or does not match the configured owner"
            )
        return ServicePrincipal(owner_id=self.owner_id, run_id=run_id)

    def _record_validation_failure(
        self,
        command: str,
        request: Mapping[str, Any],
        *,
        error: DishRuleError,
        caller: Mapping[str, str | None] | None,
    ) -> dict[str, Any] | None:
        spec = definition_for(command)
        if not spec.request_replay or spec.principal == "verification":
            return None
        client = request.get("client") if isinstance(request, Mapping) else None
        arguments = request.get("arguments") if isinstance(request, Mapping) else None
        if not isinstance(client, Mapping) or not isinstance(arguments, Mapping):
            return None
        run_id = client.get("run_id")
        request_id = client.get("request_id")
        if not isinstance(run_id, str) or not isinstance(request_id, str):
            return None
        try:
            uuid.UUID(run_id)
            uuid.UUID(request_id)
        except ValueError:
            return None
        recorded = self.validation_recorder.record(
            command,
            arguments,
            principal=self._principal(run_id, caller=caller),
            request_id=request_id,
            error=error,
            invocation_surface=NATIVE_VALIDATION_SURFACE,
        )
        recorded.setdefault("http_status", 400)
        return attach_connected_agent_guidance(recorded)

    def call(
        self,
        tool_name: str,
        request: Mapping[str, Any],
        *,
        caller: Mapping[str, str | None] | None = None,
    ) -> dict[str, Any]:
        command = TOOL_COMMANDS.get(tool_name)
        if command is None:
            raise NativeMCPRuntimeError(f"unknown Dish MCP tool: {tool_name}")
        spec = definition_for(command)
        try:
            client, arguments = spec.validate(request)
        except DishRuleError as exc:
            try:
                recorded = self._record_validation_failure(
                    command,
                    request,
                    error=exc,
                    caller=caller,
                )
            except DishRuleError as record_exc:
                if record_exc.rule == "postgresql_authority_unavailable":
                    raise NativeMCPRuntimeError(str(record_exc)) from record_exc
                raise
            return recorded or _canonical_error(command, exc)

        run_id = client["run_id"]
        request_id = client.get("request_id")
        principal = self._principal(run_id, caller=caller)
        try:
            payload = self.service.execute_agent(
                command,
                arguments,
                principal=principal,
                request_id=request_id,
            )
        except DishRuleError as exc:
            if exc.rule == "postgresql_authority_unavailable":
                raise NativeMCPRuntimeError(str(exc)) from exc
            return _canonical_error(command, exc)
        return attach_connected_agent_guidance(payload)


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise DishRuleError(
            "INVALID_ARGUMENT",
            f"{name} is required",
            rule="postgresql_native_mcp_environment_missing",
            details={"environment_key": name},
        )
    return value.strip()


def runtime_service_from_environment(
    *, authenticated_owner_id: str
) -> PostgresRuntimeService:
    """Build the direct PostgreSQL runtime behind the authenticated MCP boundary."""
    profile = _required_env("DISH_PROFILE")
    if profile not in {"test", "prod"}:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "DISH_PROFILE must be test or prod",
            rule="postgresql_runtime_profile_invalid",
        )
    asana_keys = sorted(
        key for key, value in os.environ.items() if "ASANA" in key.upper() and value
    )
    if asana_keys:
        raise DishRuleError(
            "BACKEND_REJECTED",
            "native MCP PostgreSQL runtime refuses Asana environment configuration",
            rule="postgresql_native_mcp_asana_environment_forbidden",
            details={"environment_keys": asana_keys},
        )

    owner = authenticated_owner_id.strip()
    if not owner:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "authenticated MCP owner identity is required",
            rule="postgresql_native_mcp_owner_missing",
        )

    expected_database = _required_env("DISH_PG_EXPECTED_DATABASE_NAME")
    if profile == "test":
        if (
            not expected_database.startswith("dish_")
            or not expected_database.endswith("_test")
            or "prod" in expected_database.lower()
            or "production" in expected_database.lower()
        ):
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "expected PostgreSQL database must be a disposable dish_*_test database",
                rule="postgresql_runtime_database_not_disposable",
            )
    elif (
        not expected_database.startswith("dish_")
        or not expected_database.endswith("_prod")
    ):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "expected PostgreSQL database must be an explicit dish_*_prod database",
            rule="postgresql_runtime_database_not_production_shaped",
        )

    expected_schema_head = _required_env("DISH_PG_EXPECTED_SCHEMA_HEAD")
    if expected_schema_head != ALEMBIC_HEAD:
        raise DishRuleError(
            "BACKEND_REJECTED",
            "configured PostgreSQL schema head does not match this release's ALEMBIC_HEAD",
            rule="postgresql_runtime_schema_configuration_mismatch",
            details={
                "configured_schema_head": expected_schema_head,
                "release_schema_head": ALEMBIC_HEAD,
            },
        )
    cursor_secret = _required_env("DISH_PG_CURSOR_SECRET").encode()
    if len(cursor_secret) < 24:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "PostgreSQL cursor secret must contain at least 24 bytes",
            rule="postgresql_runtime_cursor_secret_weak",
        )
    try:
        generation_id = uuid.UUID(_required_env("DISH_PG_EXPECTED_GENERATION_ID"))
    except ValueError as exc:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "DISH_PG_EXPECTED_GENERATION_ID must be a canonical UUID",
            rule="postgresql_runtime_generation_id_invalid",
        ) from exc
    state_dir = Path(_required_env("DISH_PG_AUTHORITY_STATE_DIR"))
    if not state_dir.is_dir():
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "DISH_PG_AUTHORITY_STATE_DIR must already exist",
            rule="postgresql_runtime_state_dir_missing",
        )

    config = ServiceConfig(
        db_path=state_dir / "unused-legacy-authority.sqlite3",
        honest_root=state_dir,
        action_client_id=owner,
        legacy_writer_fence_path=None,
    )
    service = PostgresRuntimeService(
        config,
        database_url=_required_env("DISH_PG_DATABASE_URL"),
        cursor_secret=cursor_secret,
        expected_database=expected_database,
        expected_schema_head=expected_schema_head,
        expected_release=_required_env("DISH_PG_EXPECTED_RELEASE"),
        expected_generation_id=generation_id,
        profile=profile,
    )
    try:
        startup = service.startup_check()
        if not startup["ok"] or startup["isolation"]["asana_environment_keys"]:
            raise RuntimeError("PostgreSQL native MCP startup validation failed")
        return service
    except BaseException:
        service.close()
        raise


def native_adapter_from_environment(*, authenticated_owner_id: str) -> NativeMCPAdapter:
    service = runtime_service_from_environment(
        authenticated_owner_id=authenticated_owner_id
    )
    return NativeMCPAdapter(service, owner_id=authenticated_owner_id)
