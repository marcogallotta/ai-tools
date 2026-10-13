"""MCP presentation tests for Planning receipt delivery and optional write fencing."""
from __future__ import annotations

import asyncio
from pathlib import Path

from dish_service import mcp_server
from dish_service.planning_context import PlanningContextError, PlanningReceiptStore

RUN = "11111111-1111-4111-8111-111111111111"


class FakeAdapter:
    def __init__(self):
        self.calls = []

    def call(self, tool, args, *, caller):
        self.calls.append((tool, args, caller))
        return {"ok": True, "code": "OK", "data": {"done": True}}


class FakeStore:
    def __init__(self, *, allow):
        self.allow = allow
        self.recorded = []
        self.checked = []

    def record(self, **data):
        self.recorded.append(data)

    def require(self, **data):
        self.checked.append(data)
        if not self.allow:
            raise PlanningContextError("Call dish_planning_context first")


def _caller(monkeypatch):
    monkeypatch.setattr(mcp_server, "get_access_token", lambda: None)
    monkeypatch.setattr(mcp_server, "_caller_audit_context", lambda _: {"subject": "192548"})


def test_planning_context_tool_schema_has_necessary_identity():
    tool = mcp_server.PlanningContextTool(
        FakeAdapter(), Path("/not-used"), PlanningReceiptStore(Path("/not-used/receipt.db")),
    )
    assert tool.name == "dish_planning_context"
    assert set(tool.parameters["required"]) == {"client", "scope", "agent"}


def test_complete_context_is_the_only_receipt_writing_path(monkeypatch):
    _caller(monkeypatch)
    fake = FakeStore(allow=True)
    tool = mcp_server.PlanningContextTool(FakeAdapter(), Path("/fake"), fake)
    payload = {
        "ok": True, "code": "OK",
        "repository_sha": "b" * 40,
        "scope": "block", "target": "wok-depth-rice",
        "canonical_dish_id": None, "receipt": "Context: block wok-depth-rice, 5 docs, 2 cook logs",
    }
    monkeypatch.setattr(mcp_server, "assemble_planning_context", lambda *args, **kwargs: payload)
    res = asyncio.run(tool.run({
        "client": {"run_id": RUN}, "agent": "gpt",
        "scope": "block", "target": "wok-depth-rice",
    }))
    assert res.structured_content["receipt_recorded"] is True
    assert res.content[0].text.startswith("Context: block wok-depth-rice, 5 docs")
    assert '"repository_sha"' in res.content[0].text
    assert fake.recorded == [{
        "owner": "192548", "run_id": RUN, "sha": "b" * 40,
        "scope": "block", "target": "wok-depth-rice",
    }]

    def fails(*args, **kwargs):
        raise PlanningContextError("History changed")
    monkeypatch.setattr(mcp_server, "assemble_planning_context", fails)
    res = asyncio.run(tool.run({
        "client": {"run_id": RUN}, "agent": "gpt", "scope": "broad",
    }))
    assert res.structured_content["ok"] is False
    assert len(fake.recorded) == 1


def test_mcp_write_fence_requires_receipt_before_create(monkeypatch):
    _caller(monkeypatch)
    definition = next(x for x in mcp_server.TOOLS if x["name"] == "dish_create")
    adapter = FakeAdapter()
    store = FakeStore(allow=False)
    tool = mcp_server.DishTool(
        definition, adapter, planning_store=store, planning_required=True,
    )
    args = {
        "client": {"run_id": RUN, "request_id": "22222222-2222-4222-8222-222222222222"},
        "arguments": {"agent": "gpt", "title": "A dish"},
    }
    denied = asyncio.run(tool.run(args))
    assert denied.structured_content["code"] == "PLANNING_CONTEXT_REQUIRED"
    assert adapter.calls == []
    assert len(store.checked) == 1

    store.allow = True
    approved = asyncio.run(tool.run(args))
    assert approved.structured_content["ok"] is True
    assert len(adapter.calls) == 1


def test_unguarded_creation_preserves_existing_behavior(monkeypatch):
    _caller(monkeypatch)
    definition = next(x for x in mcp_server.TOOLS if x["name"] == "dish_create")
    adapter = FakeAdapter()
    tool = mcp_server.DishTool(definition, adapter)
    args = {
        "client": {"run_id": RUN, "request_id": "22222222-2222-4222-8222-222222222222"},
        "arguments": {"agent": "gpt", "title": "A dish"},
    }
    result = asyncio.run(tool.run(args))
    assert result.structured_content["ok"] is True
    assert len(adapter.calls) == 1
