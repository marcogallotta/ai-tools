from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from dish_pg.command_contract import ACTION_COMMANDS, COMMAND_DEFINITIONS
from dish_pg.openapi import postgres_action_openapi
from dish_service import mcp_server
from dish_service.native_mcp_server import NativeMCPAdapter
from fastmcp.server.auth.providers.github import GitHubProvider
from mcp.server.auth.provider import AccessToken

EXPECTED_COMMANDS = (
    "create",
    "sections",
    "section-tasks",
    "search",
    "query",
    "cooked-updates",
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
    "archive",
    "unarchive",
)
RUN_ID = "11111111-1111-4111-8111-111111111111"
REQUEST_ID = "22222222-2222-4222-8222-222222222222"
BASE_URL = "https://dish-mcp.example.com/dish"
ISSUER = BASE_URL
RESOURCE_URL = f"{BASE_URL}/mcp"
REGISTRATION_BODY = json.dumps(
    {
        "redirect_uris": ["http://127.0.0.1:8765/callback"],
        "token_endpoint_auth_method": "none",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "client_name": "Dish test client",
    }
).encode()


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
    assert len(mcp_server.MCP_TOOLS) == 22
    assert "dish_qualify_file_transport" not in mcp_server.TOOL_COMMANDS
    assert "dish_queue" not in mcp_server.TOOL_COMMANDS
    for command in ("archive", "unarchive"):
        tool = _tool(command)
        arguments = tool["inputSchema"]["properties"]["arguments"]
        assert arguments["required"] == ["dish_id", "agent"]
        assert set(arguments["properties"]) == {"dish_id", "agent"}
        assert arguments["additionalProperties"] is False
        assert tool["annotations"]["destructiveHint"] is (command == "archive")
    assert "reversible with unarchive" in _tool("archive")["description"]
    assert "new client.run_id" in _tool("unarchive")["description"]
    assert "complete currently cooked Dish inventory" in _tool("query")["description"]
    assert "cook logs determine actual cook timing" in _tool("cooked-updates")["description"]


def test_server_instructions_require_current_turn_dish_discovery():
    assert "Before any Dish MCP use or unavailable claim" in mcp_server.SERVER_INSTRUCTIONS
    assert "After Marco attaches @Dish or asks to retry" in mcp_server.SERVER_INSTRUCTIONS
    assert "earlier missing dish_query is not current availability evidence" in mcp_server.SERVER_INSTRUCTIONS


def test_server_instructions_make_archive_the_multifile_default():
    instructions = mcp_server.SERVER_INSTRUCTIONS
    assert "call dish_honest_archive first" in instructions
    assert "Do not request those files individually" in instructions
    assert "bounded single-file lookup" in instructions
    assert instructions.index("dish_honest_archive first") < instructions.index("dish_honest_read only")


def test_honest_git_ignores_inherited_git_configuration(monkeypatch, tmp_path):
    from dish_service import honest_store
    observed = {}
    real_popen = subprocess.Popen

    def observe(argv, **kwargs):
        observed.update(kwargs)
        return real_popen(argv, **kwargs)

    monkeypatch.setenv("GIT_DIR", "/unrelated/repository")
    monkeypatch.setattr(honest_store.subprocess, "Popen", observe)
    assert honest_store.git(tmp_path, "--version").startswith(b"git version")
    assert observed["env"]["GIT_SSH_COMMAND"] == honest_store.SSH_COMMAND
    assert observed["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert "GIT_DIR" not in observed["env"]


def test_honest_tool_is_added_outside_connected_command_contract():
    tool = mcp_server.build_honest_tool(Path("/tmp/honest"))
    assert tool.name == "dish_honest_read"
    assert tool.annotations.read_only_hint is True
    assert "stage reading guidance" in tool.description


def test_honest_archive_tool_returns_exact_tracked_main_as_embedded_zip(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    source = tmp_path / "source"
    checkout = tmp_path / "checkout"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(origin)],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "clone", str(origin), str(source)], check=True, capture_output=True)
    (source / "nested").mkdir()
    (source / "README.md").write_text("committed\n")
    (source / ".hidden").write_text("hidden\n")
    (source / "nested/file.txt").write_text("nested\n")
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "initial"],
        check=True, capture_output=True,
    )
    subprocess.run(["git", "-C", str(source), "push", "-u", "origin", "main"], check=True)
    subprocess.run(["git", "clone", "-b", "main", str(origin), str(checkout)], check=True,
                   capture_output=True)
    (checkout / "README.md").write_text("working tree only\n")
    (checkout / "untracked.txt").write_text("not archived\n")

    from dish_service import honest_store
    monkeypatch.setattr(honest_store, "REMOTE_URL", str(origin))
    tool = mcp_server.HonestArchiveTool(tmp_path / "store.git")
    result = asyncio.run(tool.run({}))

    assert tool.name == "dish_honest_archive"
    assert tool.parameters == {"type": "object", "properties": {}, "additionalProperties": False}
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.open_world_hint is False
    assert result.is_error is False
    assert result.structured_content is None
    assert len(result.content) == 2
    resource = result.content[1].resource
    assert resource.mime_type == "application/zip"
    sha = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD^{tree}"], check=True,
        capture_output=True, text=True,
    ).stdout.strip()
    assert resource.uri == f"file:///honest-pantry-files-{sha}.zip"
    payload = base64.b64decode(resource.blob)
    assert hashlib.sha256(payload).hexdigest() in result.content[0].text
    repeated = asyncio.run(tool.run({}))
    assert base64.b64decode(repeated.content[1].resource.blob) == payload
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        assert set(bundle.namelist()) == {
            ".hidden", "README.md", "nested/", "nested/file.txt", "FILES-MANIFEST.json",
        }
        assert bundle.read("README.md") == b"committed\n"
        assert bundle.read(".hidden") == b"hidden\n"
        assert "untracked.txt" not in bundle.namelist()
        assert not any(name.startswith(".git/") for name in bundle.namelist())
        manifest = json.loads(bundle.read("FILES-MANIFEST.json"))
    assert manifest == {
        "repository": "marcogallotta/honest-pantry",
        "ref": "refs/heads/main",
        "commit": sha,
        "tree": tree,
        "files": 3,
    }


def test_honest_archive_failure_returns_no_file(monkeypatch, tmp_path):
    monkeypatch.setattr(
        mcp_server, "build_honest_archive",
        lambda checkout: (_ for _ in ()).throw(RuntimeError("refresh failed")),
    )

    result = asyncio.run(mcp_server.HonestArchiveTool(tmp_path).run({}))

    assert result.is_error is False
    assert len(result.content) == 1
    assert result.content[0].text == "Honest archive unavailable: refresh failed"


def test_honest_read_fetches_main_and_returns_pinned_text(tmp_path, monkeypatch):
    origin = tmp_path / "origin.git"
    source = tmp_path / "source"
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(source)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "checkout", "-b", "main"], check=True, capture_output=True)
    (source / "CLAUDE.md").write_text("current instructions\n")
    subprocess.run(["git", "-C", str(source), "add", "CLAUDE.md"], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "initial"], check=True, capture_output=True,
    )
    subprocess.run(["git", "-C", str(source), "push", "-u", "origin", "main"], check=True, capture_output=True)
    subprocess.run(["git", "clone", "-b", "main", str(origin), str(checkout)], check=True, capture_output=True)

    from dish_service import honest_store
    monkeypatch.setattr(honest_store, "REMOTE_URL", str(origin))
    store = tmp_path / "store.git"
    result = mcp_server.read_honest_files(store, ["CLAUDE.md"])

    assert result["sha"] == subprocess.run(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert result["files"] == [{"path": "CLAUDE.md", "text": "current instructions\n"}]
    assert result["reading_guidance"]["recommended_paths"] == []
    assert result["reading_guidance"]["missing_recommended_paths"] == []
    wrapped = asyncio.run(
        mcp_server.build_honest_tool(store).run({"paths": ["CLAUDE.md"]})
    )
    assert wrapped.is_error is False
    assert wrapped.structured_content == {
        "ok": True,
        "code": "OK",
        "retryable": False,
        **result,
    }


def _planning_checkout(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", "-b", "main", str(checkout)], check=True, capture_output=True)
    for path in (*mcp_server.HONEST_PLANNING_TRIGGER_PATHS, "planning/index.md"):
        target = checkout / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{path}\n")
    subprocess.run(["git", "-C", str(checkout), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "-c", "user.name=Test", "-c", "user.email=test@example.com",
         "commit", "-m", "initial"], check=True, capture_output=True,
    )
    subprocess.run(["git", "init", "--bare", str(tmp_path / "origin.git")], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(checkout), "remote", "add", "origin", str(tmp_path / "origin.git")], check=True)
    subprocess.run(["git", "-C", str(checkout), "push", "-u", "origin", "main"], check=True, capture_output=True)
    from dish_service import honest_store
    monkeypatch.setattr(honest_store, "REMOTE_URL", str(tmp_path / "origin.git"))
    return tmp_path / "store.git"


def test_honest_read_reports_missing_planning_start_paths(tmp_path, monkeypatch):
    checkout = _planning_checkout(tmp_path, monkeypatch)

    result = mcp_server.read_honest_files(checkout, ["planning/index.md"])

    assert result["reading_guidance"]["recommended_paths"] == [
        *mcp_server.HONEST_PLANNING_START_PATHS,
    ]
    assert result["reading_guidance"]["missing_recommended_paths"] == [
        "dish-planning-protocol.md"
    ]


def test_honest_read_planning_protocol_alone_has_no_missing_paths(tmp_path, monkeypatch):
    result = mcp_server.read_honest_files(_planning_checkout(tmp_path, monkeypatch), ["dish-planning-protocol.md"])

    assert result["reading_guidance"]["recommended_paths"] == ["dish-planning-protocol.md"]
    assert result["reading_guidance"]["missing_recommended_paths"] == []


@pytest.mark.parametrize("path", ["dish-classes.md", "planning/direction.md"])
def test_honest_read_planning_index_docs_recommend_protocol(tmp_path, monkeypatch, path):
    result = mcp_server.read_honest_files(_planning_checkout(tmp_path, monkeypatch), [path])

    assert result["reading_guidance"]["missing_recommended_paths"] == [
        "dish-planning-protocol.md"
    ]


def test_reading_guidance_never_routes_through_claude_md(tmp_path, monkeypatch):
    result = mcp_server.read_honest_files(_planning_checkout(tmp_path, monkeypatch), ["planning/index.md"])

    assert "CLAUDE.md" not in mcp_server.SERVER_INSTRUCTIONS
    assert not any("CLAUDE.md" in line for line in result["reading_guidance"]["instructions"])
    assert mcp_server.HONEST_PLANNING_READING in mcp_server.SERVER_INSTRUCTIONS
    assert mcp_server.HONEST_PLANNING_READING in result["reading_guidance"]["instructions"]


def test_honest_read_fails_without_serving_when_fetch_fails(tmp_path, monkeypatch):
    from dish_service import honest_store
    monkeypatch.setattr(honest_store, "REMOTE_URL", str(tmp_path / "missing-upstream"))
    with pytest.raises(RuntimeError, match="no files were served"):
        mcp_server.read_honest_files(tmp_path / "store.git", ["CLAUDE.md"])


def test_honest_tool_contains_refresh_failure_as_normal_result(monkeypatch):
    def fail_refresh(checkout, paths):
        raise RuntimeError("Honest Pantry update failed; no files were served")

    class HealthyDishAdapter:
        def call(self, tool_name, tool_input, *, caller=None):
            return {"ok": True, "command": "sections", "data": {"sections": []}}

    monkeypatch.setattr(mcp_server, "read_honest_files", fail_refresh)
    result = asyncio.run(
        mcp_server.build_honest_tool(Path("/tmp/honest")).run({"paths": ["CLAUDE.md"]})
    )

    assert result.is_error is False
    assert result.structured_content == {
        "ok": False,
        "code": "HONEST_READ_FAILED",
        "message": "Honest Pantry update failed; no files were served",
        "retryable": True,
        "repository": "marcogallotta/honest-pantry",
        "sha": None,
        "files": [],
    }
    followup = asyncio.run(
        mcp_server.DishTool(_tool("sections"), HealthyDishAdapter()).run(
            {"client": {"run_id": RUN_ID}, "arguments": {"agent": "gpt"}}
        )
    )
    assert followup.is_error is False
    assert followup.structured_content == {
        "ok": True,
        "command": "sections",
        "data": {"sections": []},
    }


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


def test_mcp_annotations_do_not_classify_governed_writes_as_destructive():
    for command in EXPECTED_COMMANDS:
        is_mutation = COMMAND_DEFINITIONS[command].request_replay
        annotations = _tool(command)["annotations"]
        assert annotations == {
            "readOnlyHint": not is_mutation,
            "idempotentHint": True,
            "destructiveHint": command == "archive",
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


def test_mcp_trailing_slash_redirect_uses_configured_public_resource():
    app = mcp_server.create_app(_adapter(), _config())

    status, headers, _ = _asgi_request(
        app,
        method="POST",
        path="/mcp/",
        headers={
            "host": "attacker.example",
            "forwarded": "host=attacker.example;proto=http",
            "x-forwarded-host": "attacker.example",
            "x-forwarded-proto": "http",
        },
    )

    assert status == 307
    assert headers["location"] == RESOURCE_URL


@pytest.mark.parametrize("content_type", ["application/json", "Application/JSON; charset=utf-8"])
def test_oauth_registration_accepts_json_content_type(content_type):
    app = mcp_server.create_app(_adapter(), _config())

    status, headers, body = _asgi_request(
        app,
        method="POST",
        path="/register",
        headers={"content-type": content_type},
        body=REGISTRATION_BODY,
    )

    assert status == 201
    assert headers["content-type"] == "application/json"
    assert json.loads(body)["client_name"] == "Dish test client"


@pytest.mark.parametrize("content_type", [None, "text/plain", "application/x-www-form-urlencoded"])
def test_oauth_registration_rejects_non_json_content_type(content_type):
    app = mcp_server.create_app(_adapter(), _config())
    request_headers = {} if content_type is None else {"content-type": content_type}

    status, headers, body = _asgi_request(
        app,
        method="POST",
        path="/register",
        headers=request_headers,
        body=REGISTRATION_BODY,
    )

    assert status == 415
    assert headers["content-type"] == "application/json"
    assert headers["access-control-allow-origin"] == "*"
    assert json.loads(body) == {
        "error": "invalid_client_metadata",
        "error_description": "Content-Type must be application/json",
    }


def test_main_closes_native_backend_once_when_app_initialization_fails(monkeypatch):
    class FakePostgresService:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1

    service = FakePostgresService()
    adapter = NativeMCPAdapter(service, owner_id="192548")
    monkeypatch.setattr(
        mcp_server.MCPAuthConfig,
        "from_environment",
        classmethod(lambda cls: _config()),
    )
    monkeypatch.setattr(mcp_server, "backend_from_environment", lambda config: adapter)

    def fail_create_app(created_adapter, config):
        assert created_adapter is adapter
        raise RuntimeError("app initialization failed")

    monkeypatch.setattr(mcp_server, "create_app", fail_create_app)
    monkeypatch.setattr(
        mcp_server.uvicorn,
        "run",
        lambda *args, **kwargs: pytest.fail("uvicorn must not start"),
    )

    with pytest.raises(RuntimeError, match="app initialization failed"):
        mcp_server.main()

    assert service.close_calls == 1


def test_main_preserves_app_initialization_error_when_native_cleanup_fails(
    monkeypatch,
    caplog,
):
    class FailingPostgresService:
        def __init__(self):
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            raise RuntimeError("close failed")

    service = FailingPostgresService()
    adapter = NativeMCPAdapter(service, owner_id="192548")
    monkeypatch.setattr(
        mcp_server.MCPAuthConfig,
        "from_environment",
        classmethod(lambda cls: _config()),
    )
    monkeypatch.setattr(mcp_server, "backend_from_environment", lambda config: adapter)

    def fail_create_app(created_adapter, config):
        assert created_adapter is adapter
        raise ValueError("app initialization failed")

    monkeypatch.setattr(mcp_server, "create_app", fail_create_app)
    monkeypatch.setattr(
        mcp_server.uvicorn,
        "run",
        lambda *args, **kwargs: pytest.fail("uvicorn must not start"),
    )

    with pytest.raises(ValueError, match="app initialization failed"):
        mcp_server.main()

    assert service.close_calls == 1
    assert "MCP backend cleanup failed after app initialization failure" in caplog.text


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


def test_adapter_rejects_loopback_action_url_with_embedded_credentials():
    for action_url, secrets in (
        ("http://mcp-user@127.0.0.1:8766", ("mcp-user",)),
        ("http://:mcp-password@localhost:8766", ("mcp-password",)),
        (
            "http://mcp-user:mcp-password@[::1]:8766",
            ("mcp-user", "mcp-password"),
        ),
    ):
        with pytest.raises(
            ValueError, match="must not contain embedded credentials"
        ) as caught:
            mcp_server.DishMCPAdapter(
                action_url=action_url,
                action_token="action-secret-must-not-leak",
            )
        for secret in secrets:
            assert secret not in str(caught.value)


def test_adapter_failure_redacts_action_token_and_url_credentials():
    adapter = _adapter()
    adapter.action_url = "http://mcp-user:mcp%3Apassword@127.0.0.1:8766"
    error = RuntimeError(
        f"connection reset {adapter.action_url} "
        f"mcp-user mcp%3Apassword mcp:password {adapter.action_token}"
    )
    detail = mcp_server._redacted_adapter_error(error, adapter)
    assert "connection reset" in detail
    for secret in (
        adapter.action_token,
        "mcp-user",
        "mcp%3Apassword",
        "mcp:password",
    ):
        assert secret not in detail
    assert "<redacted>" in detail


def test_adapter_config_is_loopback_only():
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
    assert "Both Projects are **MCP-app-only**" in runbook
    assert "Never\nconnect, select, or invoke the GitHub Connector" in runbook
    assert runbook.count("TRANSPORT — MCP APPS ONLY") == 2


def _honest_upstream(tmp_path, monkeypatch):
    from dish_service import honest_store
    origin, source, store = (tmp_path / name for name in ("origin.git", "source", "store.git"))
    subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(origin)],
                   check=True, capture_output=True)
    subprocess.run(["git", "clone", str(origin), str(source)], check=True, capture_output=True)
    (source / "README.md").write_text("published\n")
    (source / "other.md").write_text("same revision\n")
    _publish_honest(source, "initial")
    monkeypatch.setattr(honest_store, "REMOTE_URL", str(origin))
    return origin, source, store


def _publish_honest(source, message):
    subprocess.run(["git", "-C", str(source), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Test", "-c",
                    "user.email=test@example.com", "commit", "-m", message],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(source), "push", "origin", "HEAD:main"],
                   check=True, capture_output=True)
    return subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip()


def test_honest_store_does_not_depend_on_human_checkout(tmp_path, monkeypatch):
    _origin, source, store = _honest_upstream(tmp_path, monkeypatch)
    (source / "README.md").write_text("uncommitted draft\n")
    (source / "untracked.md").write_text("private draft\n")
    subprocess.run(["git", "-C", str(source), "checkout", "-b", "human-work"], check=True,
                   capture_output=True)
    result = mcp_server.read_honest_files(store, ["README.md"])
    assert result["files"] == [{"path": "README.md", "text": "published\n"}]
    assert subprocess.check_output(["git", "--git-dir", str(store), "rev-parse",
                                    "--is-bare-repository"]).strip() == b"true"
    source.rename(tmp_path / "human-checkout-moved")
    assert mcp_server.read_honest_files(store, ["README.md"]) == result


def test_honest_read_pins_all_files_while_upstream_advances(tmp_path, monkeypatch):
    _origin, source, store = _honest_upstream(tmp_path, monkeypatch)
    old = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"]).decode().strip()
    real_git = mcp_server._honest_git
    advanced = []

    def advance_during_read(path, *args, **kwargs):
        if args[0] == "ls-tree" and not advanced:
            (source / "README.md").write_text("new published\n")
            (source / "other.md").write_text("new same revision\n")
            advanced.append(_publish_honest(source, "advance"))
        return real_git(path, *args, **kwargs)

    monkeypatch.setattr(mcp_server, "_honest_git", advance_during_read)
    result = mcp_server.read_honest_files(store, ["README.md", "other.md"])
    assert result["sha"] == old
    assert [file["text"] for file in result["files"]] == ["published\n", "same revision\n"]
    assert mcp_server.read_honest_files(store, ["README.md"])["sha"] == advanced[0]


def test_honest_repeated_rewind_rejected_without_losing_baseline(tmp_path, monkeypatch):
    from dish_service import honest_store
    _origin, source, store = _honest_upstream(tmp_path, monkeypatch)
    initial = mcp_server.read_honest_files(store, ["README.md"])["sha"]
    (source / "README.md").write_text("second\n")
    accepted = _publish_honest(source, "second")
    assert mcp_server.read_honest_files(store, ["README.md"])["sha"] == accepted
    subprocess.run(["git", "-C", str(source), "push", "--force", "origin", f"{initial}:main"],
                   check=True, capture_output=True)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="non-fast-forward"):
            mcp_server.read_honest_files(store, ["README.md"])
        assert honest_store.git(store, "rev-parse", honest_store.SERVED_REF).decode().strip() == accepted
    (source / "README.md").write_text("third\n")
    recovered = _publish_honest(source, "third")
    assert mcp_server.read_honest_files(store, ["README.md"])["sha"] == recovered


def test_honest_failed_fetch_does_not_serve_cached_text_then_recovers(tmp_path, monkeypatch):
    from dish_service import honest_store
    origin, _source, store = _honest_upstream(tmp_path, monkeypatch)
    good = mcp_server.read_honest_files(store, ["README.md"])
    monkeypatch.setattr(honest_store, "REMOTE_URL", str(tmp_path / "missing.git"))
    result = asyncio.run(mcp_server.build_honest_tool(store).run({"paths": ["README.md"]}))
    assert result.structured_content["ok"] is False
    assert result.structured_content["files"] == []
    assert result.structured_content["sha"] is None
    assert "fetch failed" in result.structured_content["message"]
    assert "does not appear to be a git repository" in result.structured_content["message"]
    monkeypatch.setattr(honest_store, "REMOTE_URL", str(origin))
    assert mcp_server.read_honest_files(store, ["README.md"]) == good


def test_honest_partial_initialization_retries_and_invalid_store_is_preserved(tmp_path, monkeypatch):
    from dish_service import honest_store
    _origin, _source, store = _honest_upstream(tmp_path, monkeypatch)
    real_git = honest_store.git
    failed = []

    def interrupt_init(path, *args, **kwargs):
        if args[0] == "init" and not failed:
            failed.append(True)
            path.mkdir()
            raise RuntimeError("interrupted initial init")
        return real_git(path, *args, **kwargs)

    monkeypatch.setattr(honest_store, "git", interrupt_init)
    with pytest.raises(RuntimeError, match="interrupted"):
        mcp_server.read_honest_files(store, ["README.md"])
    assert mcp_server.read_honest_files(store, ["README.md"])["files"][0]["text"] == "published\n"
    invalid = tmp_path / "unexpected"
    invalid.mkdir()
    (invalid / "keep.txt").write_text("do not delete")
    with pytest.raises(RuntimeError, match="no files were served"):
        mcp_server.read_honest_files(invalid, ["README.md"])
    assert (invalid / "keep.txt").read_text() == "do not delete"


def test_honest_simultaneous_first_requests_from_processes(tmp_path, monkeypatch):
    import sys
    origin, _source, store = _honest_upstream(tmp_path, monkeypatch)
    script = """
import sys
from pathlib import Path
from dish_service import honest_store
honest_store.REMOTE_URL = sys.argv[1]
with honest_store.snapshot(Path(sys.argv[2])) as sha:
    print(sha)
"""
    processes = [subprocess.Popen([sys.executable, "-c", script, str(origin), str(store)],
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  cwd=Path(mcp_server.__file__).parents[1]) for _ in range(3)]
    results = [process.communicate(timeout=30) for process in processes]
    assert all(process.returncode == 0 for process in processes), results
    assert len({stdout.strip() for stdout, stderr in results}) == 1


def test_honest_threads_wait_for_snapshot_and_external_lock_wait_is_bounded(tmp_path, monkeypatch):
    import fcntl
    import threading
    from concurrent.futures import ThreadPoolExecutor

    from dish_service import honest_store
    _origin, _source, store = _honest_upstream(tmp_path, monkeypatch)
    started = threading.Event()

    def read():
        started.set()
        return mcp_server.read_honest_files(store, ["README.md"])

    with ThreadPoolExecutor(max_workers=1) as executor:
        with honest_store.snapshot(store):
            future = executor.submit(read)
            assert started.wait(2)
            assert not future.done()
        assert future.result(timeout=10)["files"][0]["text"] == "published\n"
    monkeypatch.setattr(honest_store, "LOCK_TIMEOUT", 0.1)
    with store.with_name(store.name + ".lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with pytest.raises(RuntimeError, match="timed out waiting for another Honest process"):
            mcp_server.read_honest_files(store, ["README.md"])


def test_honest_git_timeout_kills_ssh_descendants(tmp_path, monkeypatch):
    import shlex
    import sys

    from dish_service import honest_store
    _origin, _source, store = _honest_upstream(tmp_path, monkeypatch)
    mcp_server.read_honest_files(store, ["README.md"])
    child_file = tmp_path / "child.pid"
    ssh = tmp_path / "ssh.py"
    ssh.write_text("import subprocess,time,pathlib\n"
                   "child=subprocess.Popen(['sleep','60'])\n"
                   f"pathlib.Path({str(child_file)!r}).write_text(str(child.pid))\n"
                   "time.sleep(60)\n")
    monkeypatch.setattr(honest_store, "SSH_COMMAND", shlex.join([sys.executable, str(ssh)]))
    with pytest.raises(RuntimeError, match="subprocess group stopped"):
        honest_store.git(store, "fetch", "ssh://fixture/upstream", timeout=1)
    child = int(child_file.read_text())
    status = Path(f"/proc/{child}/stat")
    # A reparented zombie is dead; its reaping belongs to the host's init process.
    assert not status.exists() or status.read_text().split()[2] == "Z"


def test_honest_preserves_regular_file_encoding_and_size_guards(tmp_path, monkeypatch):
    _origin, source, store = _honest_upstream(tmp_path, monkeypatch)
    (source / "link").symlink_to("README.md")
    (source / "binary").write_bytes(b"\xff")
    _publish_honest(source, "invalid text fixtures")
    for name, error in [("../README.md", "invalid repository path"),
                        ("link", "not a regular tracked file"), ("binary", "not UTF-8"),
                        ("missing", "tracked file not found")]:
        with pytest.raises(ValueError, match=error):
            mcp_server.read_honest_files(store, [name])
    monkeypatch.setattr(mcp_server, "HONEST_MAX_BYTES", 1)
    with pytest.raises(ValueError, match="exceed"):
        mcp_server.read_honest_files(store, ["README.md"])


def test_honest_missing_upstream_branch_fails_closed_and_recovers(tmp_path, monkeypatch):
    origin, source, store = _honest_upstream(tmp_path, monkeypatch)
    good = mcp_server.read_honest_files(store, ["README.md"])
    # Local disposable upstream only: remove its advertised main without changing accepted store.
    subprocess.run(["git", "--git-dir", str(origin), "update-ref", "-d", "refs/heads/main"], check=True)
    with pytest.raises(RuntimeError, match="couldn't find remote ref"):
        mcp_server.read_honest_files(store, ["README.md"])
    subprocess.run(["git", "-C", str(source), "push", "origin", "HEAD:main"], check=True,
                   capture_output=True)
    assert mcp_server.read_honest_files(store, ["README.md"]) == good


def test_honest_stale_git_lock_is_preserved_and_never_serves_cached_files(tmp_path, monkeypatch):
    _origin, source, store = _honest_upstream(tmp_path, monkeypatch)
    mcp_server.read_honest_files(store, ["README.md"])
    internal_lock = store / "refs/heads/served-main.lock"
    internal_lock.write_text("preserve for manual diagnosis")
    (source / "README.md").write_text("advance\n")
    _publish_honest(source, "advance")
    for _ in range(2):
        with pytest.raises(RuntimeError, match="fetch failed"):
            mcp_server.read_honest_files(store, ["README.md"])
        assert internal_lock.read_text() == "preserve for manual diagnosis"


def test_honest_diagnostics_keep_context_and_redact_credentials():
    from dish_service import honest_store
    error = str(honest_store._error("fetch", "first cause\nhttps://user:secret@example.com/repo\nAborting"))
    assert "first cause" in error and "Aborting" in error
    assert "user:secret" not in error
    assert "[redacted]@example.com" in error
    assert len(str(honest_store._error("fetch", "x" * 10000))) < 2200
