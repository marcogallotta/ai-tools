"""Small failure-boundary tests; real transport/database proof lives beside these."""
from __future__ import annotations

import asyncio

from dish_service.planner_read import build_planner_tool


def test_planner_tool_is_read_only_and_accepts_no_caller_selected_source():
    tool = build_planner_tool()
    assert tool.name == "dish_planner_read"
    assert tool.parameters == {
        "type": "object", "properties": {}, "additionalProperties": False,
    }
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False


def test_missing_configuration_is_explicit(monkeypatch):
    monkeypatch.delenv("DISH_MCP_PLANNER_DATABASE_URL", raising=False)
    result = asyncio.run(build_planner_tool().run({})).structured_content
    assert result["ok"] is False
    assert result["code"] == "PLANNER_NOT_CONFIGURED"
    assert "state" not in result


def test_direct_tool_call_rejects_unknown_arguments(monkeypatch):
    monkeypatch.delenv("DISH_MCP_PLANNER_DATABASE_URL", raising=False)
    result = asyncio.run(build_planner_tool().run({"plan_id": "someone-else"}))
    assert result.structured_content["ok"] is False
    assert result.structured_content["code"] != "PLANNER_NOT_CONFIGURED"


def test_connection_failure_does_not_leak_credentials(monkeypatch):
    monkeypatch.setenv(
        "DISH_MCP_PLANNER_DATABASE_URL",
        "postgresql://secret_user:secret_password@127.0.0.1:1/private_db",
    )
    result = asyncio.run(build_planner_tool().run({})).structured_content
    assert result["code"] == "PLANNER_UNAVAILABLE"
    assert "secret_password" not in str(result)
    assert "secret_user" not in str(result)
    assert "private_db" not in str(result)
