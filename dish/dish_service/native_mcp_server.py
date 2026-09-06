#!/usr/bin/env python3
"""Native stdio MCP server for PostgreSQL-authoritative Dish connected agents."""
from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import Any, Mapping

import anyio
from mcp.server.context import ServerRequestContext
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
    ToolAnnotations,
)

from dish_pg.connected_command_spec import (
    CONNECTED_COMMAND_SPECS,
    TOOL_COMMANDS,
    definition_for,
    result_envelope_schema,
)
from dish_pg.postgres_service import PostgresRuntimeService
from dish_pg.release import ALEMBIC_HEAD
from dish_service.action_guidance import attach_connected_agent_guidance
from dish_service.config import ServiceConfig
from dish_service.leases import ServicePrincipal
from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope

LOG = logging.getLogger("dish.native_mcp")
SERVER_NAME = "dish-postgresql-native-mcp"
SERVER_VERSION = "1"
_CONNECTED_OWNER_ENV = "DISH_CONNECTED_AGENT_OWNER_ID"

SERVER_INSTRUCTIONS = """Dish PostgreSQL is workflow authority. Keep client.run_id stable for one Dish agent run; MCP connection, session, and JSON-RPC request IDs do not replace it. For every new replay-bound logical mutation, use a fresh canonical client.request_id. After response loss, retry the same logical mutation only with the exact same client.run_id, client.request_id, command, and arguments. Follow each canonical ResultEnvelope's allowed_actions, agent_guidance, continuation identifiers, and human_action exactly; never invent or rediscover identifiers when Dish returned one. Verification must use an independent client.run_id when required by the workflow. An ok:false ResultEnvelope is a normal Dish result, not an MCP transport failure."""


class NativeMCPRuntimeError(RuntimeError):
    """Transport-level native MCP failure; canonical Dish failures stay structured."""


def _mcp_tools() -> list[Tool]:
    tools: list[Tool] = []
    for spec in CONNECTED_COMMAND_SPECS:
        tools.append(
            Tool(
                name=spec.tool_name,
                title=spec.title,
                description=spec.description,
                input_schema=spec.input_schema(),
                output_schema=result_envelope_schema(command=spec.name),
                annotations=ToolAnnotations(
                    read_only_hint=spec.read_only,
                    destructive_hint=spec.destructive,
                    open_world_hint=spec.open_world,
                    idempotent_hint=spec.idempotent,
                ),
            )
        )
    return tools


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


def _call_result(payload: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=_minimal_content(payload))],
        structured_content=payload,
        is_error=False,
    )


def _transport_error(message: str) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        is_error=True,
    )


class NativeMCPAdapter:
    """Validate MCP calls then dispatch directly to PostgreSQL application authority."""

    def __init__(self, service: PostgresRuntimeService, *, owner_id: str) -> None:
        owner = owner_id.strip()
        if not owner:
            raise ValueError("connected-agent owner identity is required")
        self.service = service
        self.owner_id = owner

    def _principal(self, run_id: str) -> ServicePrincipal:
        return ServicePrincipal(owner_id=self.owner_id, run_id=run_id)

    def _record_validation_failure(
        self,
        command: str,
        request: Mapping[str, Any],
        *,
        error: DishRuleError,
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
        recorded = self.service.record_replay_validation_failure(
            command,
            arguments,
            principal=self._principal(run_id),
            request_id=request_id,
            error=error,
        )
        recorded.setdefault("http_status", 400)
        return attach_connected_agent_guidance(recorded)

    def call_tool(self, tool_name: str, request: Mapping[str, Any]) -> CallToolResult:
        command = TOOL_COMMANDS.get(tool_name)
        if command is None:
            raise NativeMCPRuntimeError(f"unknown Dish MCP tool: {tool_name}")
        spec = definition_for(command)
        try:
            client, arguments = spec.validate(request)
        except DishRuleError as exc:
            try:
                recorded = self._record_validation_failure(command, request, error=exc)
            except DishRuleError as record_exc:
                if record_exc.rule == "postgresql_authority_unavailable":
                    raise NativeMCPRuntimeError(str(record_exc)) from record_exc
                raise
            return _call_result(recorded or _canonical_error(command, exc))

        run_id = client["run_id"]
        request_id = client.get("request_id")
        try:
            payload = self.service.execute_agent(
                command,
                arguments,
                principal=self._principal(run_id),
                request_id=request_id,
            )
        except DishRuleError as exc:
            if exc.rule == "postgresql_authority_unavailable":
                raise NativeMCPRuntimeError(str(exc)) from exc
            return _call_result(_canonical_error(command, exc))
        return _call_result(attach_connected_agent_guidance(payload))


def create_server(adapter: NativeMCPAdapter) -> Server[Any]:
    tools = _mcp_tools()

    async def list_tools(
        _ctx: ServerRequestContext[Any],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        return ListToolsResult(tools=tools)

    async def call_tool(
        _ctx: ServerRequestContext[Any], params: CallToolRequestParams
    ) -> CallToolResult:
        arguments = params.arguments or {}
        if not isinstance(arguments, Mapping):
            return _transport_error("Dish MCP tool arguments must be an object")
        try:
            return adapter.call_tool(params.name, arguments)
        except NativeMCPRuntimeError as exc:
            LOG.error("native MCP transport failure: %s", exc)
            return _transport_error(str(exc))

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


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


def runtime_service_from_environment() -> PostgresRuntimeService:
    """Build the direct PostgreSQL runtime without Action listener/auth configuration."""

    profile = _required_env("DISH_PROFILE")
    if profile not in {"test", "prod"}:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "DISH_PROFILE must be test or prod",
            rule="postgresql_runtime_profile_invalid",
        )
    asana_keys = sorted(key for key, value in os.environ.items() if "ASANA" in key and value)
    if asana_keys:
        raise DishRuleError(
            "BACKEND_REJECTED",
            "native MCP PostgreSQL runtime refuses Asana environment configuration",
            rule="postgresql_native_mcp_asana_environment_forbidden",
            details={"environment_keys": asana_keys},
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
    elif not expected_database.startswith("dish_") or not expected_database.endswith("_prod"):
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

    connected_owner_id = _required_env(_CONNECTED_OWNER_ENV)
    config = ServiceConfig(
        db_path=state_dir / "unused-legacy-authority.sqlite3",
        honest_root=state_dir,
        action_client_id=connected_owner_id,
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


async def _run_stdio(server: Server[Any]) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def main() -> int:
    logging.basicConfig(level=os.environ.get("DISH_LOG_LEVEL", "INFO"))
    service: PostgresRuntimeService | None = None
    try:
        service = runtime_service_from_environment()
        adapter = NativeMCPAdapter(
            service,
            owner_id=_required_env(_CONNECTED_OWNER_ENV),
        )
        anyio.run(_run_stdio, create_server(adapter))
        return 0
    except (DishRuleError, NativeMCPRuntimeError, RuntimeError, ValueError) as exc:
        LOG.error("native MCP startup/runtime failure: %s", exc)
        return 2
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
