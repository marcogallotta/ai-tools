from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastmcp.server.auth.providers.github import GitHubProvider
from mcp.server.auth.provider import AccessToken
import pytest

from dish_pg.command_contract import ACTION_COMMANDS, COMMAND_DEFINITIONS
from dish_pg.openapi import postgres_action_openapi
from dish_service import mcp_server


EXPECTED_COMMANDS = (
    "create",
    "sections",
    "section-tasks",
    "search",
    "cook-logs",
    "record-cook-log",
    "read",
    "proposals",
    "apply-proposal",
    "safe-reclaim",
    "inspect",
    "start",
    "prepare",
    "approve",
    "reject",
    "submit",
    "renew-lease",
    "cooked",
)
RUN_ID = "11111111-1111-4111-8111-111111111111"
REQUEST_ID = "22222222-2222-4222-8222-222222222222"
BASE_URL = "https://dish-mcp.example.com/dish"
ISSUER = BASE_URL
RESOURCE_URL = f"{BASE_URL}/mcp"


def _tool(command: str) -> dict[str, object]:
    name = f"dish_{command.replace('-', '_')}"
    return next(tool for tool in mcp_server.TOOLS if tool["name"] == name)


def _config() -> mcp_server.MCPAuthConfig:
    return mcp_server.MCPAuthConfig(
        github_client_id="github-client-id",
        github_client_secret="github-client-secret",
        github_user_id="192548",
        resource_url=RESOURCE_URL,
    )


def _adapter() -> mcp_server.DishMCPAdapter:
    return mcp_server.DishMCPAdapter(
        action_url="http://127.0.0.1:8766",
        action_token="action-secret-must-not-leak",
    )


def _asgi_request(
    app,
    *,
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> tuple[int, dict[str, str], bytes]:
    async def invoke() -> tuple[int, dict[str, str], bytes]:
        sent: list[dict[str, object]] = []
        delivered = False

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        raw_headers = [(key.lower().encode(), value.encode()) for key, value in (headers or {}).items()]
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": raw_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("127.0.0.1", 8765),
        }
        await app(scope, receive, send)
        start = next(message for message in sent if message["type"] == "http.response.start")
        payload = b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        )
        response_headers = {
            key.decode().lower(): value.decode()
            for key, value in start.get("headers", [])
        }
        return int(start["status"]), response_headers, payload

    return asyncio.run(invoke())


def test_mcp_tool_inventory_is_exact_postgresql_connected_contract():
    assert ACTION_COMMANDS == EXPECTED_COMMANDS
    assert tuple(mcp_server.TOOL_COMMANDS.values()) == EXPECTED_COMMANDS
    assert tuple(mcp_server.TOOL_COMMANDS) == tuple(
        f"dish_{command.replace('-', '_')}" for command in EXPECTED_COMMANDS
    )
    assert len(mcp_server.MCP_TOOLS) == 18
    assert "dish_qualify_file_transport" not in mcp_server.TOOL_COMMANDS
    assert "dish_queue" not in mcp_server.TOOL_COMMANDS
    assert "dish_archive" not in mcp_server.TOOL_COMMANDS


def test_mcp_tool_schemas_project_postgresql_action_openapi():
    spec = postgres_action_openapi()
    schemas = spec["components"]["schemas"]
    for command in EXPECTED_COMMANDS:
        operation = spec["paths"][f"/v1/action/{command}"]["post"]
        tool = _tool(command)
        expected_input = operation["requestBody"]["content"]["application/json"]["schema"]
        expected_output = mcp_server._resolve_local_refs(
            operation["responses"]["200"]["content"]["application/json"]["schema"],
            schemas,
        )
        if "type" not in expected_output:
            expected_output = {"type": "object", **expected_output}
        assert tool["inputSchema"] == expected_input
        assert tool["outputSchema"] == expected_output
        assert tool["outputSchema"]["type"] == "object"
        assert "$ref" not in json.dumps(tool["outputSchema"])


def test_mcp_annotations_follow_replay_metadata_conservatively():
    for command in EXPECTED_COMMANDS:
        is_mutation = COMMAND_DEFINITIONS[command].request_replay
        annotations = _tool(command)["annotations"]
        assert annotations == {
            "readOnlyHint": not is_mutation,
            "idempotentHint": True,
            "destructiveHint": is_mutation,
            "openWorldHint": False,
        }


def test_mcp_mutation_preserves_exact_client_identity_and_arguments(monkeypatch):
    seen: dict[str, object] = {}
    expected = {"ok": True, "command": "prepare", "data": {"marker": "same"}}

    class FakeActionClient:
        def __init__(self, base_url, *, token, run_id):
            seen["init"] = (base_url, token, run_id)

        def execute(self, command, arguments, *, request_id=None):
            seen["execute"] = (command, arguments, request_id)
            return expected

    monkeypatch.setattr(mcp_server, "DishActionClient", FakeActionClient)
    adapter = mcp_server.DishMCPAdapter(
        action_url="http://127.0.0.1:8776",
        action_token="secret-action-token",
    )
    arguments = {"submission_id": "33333333-3333-4333-8333-333333333333"}
    result = adapter.call(
        "dish_prepare",
        {"client": {"run_id": RUN_ID, "request_id": REQUEST_ID}, "arguments": arguments},
    )

    assert result is expected
    assert seen["init"] == ("http://127.0.0.1:8776", "secret-action-token", RUN_ID)
    assert seen["execute"] == ("prepare", arguments, REQUEST_ID)
    with pytest.raises(ValueError, match="request_id is required"):
        adapter.call(
            "dish_create",
            {"client": {"run_id": RUN_ID}, "arguments": {"agent": "gpt", "title": "Dish"}},
        )


def test_mcp_query_preserves_run_id_and_forbids_request_id(monkeypatch):
    seen: dict[str, object] = {}

    class FakeActionClient:
        def __init__(self, base_url, *, token, run_id):
            seen["run_id"] = run_id

        def execute(self, command, arguments, *, request_id=None):
            seen["execute"] = (command, arguments, request_id)
            return {"ok": True, "command": command, "data": {}}

    monkeypatch.setattr(mcp_server, "DishActionClient", FakeActionClient)
    adapter = _adapter()
    result = adapter.call(
        "dish_sections",
        {"client": {"run_id": RUN_ID}, "arguments": {"agent": "gpt"}},
    )

    assert result["ok"] is True
    assert seen == {"run_id": RUN_ID, "execute": ("sections", {"agent": "gpt"}, None)}
    with pytest.raises(ValueError, match="request_id is not accepted"):
        adapter.call(
            "dish_sections",
            {
                "client": {"run_id": RUN_ID, "request_id": REQUEST_ID},
                "arguments": {"agent": "gpt"},
            },
        )


def test_oauth_config_requires_https_public_resource_and_loopback_listener(monkeypatch):
    monkeypatch.setenv(mcp_server.GITHUB_CLIENT_ID_ENV, "client-id")
    monkeypatch.setenv(mcp_server.GITHUB_CLIENT_SECRET_ENV, "client-secret")
    monkeypatch.setenv(mcp_server.GITHUB_USER_ID_ENV, "192548")
    monkeypatch.setenv(mcp_server.RESOURCE_URL_ENV, RESOURCE_URL)
    monkeypatch.setenv(mcp_server.BIND_HOST_ENV, "0.0.0.0")
    with pytest.raises(ValueError, match="loopback-only"):
        mcp_server.MCPAuthConfig.from_environment()

    monkeypatch.setenv(mcp_server.BIND_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(mcp_server.RESOURCE_URL_ENV, "https://dish-mcp.example.com/dish/mcp")
    config = mcp_server.MCPAuthConfig.from_environment()
    assert config.base_url == "https://dish-mcp.example.com/dish"
    assert config.issuer_url == "https://dish-mcp.example.com/dish"

    monkeypatch.setenv(mcp_server.RESOURCE_URL_ENV, "http://dish-mcp.example.com/mcp")
    with pytest.raises(ValueError, match="https URL"):
        mcp_server.MCPAuthConfig.from_environment()

    monkeypatch.setenv(mcp_server.RESOURCE_URL_ENV, "https://dish-mcp.example.com/not-mcp")
    with pytest.raises(ValueError, match="ending exactly in /mcp"):
        mcp_server.MCPAuthConfig.from_environment()


def test_oauth_http_boundary_challenges_and_publishes_protected_resource_metadata(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "tunnel-control-plane-only")
    app = mcp_server.create_app(_adapter(), _config())

    status, headers, body = _asgi_request(app, method="POST", path="/mcp")
    assert status == 401
    assert body == b""
    assert headers["www-authenticate"].startswith("Bearer resource_metadata=")
    assert "https://dish-mcp.example.com/.well-known/oauth-protected-resource/dish/mcp" in headers[
        "www-authenticate"
    ]
    assert "tunnel-control-plane-only" not in body.decode()

    status, _, body = _asgi_request(
        app,
        method="GET",
        path="/.well-known/oauth-protected-resource/dish/mcp",
    )
    metadata = json.loads(body)
    assert status == 200
    assert metadata["resource"] == RESOURCE_URL
    assert [value.rstrip("/") for value in metadata["authorization_servers"]] == [ISSUER.rstrip("/")]
    assert metadata["scopes_supported"] == []

    status, _, body = _asgi_request(
        app, method="GET", path="/.well-known/oauth-authorization-server"
    )
    authorization = json.loads(body)
    assert status == 200
    assert authorization["issuer"].rstrip("/") == ISSUER
    assert authorization["authorization_endpoint"] == f"{BASE_URL}/authorize"
    assert authorization["token_endpoint"] == f"{BASE_URL}/token"
    assert authorization["registration_endpoint"] == f"{BASE_URL}/register"
    assert authorization["code_challenge_methods_supported"] == ["S256"]


def test_github_provider_rejects_every_user_except_configured_numeric_id(monkeypatch):
    async def fake_verify(self, token: str):
        return AccessToken(token=token, client_id="client", scopes=[], subject=token)

    monkeypatch.setattr(GitHubProvider, "verify_token", fake_verify)
    provider = mcp_server.DishGitHubProvider(
        client_id="client-id",
        client_secret="client-secret",
        allowed_user_id="192548",
        base_url=BASE_URL,
        issuer_url=ISSUER,
        required_scopes=[],
    )
    assert asyncio.run(provider.verify_token("192548")) is not None
    assert asyncio.run(provider.verify_token("999999")) is None


def test_adapter_failure_redacts_action_bearer_and_config_is_loopback_only():
    adapter = _adapter()
    error = RuntimeError(f"connection reset {adapter.action_token}")
    detail = mcp_server._redacted_adapter_error(error, adapter)
    assert "connection reset" in detail
    assert adapter.action_token not in detail
    assert "<redacted>" in detail
    with pytest.raises(ValueError, match="loopback"):
        mcp_server.DishMCPAdapter(
            action_url="https://public.example.com",
            action_token="must-not-be-used-publicly",
        )


def test_caddy_and_runbook_expose_github_oauth_proxy_paths():
    dish_root = Path(__file__).resolve().parents[2]
    caddy = (dish_root / "deploy/caddy/dish-action-router.json").read_text(encoding="utf-8")
    runbook = (dish_root / "deploy/mcp-app.md").read_text(encoding="utf-8")

    for path in ("/dish/authorize", "/dish/token", "/dish/register", "/dish/auth/callback"):
        assert path in caddy
    assert "/.well-known/oauth-authorization-server/dish" in caddy
    assert '"uri": "/.well-known/oauth-authorization-server"' in caddy
    assert "DISH_MCP_GITHUB_CLIENT_ID" in runbook
    assert "DISH_MCP_GITHUB_CLIENT_SECRET" in runbook
    assert "DISH_MCP_GITHUB_USER_ID" in runbook
