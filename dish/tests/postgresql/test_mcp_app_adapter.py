from __future__ import annotations

from io import StringIO
import json
from pathlib import Path

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


def _tool(command: str) -> dict[str, object]:
    name = f"dish_{command.replace('-', '_')}"
    return next(tool for tool in mcp_server.TOOLS if tool["name"] == name)


def test_mcp_tool_inventory_is_exact_postgresql_connected_contract():
    assert ACTION_COMMANDS == EXPECTED_COMMANDS
    assert tuple(mcp_server.TOOL_COMMANDS.values()) == EXPECTED_COMMANDS
    assert tuple(mcp_server.TOOL_COMMANDS) == tuple(
        f"dish_{command.replace('-', '_')}" for command in EXPECTED_COMMANDS
    )
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
    adapter = mcp_server.DishMCPAdapter(
        action_url="http://localhost:8766",
        action_token="test-token",
    )
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


def test_stdio_canonical_ok_false_and_continuation_are_normal_tool_results(monkeypatch):
    canonical = {
        "ok": False,
        "command": "start",
        "code": "CONFLICT",
        "allowed_actions": ["start"],
        "data": {
            "agent_guidance": {"instructions": ["reuse the exact returned challenge"]},
            "human_action": {"kind": "confirm-intent"},
        },
    }

    class FakeAdapter:
        def call(self, name, arguments):
            assert name == "dish_start"
            return canonical

    request = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "dish_start", "arguments": {"client": {}, "arguments": {}}},
    }
    fake_out = StringIO()
    monkeypatch.setattr(mcp_server.sys, "stdin", StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr(mcp_server.sys, "stdout", fake_out)

    assert mcp_server.serve(FakeAdapter()) == 0
    result = json.loads(fake_out.getvalue())["result"]
    assert result["isError"] is False
    assert result["structuredContent"] == canonical
    assert json.loads(result["content"][0]["text"]) == canonical


def test_stdio_adapter_failure_is_mcp_error_and_config_is_loopback_only(monkeypatch):
    class BrokenAdapter:
        action_token = "must-stay-secret"

        def call(self, name, arguments):
            raise RuntimeError("connection reset must-stay-secret")

    request = {
        "jsonrpc": "2.0",
        "id": 8,
        "method": "tools/call",
        "params": {"name": "dish_sections", "arguments": {}},
    }
    fake_out = StringIO()
    monkeypatch.setattr(mcp_server.sys, "stdin", StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr(mcp_server.sys, "stdout", fake_out)

    mcp_server.serve(BrokenAdapter())
    result = json.loads(fake_out.getvalue())["result"]
    assert result["isError"] is True
    assert "connection reset" in result["content"][0]["text"]
    assert "must-stay-secret" not in result["content"][0]["text"]
    assert "<redacted>" in result["content"][0]["text"]
    with pytest.raises(ValueError, match="loopback"):
        mcp_server.DishMCPAdapter(
            action_url="https://public.example.com",
            action_token="must-not-be-used-publicly",
        )


def test_tunnel_unit_and_runbook_bind_supported_stdio_adapter_path():
    dish_root = Path(__file__).resolve().parents[2]
    unit = (dish_root / "deploy/systemd/dish-mcp-tunnel.service").read_text(encoding="utf-8")
    runbook = (dish_root / "deploy/mcp-app.md").read_text(encoding="utf-8")

    assert "EnvironmentFile=/home/marco/.config/dish-service/mcp-tunnel.env" in unit
    assert "ExecStart=/home/marco/.local/bin/tunnel-client run --profile dish-mcp" in unit
    assert '--mcp-command "/home/marco/ai-tools/dish/.venv/bin/python -m dish_service.mcp_server"' in runbook
    assert "DISH_MCP_ACTION_URL=http://127.0.0.1:8766" in runbook
    assert "DISH_MCP_ACTION_URL` to `http://127.0.0.1:8776" in runbook
    assert "does not\ndisable the old GPT Action" in runbook
