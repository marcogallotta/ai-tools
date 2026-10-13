"""Planning context MCP boundary tests (no database or remote Dish access)."""
from __future__ import annotations

from pathlib import Path
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
    def __init__(self, expected_agent="gpt"):
        self.calls = []
        self.expected_agent = expected_agent

    def call(self, tool: str, request: dict, *, caller: dict) -> dict:
        assert caller == CALLER
        assert request["client"] == {"run_id": RUN}
        args = request["arguments"]
        assert args["agent"] == self.expected_agent
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
    assert bundle["receipt"].splitlines() == [
        "Context loaded (block: wok-depth-rice)",
        "Docs: dish-planning-protocol, cuisine-map, dish-classes, blocks/wok-depth-rice, "
        "blocks/wok-heat (parent)",
        "Cook history: 2 dishes cooked in total",
        "Logs read: Chengdu egg fried rice (1), Other food (1)",
        "Finalists checked: none yet",
    ]
    assert bundle["history"]["all_cooked"][0] == {
        "title": "Chengdu egg fried rice", "dish_id": DISH,
        "last_cooked": "2026-10-10T10:00:00Z",
    }
    assert "latest_qualifying_update_at" not in bundle["history"]["all_cooked"][0]
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


def test_receipt_is_bound_to_owner_and_run_without_expiry_or_main_check(tmp_path):
    store = PlanningReceiptStore(tmp_path / "state" / "context.sqlite3")
    with pytest.raises(PlanningContextError, match="No planning-context receipt"):
        store.require(owner=OWNER, run_id=RUN)
    store.record(owner=OWNER, run_id=RUN, sha=SHA, scope="dish", target=DISH)
    store.require(owner=OWNER, run_id=RUN)
    # A receipt remains valid after repo changes and irrespective of its timestamp.
    import sqlite3
    with sqlite3.connect(store.path) as conn:
        conn.execute("UPDATE context_receipts SET sha=?, created_at=0", ("0" * 40,))
    store.require(owner=OWNER, run_id=RUN)
    with pytest.raises(PlanningContextError, match="dish_planning_context"):
        store.require(owner="other-owner", run_id=RUN)
    with pytest.raises(PlanningContextError, match="dish_planning_context"):
        store.require(owner=OWNER, run_id=OTHER)


def test_receipt_rejects_noncanonical_run_id(tmp_path):
    store = PlanningReceiptStore(tmp_path / "receipt.sqlite3")
    with pytest.raises(PlanningContextError, match="canonical"):
        store.record(owner=OWNER, run_id=DISH.upper(), sha=SHA, scope="broad", target="all")


def test_full_cooked_history_exhausts_all_pages():
    class Paged(Adapter):
        def call(self, tool, request, *, caller):
            if tool == "dish_cooked_updates":
                args = request["arguments"]
                if args["since"] != "1970-01-01T00:00:00Z":
                    return {"ok": True, "data": {
                        "generation_id": "33333333-3333-4333-8333-333333333333",
                        "updates": [], "next_cursor": None,
                    }}
                assert request["client"] == {"run_id": RUN}
                if "cursor" not in args:
                    return {"ok": True, "data": {
                        "generation_id": "33333333-3333-4333-8333-333333333333",
                        "updates": [{"dish_id": DISH, "title": "Chengdu egg fried rice"}],
                        "next_cursor": "nextpage",
                    }}
                assert args["generation_id"] == "33333333-3333-4333-8333-333333333333"
                assert args["cursor"] == "nextpage"
                return {"ok": True, "data": {
                    "generation_id": args["generation_id"],
                    "updates": [{"dish_id": OTHER, "title": "Other food"}],
                    "next_cursor": None,
                }}
            return super().call(tool, request, caller=caller)
    value = context(Paged(), scope="broad", target="")
    assert len(value["history"]["all_cooked"]) == 2
    assert value["history"]["cook_logs"] == []
    assert value["history"]["logged_dish_ids"] == []


def test_changed_honest_main_between_parent_and_block_fails_closed():
    counter = {"number": 0}

    def drifting(checkout, paths):
        counter["number"] += 1
        payload = read_docs(checkout, paths)
        if counter["number"] > 1:
            payload["sha"] = "d" * 40
        return payload

    with pytest.raises(PlanningContextError, match="main changed"):
        assemble_planning_context(
            Adapter(), CALLER, Path("/fake/honest"), drifting,
            run_id=RUN, agent="gpt", scope="block", target="wok-depth-rice",
        )


def test_named_dish_reads_dish_before_any_repo_document():
    events = []

    class RecordingAdapter(Adapter):
        def call(self, tool, request, *, caller):
            events.append(tool)
            return super().call(tool, request, caller=caller)

    def recording_read(checkout, paths):
        events.append("read_honest")
        return read_docs(checkout, paths)

    assemble_planning_context(
        RecordingAdapter(), CALLER, Path("/fake/honest"), recording_read,
        run_id=RUN, agent="gpt", scope="dish", target="Chengdu egg fried rice",
    )
    assert events[:3] == ["dish_search", "dish_read", "read_honest"]


def test_named_dish_rejects_nonexact_search_hits_before_document_read():
    class Inexact(Adapter):
        def call(self, tool, request, *, caller):
            if tool == "dish_search":
                return {"ok": True, "data": {
                    "results": [{"dish_id": DISH, "title": "Chengdu egg rice"}],
                    "next_cursor": None,
                }}
            return super().call(tool, request, caller=caller)

    with pytest.raises(PlanningContextError, match="possible matches"):
        context(Inexact(), scope="dish", target="Chengdu egg fried rice")



def test_focus_dish_ids_fetches_all_selected_logs_without_full_inventory_fanout():
    class OldInventory(Adapter):
        def call(self, tool, request, *, caller):
            if tool == "dish_cooked_updates":
                args = request["arguments"]
                if args["since"] != "1970-01-01T00:00:00Z":
                    return {"ok": True, "data": {
                        "generation_id": "33333333-3333-4333-8333-333333333333",
                        "updates": [], "next_cursor": None,
                    }}
            return super().call(tool, request, caller=caller)

    adapter = OldInventory()
    value = context(adapter, scope="broad", target="", focus_dish_ids=[DISH])
    assert len(value["history"]["all_cooked"]) == 2
    assert len(value["history"]["cook_logs"]) == 1
    assert value["history"]["logged_dish_ids"] == [DISH]
    assert [x for x, _ in adapter.calls].count("dish_cook_logs") == 1
    assert "finalists" in value["history"]["coverage"]
    assert "Finalists checked: Chengdu egg fried rice" in value["receipt"]


def test_invalid_focus_ids_do_not_fetch_history():
    with pytest.raises(PlanningContextError, match="focus_dish_ids"):
        context(scope="broad", target="", focus_dish_ids=["not-a-uuid"])


def test_named_block_ignores_registered_but_uncooked_dish_references():
    uncooked = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

    def with_uncooked_reference(checkout, paths):
        payload = read_docs(checkout, paths)
        for entry in payload["files"]:
            if entry["path"] == "planning/blocks/wok-depth-rice.md":
                entry["text"] += f"\nCandidate not cooked: {uncooked}\n"
        return payload

    adapter = Adapter()
    bundle = assemble_planning_context(
        adapter, CALLER, Path("/fake/honest"), with_uncooked_reference,
        run_id=RUN, agent="gpt", scope="block", target="wok-depth-rice",
    )
    assert bundle["ok"] is True
    assert uncooked not in bundle["history"]["logged_dish_ids"]
    queried = [
        args["dish_id"] for tool, args in adapter.calls
        if tool == "dish_cook_logs"
    ]
    assert uncooked not in queried


@pytest.mark.parametrize("agent", ["gpt", "claude", "codex"])
def test_same_mcp_context_route_supports_each_agent(agent):
    adapter = Adapter(expected_agent=agent)
    loaded = context(adapter, agent=agent, scope="block", target="wok-depth-rice")
    assert loaded["ok"] is True
    assert loaded["receipt"].startswith("Context loaded (block: wok-depth-rice)")
    assert any(tool == "dish_cook_logs" for tool, _ in adapter.calls)


def test_bundle_size_cap_is_150k_characters(monkeypatch):
    import dish_service.planning_context as mod
    assert mod.MAX_CONTEXT_CHARS == 150_000

    def oversized(checkout, paths):
        payload = read_docs(checkout, paths)
        payload["files"][0]["text"] = "X" * mod.MAX_CONTEXT_CHARS
        return payload

    with pytest.raises(PlanningContextError, match="Context exceeds"):
        assemble_planning_context(
            Adapter(), CALLER, Path("/fake/honest"), oversized,
            run_id=RUN, agent="gpt", scope="broad",
        )


def test_receipt_names_each_checked_dish_and_true_log_counts():
    class VariedLogs(Adapter):
        def call(self, tool, request, *, caller):
            if tool == "dish_cook_logs":
                dish_id = request["arguments"]["dish_id"]
                n = 3 if dish_id == DISH else 2
                return {"ok": True, "data": {
                    "logs": [{"log_id": str(uuid.uuid4()), "text": "tasting"} for _ in range(n)],
                    "next_cursor": None,
                }}
            return super().call(tool, request, caller=caller)

    result = context(VariedLogs(), focus_dish_ids=[OTHER, DISH])
    receipt = result["receipt"]
    assert "Logs read: Chengdu egg fried rice (3), Other food (2)" in receipt
    assert "Finalists checked: Chengdu egg fried rice, Other food" in receipt


def test_uncooked_finalist_uses_canonical_title_and_zero_logs():
    uncooked = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

    class Uncompleted(Adapter):
        def call(self, tool, request, *, caller):
            if request["arguments"].get("dish_id") == uncooked:
                if tool == "dish_cook_logs":
                    return {"ok": True, "data": {"logs": [], "next_cursor": None}}
                if tool == "dish_read":
                    return {"ok": True, "data": {
                        "dish_id": uncooked, "title": "Chinese steamed egg",
                    }}
            return super().call(tool, request, caller=caller)

    adapter = Uncompleted()
    result = context(adapter, scope="broad", target="", focus_dish_ids=[uncooked])
    assert "Finalists checked: Chinese steamed egg" in result["receipt"]
    assert "Chinese steamed egg (1)" not in result["receipt"]
    assert uncooked in result["history"]["logged_dish_ids"]
    assert any(tool == "dish_read" for tool, _ in adapter.calls)
