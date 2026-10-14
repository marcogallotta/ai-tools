"""MCP presentation tests for Planning receipt delivery and optional write fencing."""
from __future__ import annotations

import asyncio
import json
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
        "canonical_dish_id": None,
        "counts": {"docs": 5, "cooked": 288, "logs": 2, "logged_dishes": 2},
        "receipt": "Context loaded (block: wok-depth-rice)\nDocs: dish-planning-protocol",
    }
    monkeypatch.setattr(mcp_server, "assemble_planning_context", lambda *args, **kwargs: payload)
    res = asyncio.run(tool.run({
        "client": {"run_id": RUN}, "agent": "gpt",
        "scope": "block", "target": "wok-depth-rice",
    }))
    assert res.structured_content["ok"] is True
    assert res.structured_content["code"] == "OK"
    assert res.structured_content["receipt_recorded"] is True
    assert res.structured_content["receipt"] == payload["receipt"]
    assert res.structured_content["counts"] == payload["counts"]
    assert res.content[0].text.startswith("Context loaded (block: wok-depth-rice)")
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


def test_full_payload_is_in_structured_content_once_and_not_repeated_in_text(monkeypatch):
    _caller(monkeypatch)
    tool = mcp_server.PlanningContextTool(FakeAdapter(), Path("/fake"), FakeStore(allow=True))
    payload = {
        "ok": True, "code": "OK", "receipt": "Context loaded (broad: all)",
        "receipt_recorded": False,  # the tool records it after assembly
        "scope": "broad", "target": "all", "canonical_dish_id": None,
        "repository_sha": "c" * 40,
        "counts": {"docs": 6, "cooked": 288, "logs": 0, "logged_dishes": 0},
        "files": [{"path": "planning/roadmap.md", "text": "doc-marker-" + "x" * 8000}],
        "history": {"all_cooked": [{"title": "history-marker"}], "cook_logs": []},
    }
    monkeypatch.setattr(mcp_server, "assemble_planning_context", lambda *a, **kw: payload)
    result = asyncio.run(tool.run({"client": {"run_id": RUN}, "agent": "gpt", "scope": "broad"}))
    # Codex and Claude Code give the model structuredContent alone, so it
    # carries the complete payload.
    full = result.structured_content
    assert full == payload
    assert full["receipt_recorded"] is True
    assert full["files"][0]["text"].startswith("doc-marker-")
    assert full["history"]["all_cooked"][0]["title"] == "history-marker"

    # Text is the receipt plus a small summary, not a second copy.
    text = result.content[0].text
    assert text.startswith(payload["receipt"] + "\n")
    summary = json.loads(text.removeprefix(payload["receipt"] + "\n"))
    assert set(summary) == {
        "ok", "code", "receipt_recorded", "scope", "target",
        "canonical_dish_id", "repository_sha", "counts",
    }
    assert summary["receipt_recorded"] is True
    assert "doc-marker-" not in text
    assert "history-marker" not in text
    assert len(text) < 2_000


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


def test_write_fence_blocks_planning_starts_but_not_other_starts(monkeypatch):
    _caller(monkeypatch)
    definition = next(x for x in mcp_server.TOOLS if x["name"] == "dish_start")
    adapter = FakeAdapter()
    store = FakeStore(allow=False)
    tool = mcp_server.DishTool(
        definition, adapter, planning_store=store, planning_required=True,
    )
    request = {
        "client": {
            "run_id": RUN,
            "request_id": "22222222-2222-4222-8222-222222222222",
        },
        "arguments": {
            "agent": "gpt",
            "dish_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "kind": "planning",
        },
    }
    blocked = asyncio.run(tool.run(request))
    assert blocked.structured_content["code"] == "PLANNING_CONTEXT_REQUIRED"
    assert adapter.calls == []
    assert len(store.checked) == 1

    request["arguments"]["kind"] = "initial"
    allowed = asyncio.run(tool.run(request))
    assert allowed.structured_content["ok"] is True
    assert len(adapter.calls) == 1

