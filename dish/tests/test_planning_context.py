"""Planning context MCP boundary tests (no database or remote Dish access)."""
from __future__ import annotations

from pathlib import Path
import subprocess
import uuid

import pytest
from dish_service.planning_context import (
    PlanningContextError,
    PlanningReceiptStore,
    assemble_planning_context,
)

RUN = "11111111-1111-4111-8111-111111111111"
OWNER = "192548"
CALLER = {"subject": OWNER}
DISH = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
SHA = "c" * 40
BLOCK_INDEX = (
    "| Programme | State | Current position | Next action |\n"
    "| --- | --- | --- | --- |\n"
    "| [\x60wok-depth-rice.md\x60](wok-depth-rice.md) | Ready | Egg and rice char | Review |\n"
    "| [\x60wok-depth-noodles.md\x60](wok-depth-noodles.md) | Ready | Noodle char | Review |\n"
)
DOCS = {
    "dish-planning-protocol.md": "# Planning Protocol\n",
    "planning/index.md": "# Planning Index\n",
    "planning/roadmap.md": "# Roadmap\n",
    "cuisine-map.md": "# Cuisine Map\n",
    "dish-classes.md": "# Dish Classes\n",
    "planning/blocks/index.md": BLOCK_INDEX,
    "planning/blocks/wok-depth-rice.md":
        "# Fried Rice\nA Round 2 child of [Wok Heat](wok-heat.md).\n",
    "planning/blocks/wok-heat.md": "# Wok Heat parent\n",
}


def read_docs(checkout: Path, paths: list[str]) -> dict:
    assert checkout == Path("/fake/honest")
    return {
        "sha": SHA,
        "files": [{"path": path, "text": DOCS[path]} for path in paths],
    }


class Adapter:
    def __init__(self):
        self.calls = []

    def call(self, tool: str, request: dict, *, caller: dict) -> dict:
        assert caller == CALLER
        assert request["client"] == {"run_id": RUN}
        args = request["arguments"]
        assert args["agent"] == "gpt"
        self.calls.append((tool, args))
        if tool == "dish_cooked_updates":
            records = [
                {"dish_id": DISH, "title": "Chengdu egg fried rice", "current_cooked_at": "2026-10-10T10:00:00Z"},
                {"dish_id": OTHER, "title": "Other food", "current_cooked_at": "2026-10-09T10:00:00Z"},
            ]
            return {"ok": True, "data": {
                "generation_id": "33333333-3333-4333-8333-333333333333",
                "updates": records, "next_cursor": None,
            }}
        if tool == "dish_cook_logs":
            return {"ok": True, "data": {
                "logs": [{"log_id": str(uuid.uuid4()), "text": "good result"}],
                "next_cursor": None,
            }}
        if tool == "dish_search":
            results = [{"dish_id": DISH, "title": "Chengdu egg fried rice"}]
            return {"ok": True, "data": {"results": results, "next_cursor": None}}
        if tool == "dish_read":
            return {"ok": True, "data": {"dish_id": args["dish_id"], "notes": "verified"}}
        raise AssertionError(f"unexpected tool: {tool}")


def context(adapter=None, **overrides):
    adapter = adapter or Adapter()
    params = {
        "run_id": RUN,
        "agent": "gpt",
        "scope": "block",
        "target": "wok-depth-rice",
    }
    params.update(overrides)
    return assemble_planning_context(
        adapter, CALLER, Path("/fake/honest"), read_docs, **params,
    )


def test_named_block_loads_its_registered_row_parent_protocol_classes_and_history():
    adapter = Adapter()
    bundle = context(adapter)
    paths = [entry["path"] for entry in bundle["files"]]
    assert paths == [
        "dish-planning-protocol.md", "cuisine-map.md", "dish-classes.md",
        "planning/blocks/wok-depth-rice.md", "planning/blocks/wok-heat.md",
    ]
    assert "wok-depth-rice.md" in bundle["block_index_row"]
    assert bundle["receipt"].startswith("Context: block wok-depth-rice, 5 docs, 2 cook logs")
    assert bundle["history"]["all_cooked"][0]["dish_id"] == DISH
    assert len(bundle["history"]["recent_updates"]) == 2
    assert [tool for tool, _ in adapter.calls].count("dish_cooked_updates") == 2
    assert [tool for tool, _ in adapter.calls].count("dish_cook_logs") == 2


def test_broad_loads_full_discovery_set_without_guessing_a_block():
    bundle = context(scope="broad", target="")
    assert [f["path"] for f in bundle["files"]] == [
        "dish-planning-protocol.md", "planning/index.md",
        "planning/roadmap.md", "cuisine-map.md", "dish-classes.md",
        "planning/blocks/index.md",
    ]


def test_named_dish_loads_single_protocol_canonical_task_and_its_cook_logs():
    adapter = Adapter()
    bundle = context(adapter, scope="dish", target="Chengdu egg fried rice")
    assert bundle["canonical_dish_id"] == DISH
    assert bundle["canonical_dish"]["dish_id"] == DISH
    assert [f["path"] for f in bundle["files"]] == ["dish-planning-protocol.md"]
    assert len(bundle["history"]["recent_updates"]) == 0
    assert len(bundle["history"]["cook_logs"]) == 1


@pytest.mark.parametrize("target", ["rice", "wok-unknown", "../wok-depth-rice"])
def test_block_resolution_fails_closed_without_exact_registry_match(target):
    with pytest.raises(PlanningContextError, match="registered|Block target"):
        context(target=target)


def test_selected_dish_never_reopens_as_a_broad_request():
    with pytest.raises(PlanningContextError, match="broad scope cannot"):
        context(scope="broad", target="Chengdu egg fried rice")


def test_history_failure_issues_no_complete_bundle():
    class Broken(Adapter):
        def call(self, tool, request, *, caller):
            if tool == "dish_cooked_updates":
                return {"ok": False, "code": "GENERATION_CHANGED"}
            return super().call(tool, request, caller=caller)
    with pytest.raises(PlanningContextError, match="GENERATION_CHANGED"):
        context(Broken())


def test_receipt_requires_matching_owner_run_fresh_main_and_dish(monkeypatch, tmp_path):
    store = PlanningReceiptStore(tmp_path / "state" / "context.sqlite3")
    checkout = tmp_path / "honest"
    checkout.mkdir()
    def git_head(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=f"{SHA}\n".encode())
    monkeypatch.setattr("dish_service.planning_context.subprocess.run", git_head)
    with pytest.raises(PlanningContextError, match="No planning-context receipt"):
        store.require(owner=OWNER, run_id=RUN, checkout=checkout)
    store.record(owner=OWNER, run_id=RUN, sha=SHA, scope="dish", target=DISH)
    store.require(owner=OWNER, run_id=RUN, checkout=checkout, dish_id=DISH)
    with pytest.raises(PlanningContextError, match="another dish"):
        store.require(owner=OWNER, run_id=RUN, checkout=checkout, dish_id=OTHER)
    with pytest.raises(PlanningContextError, match="dish_planning_context"):
        store.require(owner="other-owner", run_id=RUN, checkout=checkout)
    monkeypatch.setattr(
        "dish_service.planning_context.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, stdout=b"0" * 40),
    )
    with pytest.raises(PlanningContextError, match="main changed"):
        store.require(owner=OWNER, run_id=RUN, checkout=checkout)


def test_receipt_rejects_noncanonical_run_id(tmp_path):
    store = PlanningReceiptStore(tmp_path / "receipt.sqlite3")
    with pytest.raises(PlanningContextError, match="canonical"):
        store.record(owner=OWNER, run_id=DISH.upper(), sha=SHA, scope="broad", target="all")
