from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dish_pg.command_contract import ACTION_COMMANDS, COMMAND_DEFINITIONS
from dish_pg.connected_command_spec import (
    CONNECTED_COMMANDS,
    CONNECTED_COMMAND_SPECS,
    TOOL_COMMANDS,
    result_envelope_schema,
)
from dish_pg.openapi import postgres_action_openapi
from dish_service import native_mcp_server
from dish_service.leases import ServicePrincipal
from dish_tool.errors import DishRuleError


RUN_ID = "11111111-1111-4111-8111-111111111111"
REQUEST_ID = "22222222-2222-4222-8222-222222222222"


class FakeService:
    def __init__(self, result: dict[str, Any] | None = None) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.result = result or {
            "ok": True,
            "command": "sections",
            "code": "OK",
            "http_status": 200,
            "task_gid": None,
            "submission_id": None,
            "state": None,
            "retryable": False,
            "request_replayed": False,
            "allowed_actions": [],
            "data": {"sections": []},
            "errors": [],
        }

    def execute_agent(
        self,
        command: str,
        arguments: dict[str, Any],
        *,
        principal: ServicePrincipal,
        request_id: str | None,
    ) -> dict[str, Any]:
        self.calls.append(("execute", command, arguments, principal, request_id))
        return dict(self.result)

    def record_replay_validation_failure(
        self,
        command: str,
        arguments: dict[str, Any],
        *,
        principal: ServicePrincipal,
        request_id: str,
        error: DishRuleError,
        invocation_surface: str,
    ) -> dict[str, Any]:
        self.calls.append(
            (
                "validation",
                command,
                arguments,
                principal,
                request_id,
                error.rule,
                invocation_surface,
            )
        )
        return {
            "ok": False,
            "command": command,
            "code": error.code,
            "retryable": error.retryable,
            "allowed_actions": [],
            "data": {"message": str(error), "request_id": request_id},
            "errors": [{"rule": error.rule}],
        }


def _resolve_openapi_refs(value: Any, schemas: dict[str, Any]) -> Any:
    if isinstance(value, list):
        return [_resolve_openapi_refs(item, schemas) for item in value]
    if not isinstance(value, dict):
        return value
    ref = value.get("$ref")
    if isinstance(ref, str):
        name = ref.removeprefix("#/components/schemas/")
        return _resolve_openapi_refs(schemas[name], schemas)
    return {key: _resolve_openapi_refs(child, schemas) for key, child in value.items()}


def test_connected_registry_is_exact_18_command_product_contract() -> None:
    assert CONNECTED_COMMANDS == ACTION_COMMANDS
    assert len(CONNECTED_COMMANDS) == 18
    assert tuple(spec.name for spec in CONNECTED_COMMAND_SPECS) == CONNECTED_COMMANDS
    assert tuple(TOOL_COMMANDS.values()) == CONNECTED_COMMANDS
    assert "qualify-file-transport" not in CONNECTED_COMMANDS
    assert "queue" not in CONNECTED_COMMANDS
    assert "archive" not in CONNECTED_COMMANDS


def test_registry_derives_identity_classification_and_annotations() -> None:
    for spec in CONNECTED_COMMAND_SPECS:
        definition = COMMAND_DEFINITIONS[spec.name]
        expected_kind = (
            "read"
            if not definition.request_replay
            else "continuation"
            if definition.operation_required
            else "mutation"
        )
        assert spec.principal == definition.principal
        assert spec.request_replay == definition.request_replay
        assert spec.kind == expected_kind
        assert spec.workflow_action == definition.workflow_action
        assert spec.input_schema()["type"] == "object"
        client = spec.input_schema()["properties"]["client"]
        assert ("request_id" in client["required"]) is definition.request_replay
        assert spec.annotations() == {
            "readOnlyHint": not definition.request_replay,
            "destructiveHint": definition.request_replay,
            "openWorldHint": False,
            "idempotentHint": True,
        }


def test_connected_output_schemas_preserve_qualified_result_envelope_parity() -> None:
    document = postgres_action_openapi(server_url="https://dish.example")
    schemas = document["components"]["schemas"]
    for command in CONNECTED_COMMANDS:
        response_schema = document["paths"][f"/v1/action/{command}"]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
        expected = _resolve_openapi_refs(response_schema, schemas)
        if isinstance(expected, dict) and "type" not in expected:
            expected = {**expected, "type": "object"}
        assert result_envelope_schema(command=command) == expected


def test_native_read_dispatches_directly_to_connected_service() -> None:
    service = FakeService()
    adapter = native_mcp_server.NativeMCPAdapter(service, owner_id="chatgpt-project")

    result = adapter.call_tool(
        "dish_sections",
        {"client": {"run_id": RUN_ID}, "arguments": {}},
    )

    assert result.is_error is False
    assert result.structured_content["ok"] is True
    assert result.structured_content["data"]["agent_guidance"]["source"] == "dish"
    assert service.calls == [
        (
            "execute",
            "sections",
            {},
            ServicePrincipal(owner_id="chatgpt-project", run_id=RUN_ID),
            None,
        )
    ]


def test_native_mutation_preserves_explicit_replay_identity() -> None:
    service = FakeService(
        {
            "ok": True,
            "command": "create",
            "code": "OK",
            "http_status": 200,
            "task_gid": "123",
            "submission_id": None,
            "state": "planning",
            "retryable": False,
            "request_replayed": False,
            "allowed_actions": ["start"],
            "data": {"dish_id": "33333333-3333-4333-8333-333333333333"},
            "errors": [],
        }
    )
    adapter = native_mcp_server.NativeMCPAdapter(service, owner_id="chatgpt-project")

    result = adapter.call_tool(
        "dish_create",
        {
            "client": {"run_id": RUN_ID, "request_id": REQUEST_ID},
            "arguments": {"agent": "gpt", "title": "MCP direct"},
        },
    )

    assert result.structured_content["command"] == "create"
    assert service.calls[0][4] == REQUEST_ID
    assert service.calls[0][3] == ServicePrincipal(
        owner_id="chatgpt-project", run_id=RUN_ID
    )


def test_replay_bound_validation_failure_is_recorded_with_same_ids() -> None:
    service = FakeService()
    adapter = native_mcp_server.NativeMCPAdapter(service, owner_id="chatgpt-project")

    result = adapter.call_tool(
        "dish_create",
        {
            "client": {"run_id": RUN_ID, "request_id": REQUEST_ID},
            "arguments": {"agent": "gpt"},
        },
    )

    assert result.is_error is False
    assert result.structured_content["ok"] is False
    assert service.calls[0][0] == "validation"
    assert service.calls[0][4] == REQUEST_ID


def test_normal_dish_failure_is_structured_not_mcp_transport_error() -> None:
    service = FakeService(
        {
            "ok": False,
            "command": "sections",
            "code": "CONFLICT",
            "http_status": 409,
            "task_gid": None,
            "submission_id": None,
            "state": None,
            "retryable": False,
            "request_replayed": False,
            "allowed_actions": [],
            "data": {"message": "blocked"},
            "errors": [{"rule": "blocked"}],
        }
    )
    adapter = native_mcp_server.NativeMCPAdapter(service, owner_id="chatgpt-project")

    result = adapter.call_tool(
        "dish_sections",
        {"client": {"run_id": RUN_ID}, "arguments": {}},
    )

    assert result.is_error is False
    assert result.structured_content["ok"] is False
    assert result.structured_content["code"] == "CONFLICT"


def test_backend_unavailability_becomes_transport_failure() -> None:
    service = FakeService()

    def unavailable(*args, **kwargs):
        raise DishRuleError(
            "BACKEND_REJECTED",
            "PostgreSQL unavailable",
            rule="postgresql_authority_unavailable",
            retryable=True,
        )

    service.execute_agent = unavailable  # type: ignore[method-assign]
    adapter = native_mcp_server.NativeMCPAdapter(service, owner_id="chatgpt-project")

    with pytest.raises(native_mcp_server.NativeMCPRuntimeError):
        adapter.call_tool(
            "dish_sections",
            {"client": {"run_id": RUN_ID}, "arguments": {}},
        )


def test_server_advertises_exact_tools_schemas_annotations_and_instructions() -> None:
    tools = native_mcp_server._mcp_tools()
    assert len(tools) == 18
    assert [tool.name for tool in tools] == [
        spec.tool_name for spec in CONNECTED_COMMAND_SPECS
    ]
    assert all(tool.output_schema is not None for tool in tools)
    options = native_mcp_server.create_server(
        native_mcp_server.NativeMCPAdapter(FakeService(), owner_id="chatgpt-project")
    ).create_initialization_options()
    assert options.instructions == native_mcp_server.SERVER_INSTRUCTIONS
    assert "MCP connection" in options.instructions
    assert "client.request_id" in options.instructions
    assert "allowed_actions" in options.instructions
    assert "human_action" in options.instructions


def test_non_connected_tool_is_rejected_before_service_dispatch() -> None:
    service = FakeService()
    adapter = native_mcp_server.NativeMCPAdapter(service, owner_id="chatgpt-project")

    with pytest.raises(native_mcp_server.NativeMCPRuntimeError):
        adapter.call_tool(
            "dish_qualify_file_transport",
            {"client": {"run_id": RUN_ID}, "arguments": {}},
        )
    assert service.calls == []


def test_runtime_refuses_asana_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DISH_PROFILE", "test")
    monkeypatch.setenv("DISH_ASANA_TOKEN", "must-not-exist")
    with pytest.raises(DishRuleError) as exc_info:
        native_mcp_server.runtime_service_from_environment()
    assert exc_info.value.rule == "postgresql_native_mcp_asana_environment_forbidden"


def test_runtime_uses_direct_postgres_identity_without_action_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    generation = "33333333-3333-4333-8333-333333333333"
    for key in list(__import__("os").environ):
        if "ASANA" in key:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DISH_PROFILE", "test")
    monkeypatch.setenv(
        "DISH_PG_DATABASE_URL", "postgresql+psycopg://dish:test@127.0.0.1/dish_native_test"
    )
    monkeypatch.setenv("DISH_PG_EXPECTED_DATABASE_NAME", "dish_native_test")
    monkeypatch.setenv("DISH_PG_EXPECTED_SCHEMA_HEAD", "head-1")
    monkeypatch.setenv("DISH_PG_EXPECTED_RELEASE", "release-1")
    monkeypatch.setenv("DISH_PG_EXPECTED_GENERATION_ID", generation)
    monkeypatch.setenv("DISH_PG_CURSOR_SECRET", "x" * 24)
    monkeypatch.setenv("DISH_PG_AUTHORITY_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("DISH_CONNECTED_AGENT_OWNER_ID", "chatgpt-project")
    monkeypatch.setattr(native_mcp_server, "ALEMBIC_HEAD", "head-1")
    captured: dict[str, Any] = {}

    class FakeRuntime:
        def __init__(self, config, **kwargs) -> None:
            captured["config"] = config
            captured.update(kwargs)

        def startup_check(self) -> dict[str, Any]:
            return {"ok": True, "isolation": {"asana_environment_keys": []}}

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(native_mcp_server, "PostgresRuntimeService", FakeRuntime)

    runtime = native_mcp_server.runtime_service_from_environment()

    assert runtime is not None
    assert captured["profile"] == "test"
    assert captured["expected_database"] == "dish_native_test"
    assert captured["expected_generation_id"] == __import__("uuid").UUID(generation)
    assert captured["config"].action_token is None
    assert captured["config"].action_client_id == "chatgpt-project"
