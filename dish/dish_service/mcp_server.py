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

import jwt
from mcp.server import Server, ServerRequestContext
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    TextContent,
    Tool,
)
import uvicorn

from dish_pg.command_contract import ACTION_COMMANDS, COMMAND_DEFINITIONS
from dish_pg.openapi import postgres_action_openapi
from dish_service.client import DishActionClient

SERVER_NAME = "dish-postgresql-mcp"
SERVER_VERSION = "2"
ACTION_URL_ENV = "DISH_MCP_ACTION_URL"
ACTION_TOKEN_ENV = "DISH_MCP_ACTION_TOKEN"
OAUTH_ISSUER_ENV = "DISH_MCP_OAUTH_ISSUER"
OAUTH_JWKS_URL_ENV = "DISH_MCP_OAUTH_JWKS_URL"
OAUTH_AUDIENCE_ENV = "DISH_MCP_OAUTH_AUDIENCE"
RESOURCE_URL_ENV = "DISH_MCP_RESOURCE_URL"
BIND_HOST_ENV = "DISH_MCP_BIND_HOST"
BIND_PORT_ENV = "DISH_MCP_BIND_PORT"
REQUIRED_SCOPE = "dish:connected"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8765
JWT_ALGORITHMS = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)
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
        if parsed.path != "/mcp" or parsed.params or parsed.query:
            raise ValueError(f"{label} must be the public MCP resource URL ending exactly in /mcp")
    elif label == OAUTH_ISSUER_ENV and (parsed.params or parsed.query):
        raise ValueError(f"{label} must be an issuer URL without parameters or query")
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
    issuer_url: str
    jwks_url: str
    resource_url: str
    audience: str | None = None
    bind_host: str = DEFAULT_BIND_HOST
    bind_port: int = DEFAULT_BIND_PORT

    @classmethod
    def from_environment(cls) -> "MCPAuthConfig":
        issuer = os.environ.get(OAUTH_ISSUER_ENV, "")
        jwks = os.environ.get(OAUTH_JWKS_URL_ENV, "")
        resource = os.environ.get(RESOURCE_URL_ENV, "")
        missing = [
            name
            for name, value in (
                (OAUTH_ISSUER_ENV, issuer),
                (OAUTH_JWKS_URL_ENV, jwks),
                (RESOURCE_URL_ENV, resource),
            )
            if not value.strip()
        ]
        if missing:
            raise ValueError(f"required MCP OAuth configuration missing: {', '.join(missing)}")
        audience = os.environ.get(OAUTH_AUDIENCE_ENV, "").strip() or None
        return cls(
            issuer_url=_https_url(issuer, label=OAUTH_ISSUER_ENV),
            jwks_url=_https_url(jwks, label=OAUTH_JWKS_URL_ENV),
            resource_url=_https_url(resource, label=RESOURCE_URL_ENV, resource=True),
            audience=audience,
            bind_host=_loopback_bind_host(os.environ.get(BIND_HOST_ENV, DEFAULT_BIND_HOST)),
            bind_port=_bind_port(os.environ.get(BIND_PORT_ENV, str(DEFAULT_BIND_PORT))),
        )


def _claim_strings(value: Any, *, claim: str) -> tuple[str, ...]:
    if isinstance(value, str) and value.strip():
        return (value.strip(),)
    if isinstance(value, list) and value and all(isinstance(item, str) and item.strip() for item in value):
        return tuple(item.strip() for item in value)
    raise ValueError(f"OAuth access token claim {claim} has unsupported shape")


def _token_scopes(claims: Mapping[str, Any]) -> list[str]:
    scope = claims.get("scope")
    if scope is not None:
        if not isinstance(scope, str):
            raise ValueError("OAuth access token scope claim must be a string")
        return [item for item in scope.split() if item]
    scp = claims.get("scp")
    if scp is None:
        return []
    if isinstance(scp, str):
        return [item for item in scp.split() if item]
    if isinstance(scp, list) and all(isinstance(item, str) and item.strip() for item in scp):
        return [item.strip() for item in scp]
    raise ValueError("OAuth access token scp claim has unsupported shape")


def _token_client_id(claims: Mapping[str, Any]) -> str:
    client_id = claims.get("client_id")
    azp = claims.get("azp")
    if client_id is not None and (not isinstance(client_id, str) or not client_id.strip()):
        raise ValueError("OAuth access token client_id claim must be a non-empty string")
    if azp is not None and (not isinstance(azp, str) or not azp.strip()):
        raise ValueError("OAuth access token azp claim must be a non-empty string")
    if isinstance(client_id, str) and isinstance(azp, str) and client_id.strip() != azp.strip():
        raise ValueError("OAuth access token client_id and azp claims disagree")
    value = client_id if isinstance(client_id, str) else azp
    if not isinstance(value, str) or not value.strip():
        raise ValueError("OAuth access token must identify its client with client_id or azp")
    return value.strip()


def _token_subject(claims: Mapping[str, Any]) -> str | None:
    subject = claims.get("sub")
    if subject is None:
        return None
    if not isinstance(subject, str) or not subject.strip():
        raise ValueError("OAuth access token sub claim must be a non-empty string")
    return subject.strip()


class OIDCJWTVerifier(TokenVerifier):
    """Verify external OAuth/OIDC JWT access tokens against the configured JWKS."""

    def __init__(self, config: MCPAuthConfig, *, jwks_client: Any | None = None) -> None:
        self.config = config
        self._jwks = jwks_client or jwt.PyJWKClient(config.jwks_url, cache_keys=True)

    def _target_is_valid(self, claims: Mapping[str, Any]) -> bool:
        resource = claims.get("resource")
        if resource is not None:
            try:
                return self.config.resource_url in _claim_strings(resource, claim="resource")
            except ValueError:
                return False
        audience = claims.get("aud")
        try:
            values = set(_claim_strings(audience, claim="aud"))
        except ValueError:
            return False
        allowed = {self.config.resource_url}
        if self.config.audience:
            allowed.add(self.config.audience)
        return bool(values & allowed)

    def _verify_sync(self, token: str) -> AccessToken | None:
        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                key=signing_key,
                algorithms=list(JWT_ALGORITHMS),
                issuer=self.config.issuer_url,
                options={"require": ["exp", "iss"], "verify_aud": False},
            )
            if not isinstance(claims, Mapping) or not self._target_is_valid(claims):
                raise ValueError("OAuth access token is not targeted at this Dish MCP resource")
            client_id = _token_client_id(claims)
            subject = _token_subject(claims)
            scopes = _token_scopes(claims)
            expires_at = int(claims["exp"])
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scopes,
                expires_at=expires_at,
                resource=self.config.resource_url,
                subject=subject,
                claims={"iss": self.config.issuer_url},
            )
        except (jwt.PyJWTError, TypeError, ValueError, OverflowError) as exc:
            # Never log the raw bearer or decoded claims. Rejection class is sufficient support evidence.
            LOG.info("mcp_oauth_token_rejected reason=%s", type(exc).__name__)
            return None

    async def verify_token(self, token: str) -> AccessToken | None:
        if not isinstance(token, str) or not token.strip():
            return None
        return await asyncio.to_thread(self._verify_sync, token)


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


def build_server(adapter: DishMCPAdapter) -> Server[Any]:
    async def list_tools(
        ctx: ServerRequestContext[Any], params: PaginatedRequestParams | None
    ) -> ListToolsResult:
        del ctx, params
        return ListToolsResult(tools=list(MCP_TOOLS))

    async def call_tool(
        ctx: ServerRequestContext[Any], params: CallToolRequestParams
    ) -> CallToolResult:
        del ctx
        arguments = params.arguments or {}
        if not isinstance(arguments, Mapping):
            return CallToolResult(
                content=[TextContent(type="text", text="Dish MCP adapter failure: tool arguments must be an object")],
                is_error=True,
            )
        caller = _caller_audit_context(get_access_token())
        client = arguments.get("client") if isinstance(arguments, Mapping) else None
        run_id = client.get("run_id") if isinstance(client, Mapping) else None
        LOG.info(
            "mcp_connected_agent_call caller=%s tool=%s run_id=%s",
            json.dumps(caller, sort_keys=True, separators=(",", ":")),
            params.name,
            run_id,
        )
        try:
            structured = await asyncio.to_thread(adapter.call, params.name, arguments)
        except Exception as exc:
            LOG.warning(
                "mcp_adapter_failure caller=%s tool=%s error_type=%s",
                json.dumps(caller, sort_keys=True, separators=(",", ":")),
                params.name,
                type(exc).__name__,
            )
            return CallToolResult(
                content=[
                    TextContent(
                        type="text",
                        text=f"Dish MCP adapter failure: {_redacted_adapter_error(exc, adapter)}",
                    )
                ],
                is_error=True,
            )
        return CallToolResult(
            content=[
                TextContent(
                    type="text",
                    text=json.dumps(structured, sort_keys=True, ensure_ascii=False),
                )
            ],
            structured_content=structured,
            is_error=False,
        )

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def create_app(
    adapter: DishMCPAdapter,
    config: MCPAuthConfig,
    *,
    token_verifier: TokenVerifier | None = None,
):
    """Build the private OAuth-protected Streamable HTTP ASGI app."""
    server = build_server(adapter)
    verifier = token_verifier or OIDCJWTVerifier(config)
    auth = AuthSettings.model_validate(
        {
            "issuer_url": config.issuer_url,
            "resource_server_url": config.resource_url,
            "required_scopes": [REQUIRED_SCOPE],
        }
    )
    return server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host=config.bind_host,
        auth=auth,
        token_verifier=verifier,
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
