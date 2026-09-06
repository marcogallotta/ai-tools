from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric import rsa
import jwt
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
ISSUER = "https://idp.example.com"
JWKS_URL = "https://idp.example.com/.well-known/jwks.json"
RESOURCE_URL = "https://dish-mcp.example.com/mcp"


def _tool(command: str) -> dict[str, object]:
    name = f"dish_{command.replace('-', '_')}"
    return next(tool for tool in mcp_server.TOOLS if tool["name"] == name)


def _config(*, audience: str | None = None) -> mcp_server.MCPAuthConfig:
    return mcp_server.MCPAuthConfig(
        issuer_url=ISSUER,
        jwks_url=JWKS_URL,
        resource_url=RESOURCE_URL,
        audience=audience,
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


class _StaticTokenVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        if token not in {"valid-token", "missing-scope-token"}:
            return None
        scopes = [mcp_server.REQUIRED_SCOPE] if token == "valid-token" else []
        return AccessToken(
            token=token,
            client_id="chatgpt-client",
            scopes=scopes,
            expires_at=int(time.time()) + 300,
            resource=RESOURCE_URL,
            subject="marco",
            claims={"iss": ISSUER},
        )


class _StaticJWKClient:
    def __init__(self, key) -> None:
        self.key = key

    def get_signing_key_from_jwt(self, token: str):
        assert isinstance(token, str)
        return SimpleNamespace(key=self.key)


def _jwt_claims(**overrides) -> dict[str, object]:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": ISSUER,
        "sub": "marco",
        "client_id": "chatgpt-client",
        "aud": RESOURCE_URL,
        "scope": mcp_server.REQUIRED_SCOPE,
        "iat": now,
        "nbf": now - 1,
        "exp": now + 300,
    }
    claims.update(overrides)
    return claims


def _signed_token(private_key, **claims) -> str:
    return jwt.encode(_jwt_claims(**claims), private_key, algorithm="RS256", headers={"kid": "test"})


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
        assert "authorization" not in json.dumps(tool).lower()
        assert "bearer" not in json.dumps(tool).lower()


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
    monkeypatch.setenv(mcp_server.OAUTH_ISSUER_ENV, ISSUER)
    monkeypatch.setenv(mcp_server.OAUTH_JWKS_URL_ENV, JWKS_URL)
    monkeypatch.setenv(mcp_server.RESOURCE_URL_ENV, RESOURCE_URL)
    monkeypatch.setenv(mcp_server.BIND_HOST_ENV, "0.0.0.0")
    with pytest.raises(ValueError, match="loopback-only"):
        mcp_server.MCPAuthConfig.from_environment()

    monkeypatch.setenv(mcp_server.BIND_HOST_ENV, "127.0.0.1")
    monkeypatch.setenv(mcp_server.RESOURCE_URL_ENV, "http://dish-mcp.example.com/mcp")
    with pytest.raises(ValueError, match="https URL"):
        mcp_server.MCPAuthConfig.from_environment()

    monkeypatch.setenv(mcp_server.RESOURCE_URL_ENV, "https://dish-mcp.example.com/not-mcp")
    with pytest.raises(ValueError, match="ending exactly in /mcp"):
        mcp_server.MCPAuthConfig.from_environment()


def test_oauth_http_boundary_challenges_and_publishes_protected_resource_metadata(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "tunnel-control-plane-only")
    app = mcp_server.create_app(_adapter(), _config(), token_verifier=_StaticTokenVerifier())

    status, headers, body = _asgi_request(app, method="POST", path="/mcp")
    assert status == 401
    assert json.loads(body) == {"error": "invalid_token", "error_description": "Authentication required"}
    assert headers["www-authenticate"].startswith('Bearer error="invalid_token"')
    assert "https://dish-mcp.example.com/.well-known/oauth-protected-resource/mcp" in headers[
        "www-authenticate"
    ]
    assert "tunnel-control-plane-only" not in body.decode()

    status, _, body = _asgi_request(
        app,
        method="GET",
        path="/.well-known/oauth-protected-resource/mcp",
    )
    metadata = json.loads(body)
    assert status == 200
    assert metadata["resource"] == RESOURCE_URL
    assert [value.rstrip("/") for value in metadata["authorization_servers"]] == [ISSUER.rstrip("/")]
    assert metadata["scopes_supported"] == [mcp_server.REQUIRED_SCOPE]


def test_oauth_http_boundary_returns_401_for_invalid_token_and_403_for_missing_scope():
    app = mcp_server.create_app(_adapter(), _config(), token_verifier=_StaticTokenVerifier())

    status, _, _ = _asgi_request(
        app,
        method="POST",
        path="/mcp",
        headers={"authorization": "Bearer invalid-token"},
    )
    assert status == 401

    status, headers, body = _asgi_request(
        app,
        method="POST",
        path="/mcp",
        headers={"authorization": "Bearer missing-scope-token"},
    )
    assert status == 403
    assert json.loads(body)["error"] == "insufficient_scope"
    assert mcp_server.REQUIRED_SCOPE in headers["www-authenticate"]


def test_jwt_verifier_accepts_signed_targeted_token_and_keeps_bearer_out_of_audit_context():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = mcp_server.OIDCJWTVerifier(
        _config(),
        jwks_client=_StaticJWKClient(private_key.public_key()),
    )
    raw = _signed_token(private_key)

    token = asyncio.run(verifier.verify_token(raw))

    assert token is not None
    assert token.client_id == "chatgpt-client"
    assert token.subject == "marco"
    assert token.resource == RESOURCE_URL
    assert token.scopes == [mcp_server.REQUIRED_SCOPE]
    assert token.expires_at is not None
    audit = mcp_server._caller_audit_context(token)
    assert audit == {
        "principal_class": "connected-agent",
        "issuer": ISSUER,
        "client_id": "chatgpt-client",
        "subject": "marco",
    }
    assert raw not in json.dumps(audit)


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://wrong-issuer.example.com"},
        {"exp": 1},
        {"nbf": int(time.time()) + 3600},
        {"aud": "https://other-resource.example.com/mcp"},
        {"resource": "https://other-resource.example.com/mcp"},
    ],
)
def test_jwt_verifier_rejects_wrong_issuer_expiry_nbf_and_target(claims):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = mcp_server.OIDCJWTVerifier(
        _config(),
        jwks_client=_StaticJWKClient(private_key.public_key()),
    )
    raw = _signed_token(private_key, **claims)
    assert asyncio.run(verifier.verify_token(raw)) is None


def test_jwt_verifier_rejects_bad_signature_and_supports_explicit_audience():
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = mcp_server.OIDCJWTVerifier(
        _config(audience="dish-api"),
        jwks_client=_StaticJWKClient(signing_key.public_key()),
    )
    bad_signature = _signed_token(wrong_key)
    assert asyncio.run(verifier.verify_token(bad_signature)) is None

    custom_audience = _signed_token(signing_key, aud="dish-api")
    assert asyncio.run(verifier.verify_token(custom_audience)) is not None


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


def test_tunnel_units_and_runbook_bind_private_http_oauth_path():
    dish_root = Path(__file__).resolve().parents[2]
    tunnel_unit = (dish_root / "deploy/systemd/dish-mcp-tunnel.service").read_text(encoding="utf-8")
    mcp_unit = (dish_root / "deploy/systemd/dish-mcp.service").read_text(encoding="utf-8")
    runbook = (dish_root / "deploy/mcp-app.md").read_text(encoding="utf-8")

    assert "Requires=dish-mcp.service" in tunnel_unit
    assert "ExecStart=/home/marco/.local/bin/tunnel-client run --profile dish-mcp" in tunnel_unit
    assert "ExecStart=/home/marco/ai-tools/dish/.venv/bin/python -m dish_service.mcp_server" in mcp_unit
    assert "DISH_MCP_BIND_HOST=127.0.0.1" in runbook
    assert "DISH_MCP_ACTION_URL=http://127.0.0.1:8766" in runbook
    assert "sample_mcp_with_dcr" in runbook
    assert '--mcp-server-url "http://127.0.0.1:8765/mcp"' in runbook
    assert "DISH_MCP_ACTION_URL` to `http://127.0.0.1:8776" in runbook
    assert "does not\ndisable the old GPT Action" in runbook
