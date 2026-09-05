#!/usr/bin/env python3
"""Thin stdio MCP projection of the canonical PostgreSQL Dish Action contract."""
from __future__ import annotations

from copy import deepcopy
import json
import os
import sys
from typing import Any, Mapping
from urllib.parse import urlparse

from dish_pg.command_contract import ACTION_COMMANDS, COMMAND_DEFINITIONS
from dish_pg.openapi import postgres_action_openapi
from dish_service.client import DishActionClient

SERVER_NAME = "dish-postgresql-mcp"
SERVER_VERSION = "1"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
ACTION_URL_ENV = "DISH_MCP_ACTION_URL"
ACTION_TOKEN_ENV = "DISH_MCP_ACTION_TOKEN"
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


def _reply(value: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(dict(value), separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def serve(adapter: DishMCPAdapter) -> int:
    for line in sys.stdin:
        request: Any = None
        try:
            request = json.loads(line)
            if not isinstance(request, Mapping):
                continue
            request_id = request.get("id")
            if request_id is None:
                continue
            method = str(request.get("method") or "")
            params = request.get("params") if isinstance(request.get("params"), Mapping) else {}
            if method == "initialize":
                result = {
                    "protocolVersion": str(params.get("protocolVersion") or DEFAULT_PROTOCOL_VERSION),
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "instructions": SERVER_INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": list(TOOLS)}
            elif method == "tools/call":
                name = str(params.get("name") or "")
                arguments = params.get("arguments")
                if not isinstance(arguments, Mapping):
                    raise ValueError("tool arguments must be an object")
                structured = adapter.call(name, arguments)
                result = {
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(structured, sort_keys=True, ensure_ascii=False),
                        }
                    ],
                    "structuredContent": structured,
                    "isError": False,
                }
            else:
                _reply(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "error": {"code": -32601, "message": "Method not found"},
                    }
                )
                continue
            _reply({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as exc:
            request_id = request.get("id") if isinstance(request, Mapping) else None
            detail = f"{type(exc).__name__}: {exc}"
            action_token = getattr(adapter, "action_token", "")
            if isinstance(action_token, str) and action_token:
                detail = detail.replace(action_token, "<redacted>")
            _reply(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": {
                        "content": [
                            {
                                "type": "text",
                                "text": f"Dish MCP adapter failure: {detail}",
                            }
                        ],
                        "isError": True,
                    },
                }
            )
    return 0


def main() -> int:
    return serve(DishMCPAdapter.from_environment())


if __name__ == "__main__":
    raise SystemExit(main())
