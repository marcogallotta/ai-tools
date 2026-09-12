#!/usr/bin/env python3
"""Authenticated Streamable HTTP MCP shell for Dish connected-agent backends."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Mapping
from urllib.parse import urlparse

import uvicorn
from dish_pg.connected_command_spec import (
    CONNECTED_COMMAND_SPECS,
    TOOL_COMMANDS,
    definition_for,
    result_envelope_schema,
)
from fastmcp import FastMCP
from fastmcp.server.auth.providers.github import GitHubProvider
from fastmcp.tools import FunctionTool
from fastmcp.tools import Tool as FastMCPTool
from fastmcp.tools.base import ToolResult
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.types import TextContent, Tool, ToolAnnotations
from pydantic import PrivateAttr

from dish_service.client import DishActionClient

SERVER_NAME = "dish-postgresql-mcp"
SERVER_VERSION = "4"
ACTION_URL_ENV = "DISH_MCP_ACTION_URL"
ACTION_TOKEN_ENV = "DISH_MCP_ACTION_TOKEN"
BACKEND_ENV = "DISH_MCP_BACKEND"
GITHUB_CLIENT_ID_ENV = "DISH_MCP_GITHUB_CLIENT_ID"
GITHUB_CLIENT_SECRET_ENV = "DISH_MCP_GITHUB_CLIENT_SECRET"
GITHUB_USER_ID_ENV = "DISH_MCP_GITHUB_USER_ID"
RESOURCE_URL_ENV = "DISH_MCP_RESOURCE_URL"
BIND_HOST_ENV = "DISH_MCP_BIND_HOST"
BIND_PORT_ENV = "DISH_MCP_BIND_PORT"
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_BIND_PORT = 8765
HONEST_CHECKOUT = Path("/home/marco/honest-pantry")
HONEST_MAX_FILES = 16
HONEST_MAX_BYTES = 512 * 1024
HONEST_LOCK = threading.Lock()
LOG = logging.getLogger("dish.mcp")
HONEST_PLANNING_START_PATHS = (
    "dish-planning-protocol.md",
    "planning/index.md",
    "dish-classes.md",
    "planning/blocks/index.md",
)
SERVER_INSTRUCTIONS = (
    "Dish PostgreSQL workflow authority is behind these tools. Before any Dish MCP use or "
    "unavailable claim, select the installed Dish app in the current turn. After Marco attaches "
    "@Dish or asks to retry, re-check the current tool registry; an earlier missing dish_query is "
    "not current availability evidence. Keep one stable client.run_id "
    "for the logical agent run/stage. For every replay-bound mutation, create one fresh canonical "
    "client.request_id for the logical request and reuse that exact run_id, request_id, command, "
    "and arguments only when retrying after no Dish envelope was received. Once any Dish envelope "
    "is received, stop transport retry behavior and follow its allowed_actions, data.agent_guidance, "
    "continuation fields, and any human_action. Never invent or reconstruct Dish, operation, cycle, "
    "lease, proposal, recovery, or review identifiers. Independent Verification uses a genuinely "
    "different run from the run that authored or materially edited the candidate. An ok:false Dish "
    "envelope is an authoritative normal tool result, not an MCP transport failure. "
    "Use dish_honest_read to read current Honest Pantry files; it updates main first and serves "
    "nothing if that update fails. Start with current CLAUDE.md and follow its routed stage "
    "protocol. Dish Planning must read dish-planning-protocol.md, planning/index.md, "
    "dish-classes.md, and planning/blocks/index.md, then use Dish cooked discovery and immutable "
    "cook logs as that protocol directs."
)


def _honest_git(checkout: Path, *args: str, timeout: int = 10) -> bytes:
    try:
        result = subprocess.run(
            ["/usr/bin/git", *args], cwd=checkout, capture_output=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Honest Pantry update failed; no files were served ({exc})") from exc
    if result.returncode:
        stderr_lines = result.stderr.decode("utf-8", "replace").strip().splitlines()
        reason = stderr_lines[-1] if stderr_lines else f"git {' '.join(args)} exited {result.returncode}"
        raise RuntimeError(f"Honest Pantry update failed; no files were served ({reason})")
    return result.stdout


def read_honest_files(checkout: Path, paths: list[str]) -> dict[str, Any]:
    if not paths or len(paths) > HONEST_MAX_FILES or len(set(paths)) != len(paths):
        raise ValueError(f"paths must contain 1-{HONEST_MAX_FILES} unique entries")
    for path in paths:
        parts = PurePosixPath(path)
        if (not path or parts.is_absolute() or "." in parts.parts or ".." in parts.parts
                or any(ord(char) < 32 for char in path)):
            raise ValueError(f"invalid repository path: {path!r}")

    with HONEST_LOCK:
        if _honest_git(checkout, "branch", "--show-current").decode().strip() != "main":
            raise RuntimeError("Honest Pantry checkout is not on main; no files were served")
        try:
            _honest_git(checkout, "pull", "--ff-only", "origin", "main", timeout=30)
        except RuntimeError:
            _honest_git(checkout, "pull", "--ff-only", "origin", "main", timeout=30)
        sha = _honest_git(checkout, "rev-parse", "HEAD").decode().strip()
        if sha != _honest_git(checkout, "rev-parse", "FETCH_HEAD").decode().strip():
            raise RuntimeError("Honest Pantry checkout does not match origin/main; no files were served")
        files: list[dict[str, str]] = []
        total = 0
        for path in paths:
            entry = _honest_git(checkout, "ls-tree", "-z", sha, "--", path)
            prefix, separator, listed_path = entry.partition(b"\t")
            if not separator or listed_path.rstrip(b"\0").decode() != path:
                raise ValueError(f"tracked file not found: {path}")
            mode, kind, _object = prefix.decode().split()
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise ValueError(f"not a regular tracked file: {path}")
            content = _honest_git(checkout, "show", f"{sha}:{path}")
            total += len(content)
            if total > HONEST_MAX_BYTES:
                raise ValueError(f"requested files exceed {HONEST_MAX_BYTES} bytes")
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"file is not UTF-8 text: {path}") from exc
            files.append({"path": path, "text": text})
        requested = set(paths)
        planning_requested = bool(
            requested.intersection(HONEST_PLANNING_START_PATHS)
            or any(path.startswith("planning/") for path in requested)
        )
        recommended = ["CLAUDE.md"]
        if planning_requested:
            recommended.extend(HONEST_PLANNING_START_PATHS)
        missing = [path for path in recommended if path not in requested]
        return {
            "repository": "marcogallotta/honest-pantry",
            "sha": sha,
            "files": files,
            "reading_guidance": {
                "instructions": [
                    "Read current CLAUDE.md first, then follow the stage protocol it routes.",
                    (
                        "For Dish Planning, read the planning protocol plus the compact cuisine, "
                        "class, and block indexes before opening conditional detail."
                    ),
                ],
                "recommended_paths": recommended,
                "missing_recommended_paths": missing,
            },
        }


def build_honest_tool(checkout: Path = HONEST_CHECKOUT) -> FunctionTool:
    def dish_honest_read(paths: list[str]) -> dict[str, Any]:
        """Read current Honest Pantry files and return guidance about required routed context."""
        try:
            result = read_honest_files(checkout, paths)
        except (RuntimeError, ValueError) as exc:
            LOG.warning("mcp_honest_read_failure error_type=%s", type(exc).__name__)
            return {
                "ok": False,
                "code": "HONEST_READ_FAILED",
                "message": str(exc),
                "retryable": isinstance(exc, RuntimeError),
                "repository": "marcogallotta/honest-pantry",
                "sha": None,
                "files": [],
            }
        return {"ok": True, "code": "OK", "retryable": False, **result}

    return FunctionTool.from_function(
        dish_honest_read,
        annotations=ToolAnnotations(
            readOnlyHint=True, idempotentHint=True, destructiveHint=False, openWorldHint=False,
        ),
    )


def _resolve_local_refs(value: Any, schemas: Mapping[str, Any]) -> Any:
    """Compatibility helper retained for transition parity tests."""
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
    return {str(key): _resolve_local_refs(child, schemas) for key, child in value.items()}


def build_tools() -> tuple[dict[str, Any], ...]:
    """Project MCP tools from the transport-neutral connected command contract."""
    return tuple(
        {
            "name": spec.tool_name,
            "title": spec.title,
            "description": spec.description,
            "inputSchema": spec.input_schema(),
            "outputSchema": result_envelope_schema(command=spec.name),
            "annotations": spec.annotations(),
        }
        for spec in CONNECTED_COMMAND_SPECS
    )


TOOLS = build_tools()
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
    """Transition Action backend used while native PostgreSQL qualification is incomplete."""

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

    def call(
        self,
        tool_name: str,
        tool_input: Mapping[str, Any],
        *,
        caller: Mapping[str, str | None] | None = None,
    ) -> dict[str, Any]:
        del caller
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
        spec = definition_for(command)
        request_id = client.get("request_id")
        if spec.request_replay:
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


def _redacted_adapter_error(exc: Exception, adapter: Any) -> str:
    detail = f"{type(exc).__name__}: {exc}"
    token = str(getattr(adapter, "action_token", "") or "")
    if token:
        detail = detail.replace(token, "<redacted>")
    return detail


class DishTool(FastMCPTool):
    _adapter: Any = PrivateAttr()

    def __init__(self, definition: Mapping[str, Any], adapter: Any) -> None:
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
            structured = await asyncio.to_thread(
                self._adapter.call,
                self.name,
                arguments,
                caller=caller,
            )
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
        renderer = getattr(self._adapter, "content_text", None)
        text = (
            renderer(structured)
            if callable(renderer)
            else json.dumps(structured, sort_keys=True, ensure_ascii=False)
        )
        return ToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content=structured,
            is_error=False,
        )


def create_app(adapter: Any, config: MCPAuthConfig):
    """Build the one authenticated GitHub OAuth MCP shell around a backend adapter."""
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
        tools=[*[DishTool(tool, adapter) for tool in TOOLS], build_honest_tool()],
    )
    return server.http_app(
        path="/mcp",
        json_response=True,
        stateless_http=True,
    )


def backend_from_environment(config: MCPAuthConfig) -> Any:
    backend = os.environ.get(BACKEND_ENV, "action").strip().lower() or "action"
    if backend == "action":
        return DishMCPAdapter.from_environment()
    if backend == "postgresql":
        from dish_service.native_mcp_server import native_adapter_from_environment

        return native_adapter_from_environment(authenticated_owner_id=config.github_user_id)
    raise ValueError(f"{BACKEND_ENV} must be action or postgresql")


def main() -> int:
    config = MCPAuthConfig.from_environment()
    adapter = backend_from_environment(config)
    app = create_app(adapter, config)
    try:
        uvicorn.run(
            app,
            host=config.bind_host,
            port=config.bind_port,
            access_log=False,
            log_level="info",
        )
        return 0
    finally:
        close = getattr(adapter, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    raise SystemExit(main())
