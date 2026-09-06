#!/usr/bin/env python3
"""Authenticated Streamable HTTP MCP projection of the PostgreSQL Dish Action contract."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass
import json
import logging
import os
from typing import Any, Mapping
from urllib.parse import urlparse

from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.tools import Tool as FastMCPTool
from fastmcp.tools.base import ToolResult
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.types import (
    TextContent,
    Tool,
    ToolAnnotations,
)
from pydantic import PrivateAttr
import uvicorn

from dish_pg.command_contract import ACTION_COMMANDS, COMMAND_DEFINITIONS
from dish_pg.openapi import postgres_action_openapi
from dish_service.client import DishActionClient

SERVER_NAME = "dish-postgresql-mcp"
SERVER_VERSION = "2"
ACTION_URL_ENV = "DISH_MCP_ACTION_URL"
ACTION_TOKEN_ENV = "DISH_MCP_ACTION_TOKEN"
GITHUB_CLIENT_ID_ENV = "DISH_MCP_GITHUB_CLIENT_ID"
GITHUB_CLIENT_SECRET_ENV = "DISH_MCP_GITHUB_CLIENT_SECRET"
GITHUB_USER_ID_ENV = "DISH_MCP_GITHUB_USER_ID"
RESOURCE_URL_ENV = "DISH_MCP_RESOURCE_URL"
BIND_HOST_ENV = "DISH_MCP_BIND_HOST"
BIND_PORT_ENV = "DISH_MCP_BIND_PORT"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8765
LOG = logging.getLogger("dish.mcp")
SERVER_INSTRUCTIONS = (
    "Dish PostgreSQL workflow authority is behind these tools. Keep one stable client.run_id "
    "for the logical agent run/stage. For every replay-bound mutation, create one fresh canonical "
    "client.request_id for the logical request and reuse that exact run_id, request_id, command, "
    "and arguments only when retrying after no Dish envelope was received. Once any Dish envelope "
    "is received, stop transport retry behavior and follow its allowed_actions, data.agent_guidance, "
    "continuation fields, and any human_action. Never invent or reconstruct Dish, operation, cycle, "
    "lease, proposal, recovery, or review identifiers. Independent Verification uses a genuinely "
    "different run from the run that authored or materially edited the candidate. An ok:false Dish "
    "envelope is an authoritative normal tool result, not an MCP transport failure."
)


def _resolve_local_refs(value: Any, schemas: Mapping[str, Any]) -> Any:
    """Resolve OpenAPI component-schema refs into a standalone MCP JSON Schema."""
    if isinstance(value, list):
        return [_resolve_local_refs(item, schemas) for item in value]
    if not isinstance(value, Mapping):
        return deepcopy(value)
    ref = value.get("$ref")
    if isinstance(ref, str):
        prefix = "#/components/schemas/"
        if not ref.startswith(prefix):
            raise ValueError(f"unsupported OpenAPI schema reference: {ref}")
        name = ref.removeprefix(prefix)
        target = schemas.get(name)
        if not isinstance(target, Mapping):
            raise ValueError(f"unknown OpenAPI component schema: {name}")
        merged = _resolve_local_refs(target, schemas)
        extras = {key: child for key, child in value.items() if key != "$ref"}
        if extras:
            if not isinstance(merged, dict):
                raise ValueError(f"schema reference {name} did not resolve to an object")
            merged.update(_resolve_local_refs(extras, schemas))
        return merged
    return {
        str(key): _resolve_local_refs(child, schemas)
        for key, child in value.items()
    }


def _mcp_output_schema(value: Any, schemas: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical Action response as a valid MCP object output schema."""
    resolved = _resolve_local_refs(value, schemas)
    if not isinstance(resolved, dict):
        raise ValueError("Dish MCP output schema must resolve to an object schema")
    schema_type = resolved.get("type")
    if schema_type is None:
        resolved["type"] = "object"
    elif schema_type != "object":
        raise ValueError("Dish MCP output schema must have type object")
    return resolved


def _tool_annotations(command: str) -> dict[str, bool]:
    definition = COMMAND_DEFINITIONS[command]
    is_mutation = definition.request_replay
    return {
        "readOnlyHint": not is_mutation,
        "idempotentHint": True,
        "destructiveHint": is_mutation,
        "openWorldHint": False,
    }


def build_tools() -> tuple[dict[str, Any], ...]:
    """Project MCP tools from the authoritative PostgreSQL Action/OpenAPI metadata."""
    spec = postgres_action_openapi()
    schemas = spec["components"]["schemas"]
    tools: list[dict[str, Any]] = []
    for command in ACTION_COMMANDS:
        operation = spec["paths"][f"/v1/action/{command}"]["post"]
        request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
        response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
        name = f"dish_{command.replace('-', '_')}"
        tools.append(
            {
                "name": name,
                "title": operation["summary"],
                "description": (
                    f"{operation['description']} Supply the existing Dish Action request body "
                    "unchanged as client plus arguments."
                ),
                "inputSchema": _resolve_local_refs(request_schema, schemas),
                "outputSchema": _mcp_output_schema(response_schema, schemas),
                "annotations": _tool_annotations(command),
            }
        )
    return tuple(tools)


TOOLS = build_tools()
TOOL_COMMANDS = {tool["name"]: command for tool, command in zip(TOOLS, ACTION_COMMANDS)}
MCP_TOOLS = tuple(Tool.model_validate(tool) for tool in TOOLS)


def _loopback_action_url(raw_url: str) -> str:
    value = raw_url.strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "127.0.0.1",
        "localhost",
        "::1",
    }:
        raise ValueError(f"{ACTION_URL_ENV} must name the loopback Dish Action listener")
    if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
        raise ValueError(f"{ACTION_URL_ENV} must be an origin without a path, query, or fragment")
    return value


def _https_url(raw_value: str, *, label: str, resource: bool = False) -> str:
    value = raw_value.strip()
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"{label} must be an https URL without embedded credentials")
    if parsed.fragment:
        raise ValueError(f"{label} must not contain a fragment")
    if resource:
        if not parsed.path.endswith("/mcp") or parsed.params or parsed.query:
            raise ValueError(f"{label} must be the public MCP resource URL ending exactly in /mcp")
    return value


def _loopback_bind_host(raw_value: str) -> str:
    value = raw_value.strip()
    if value not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError(f"{BIND_HOST_ENV} must remain loopback-only")
    return value


def _bind_port(raw_value: str) -> int:
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{BIND_PORT_ENV} must be an integer") from exc
    if not 1 <= value <= 65535:
        raise ValueError(f"{BIND_PORT_ENV} must be between 1 and 65535")
    return value


@dataclass(frozen=True, slots=True)
class MCPAuthConfig:
    github_client_id: str
    github_client_secret: str
    github_user_id: str
    resource_url: str
    bind_host: str = DEFAULT_BIND_HOST
    bind_port: int = DEFAULT_BIND_PORT

    @property
    def base_url(self) -> str:
        return self.resource_url.removesuffix("/mcp").rstrip("/")

    @property
    def issuer_url(self) -> str:
        return self.base_url

    @classmethod
    def from_environment(cls) -> "MCPAuthConfig":
        client_id = os.environ.get(GITHUB_CLIENT_ID_ENV, "")
        client_secret = os.environ.get(GITHUB_CLIENT_SECRET_ENV, "")
        user_id = os.environ.get(GITHUB_USER_ID_ENV, "")
        resource = os.environ.get(RESOURCE_URL_ENV, "")
        missing = [
            name
            for name, value in (
                (GITHUB_CLIENT_ID_ENV, client_id),
                (GITHUB_CLIENT_SECRET_ENV, client_secret),
                (GITHUB_USER_ID_ENV, user_id),
                (RESOURCE_URL_ENV, resource),
            )
            if not value.strip()
        ]
        if missing:
            raise ValueError(f"required MCP OAuth configuration missing: {', '.join(missing)}")
        if not user_id.strip().isdigit():
            raise ValueError(f"{GITHUB_USER_ID_ENV} must be a numeric GitHub user ID")
        return cls(
            github_client_id=client_id.strip(),
            github_client_secret=client_secret.strip(),
            github_user_id=user_id.strip(),
            resource_url=_https_url(resource, label=RESOURCE_URL_ENV, resource=True),
            bind_host=_loopback_bind_host(os.environ.get(BIND_HOST_ENV, DEFAULT_BIND_HOST)),
            bind_port=_bind_port(os.environ.get(BIND_PORT_ENV, str(DEFAULT_BIND_PORT))),
        )


class DishGitHubProvider(GitHubProvider):
    """FastMCP OAuth proxy restricted to one immutable GitHub account ID."""

    def __init__(self, *, allowed_user_id: str, **kwargs: Any) -> None:
        self.allowed_user_id = allowed_user_id
        super().__init__(**kwargs)

    async def verify_token(self, token: str) -> AccessToken | None:
        access = await super().verify_token(token)
        if access is None or access.subject != self.allowed_user_id:
            if access is not None:
                LOG.warning("mcp_github_user_rejected")
            return None
        return access


class DishMCPAdapter:
    def __init__(self, *, action_url: str, action_token: str):
        self.action_url = _loopback_action_url(action_url)
        self.action_token = action_token.strip()
        if not self.action_token:
            raise ValueError(f"{ACTION_TOKEN_ENV} is required")

    @classmethod
    def from_environment(cls) -> "DishMCPAdapter":
        action_url = os.environ.get(ACTION_URL_ENV, "")
        action_token = os.environ.get(ACTION_TOKEN_ENV, "")
        if not action_url:
            raise ValueError(f"{ACTION_URL_ENV} is required")
        return cls(action_url=action_url, action_token=action_token)

    def call(self, tool_name: str, tool_input: Mapping[str, Any]) -> dict[str, Any]:
        try:
            command = TOOL_COMMANDS[tool_name]
        except KeyError as exc:
            raise ValueError("unknown Dish MCP tool") from exc
        if set(tool_input) != {"client", "arguments"}:
            raise ValueError("tool input must contain exactly client and arguments")
        client = tool_input.get("client")
        arguments = tool_input.get("arguments")
        if not isinstance(client, Mapping) or not isinstance(arguments, Mapping):
            raise ValueError("client and arguments must be objects")
        if set(client) - {"run_id", "request_id"}:
            raise ValueError("client contains unsupported fields")
        run_id = client.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("client.run_id is required")
        definition = COMMAND_DEFINITIONS[command]
        request_id = client.get("request_id")
        if definition.request_replay:
            if not isinstance(request_id, str) or not request_id.strip():
                raise ValueError("client.request_id is required for this replay-bound command")
        elif request_id is not None:
            raise ValueError("client.request_id is not accepted for this read-only command")
        action = DishActionClient(
            self.action_url,
            token=self.action_token,
            run_id=run_id,
        )
        return action.execute(command, dict(arguments), request_id=request_id)


def _caller_audit_context(token: AccessToken | None) -> dict[str, str | None]:
    if token is None:
        return {
            "principal_class": "connected-agent",
            "issuer": None,
            "client_id": None,
            "subject": None,
        }
    issuer = (token.claims or {}).get("iss")
    return {
        "principal_class": "connected-agent",
        "issuer": str(issuer) if issuer is not None else None,
        "client_id": token.client_id,
        "subject": token.subject,
    }


def _redacted_adapter_error(exc: Exception, adapter: DishMCPAdapter) -> str:
    detail = f"{type(exc).__name__}: {exc}"
    if adapter.action_token:
        detail = detail.replace(adapter.action_token, "<redacted>")
    return detail


class DishTool(FastMCPTool):
    _adapter: DishMCPAdapter = PrivateAttr()

    def __init__(self, definition: Mapping[str, Any], adapter: DishMCPAdapter) -> None:
        super().__init__(
            name=str(definition["name"]),
            title=str(definition["title"]),
            description=str(definition["description"]),
            parameters=dict(definition["inputSchema"]),
            output_schema=dict(definition["outputSchema"]),
            annotations=ToolAnnotations.model_validate(definition["annotations"]),
        )
        self._adapter = adapter

    async def run(self, arguments: dict[str, Any]) -> ToolResult:
        caller = _caller_audit_context(get_access_token())
        client = arguments.get("client")
        run_id = client.get("run_id") if isinstance(client, Mapping) else None
        LOG.info(
            "mcp_connected_agent_call caller=%s tool=%s run_id=%s",
            json.dumps(caller, sort_keys=True, separators=(",", ":")),
            self.name,
            run_id,
        )
        try:
            structured = await asyncio.to_thread(self._adapter.call, self.name, arguments)
        except Exception as exc:
            LOG.warning(
                "mcp_adapter_failure caller=%s tool=%s error_type=%s",
                json.dumps(caller, sort_keys=True, separators=(",", ":")),
                self.name,
                type(exc).__name__,
            )
            return ToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"Dish MCP adapter failure: {_redacted_adapter_error(exc, self._adapter)}",
                    )
                ],
                is_error=True,
            )
        return ToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(structured, sort_keys=True, ensure_ascii=False),
                )
            ],
            structured_content=structured,
            is_error=False,
        )


def create_app(
    adapter: DishMCPAdapter,
    config: MCPAuthConfig,
):
    """Build the GitHub-backed MCP OAuth proxy and Streamable HTTP app."""
    auth = DishGitHubProvider(
        client_id=config.github_client_id,
        client_secret=config.github_client_secret,
        allowed_user_id=config.github_user_id,
        base_url=config.base_url,
        issuer_url=config.issuer_url,
        required_scopes=[],
        require_authorization_consent=True,
    )
    server = FastMCP(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
        auth=auth,
        tools=[DishTool(tool, adapter) for tool in TOOLS],
    )
    return server.http_app(
        path="/mcp",
        json_response=True,
        stateless_http=True,
    )


def main() -> int:
    config = MCPAuthConfig.from_environment()
    app = create_app(DishMCPAdapter.from_environment(), config)
    uvicorn.run(
        app,
        host=config.bind_host,
        port=config.bind_port,
        access_log=False,
        log_level="info",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
