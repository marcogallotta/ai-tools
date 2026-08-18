from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "test_impact_graph.py"
SPEC = importlib.util.spec_from_file_location("test_impact_graph", SCRIPT)
assert SPEC and SPEC.loader
graph = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = graph
SPEC.loader.exec_module(graph)


def envelope(paths: list[str], provenance: str, obligations: list[dict[str, object]]):
    return {
        "format": "dish-test-obligations-v1",
        "provenance": provenance,
        "engine_identity": "a" * 64,
        "changed_paths": paths,
        "obligations": [{**item, "provenance": provenance} for item in obligations],
    }


def obligation(path: str, key: str, boundary: str, *, preferred: list[str] | None = None):
    return {
        "path": path,
        "key": key,
        "guarantee": key,
        "execution_boundary": boundary,
        "preferred_targets": preferred or [],
        "fallback_target": f"fallback:{boundary}",
    }


def test_catalog_bootstraps_owned_test_files_with_stable_target_ids():
    first = graph.load_targets()
    second = graph.load_targets()
    target_id = "dish-pytest:tests/test_application_service.py"
    assert first == second
    assert target_id in first
    assert first[target_id]["generated_from"] == "dish/test_selection/ownership.csv"
    assert len(first) >= 376


def test_python_import_edges_are_additive_target_evidence():
    targets = graph.load_targets()
    selected = graph._static_python_targets("dish/dish_tool/application_service.py", targets, ROOT)
    assert "dish-pytest:tests/test_application_service.py" in selected


def test_authoritative_patterns_are_rejected_in_v1(tmp_path: Path):
    targets = graph.load_targets()
    path = tmp_path / "edges.json"
    path.write_text(json.dumps({
        "format": "dish-test-impact-edges-v1",
        "mappings": [{
            "pattern": "dish/**/*.py", "mode": "authoritative",
            "targets": ["fallback:python-control-plane"], "legacy_dispositions": [],
        }],
    }), encoding="utf-8")
    with pytest.raises(graph.GraphError, match="exact-path-only"):
        graph.load_edges(targets, path)


def test_candidate_added_path_cannot_become_authoritative():
    path = "scripts/pr_lifecycle.py"
    base = graph.build_legacy_envelope([path], provenance="base")
    candidate = graph.build_legacy_envelope([path], provenance="candidate")
    with pytest.raises(graph.GraphError, match="both BASE and CANDIDATE"):
        graph.build_graph_plan(
            [path], base_envelope=base, candidate_envelope=candidate,
            base_paths=[], candidate_paths=[path],
        )


def test_authoritative_cutover_uses_exact_base_candidate_disposition_union():
    path = "scripts/pr_lifecycle.py"
    base = graph.build_legacy_envelope([path], provenance="base")
    candidate = graph.build_legacy_envelope([path], provenance="candidate")
    base["obligations"].append({
        **obligation(path, "legacy-boundary:python-control-plane:base-only-L", "python-control-plane"),
        "provenance": "base",
    })
    with pytest.raises(graph.GraphError, match="disposition mismatch"):
        graph.build_graph_plan(
            [path], base_envelope=base, candidate_envelope=candidate,
            base_paths=[path], candidate_paths=[path],
        )


def test_mapped_disposition_cannot_cross_execution_boundary():
    path = "scripts/pr_lifecycle.py"
    base = graph.build_legacy_envelope([path], provenance="base")
    candidate = graph.build_legacy_envelope([path], provenance="candidate")
    for payload in (base, candidate):
        mapped = next(
            item for item in payload["obligations"]
            if item["key"] == "legacy-boundary:python-control-plane:repository control-plane"
        )
        mapped["execution_boundary"] = "native-postgresql"
        mapped["fallback_target"] = "fallback:native-postgresql"
    with pytest.raises(graph.GraphError, match="crosses execution boundaries"):
        graph.build_graph_plan(
            [path], base_envelope=base, candidate_envelope=candidate,
            base_paths=[path], candidate_paths=[path],
        )


def test_unmigrated_path_keeps_base_only_obligation_through_same_boundary_fallback():
    path = "unexpected.bin"
    base = envelope([path], "base", [
        obligation(path, "legacy-target:removed", "browser-acceptance", preferred=["removed-target"])
    ])
    candidate = envelope([path], "candidate", [])
    plan = graph.build_graph_plan([path], base_envelope=base, candidate_envelope=candidate)
    assert plan["legacy_adapter_paths"] == [path]
    assert plan["impact_fingerprint"]["target_ids"] == [
        "fallback:browser-acceptance", "fallback:frontend-static"
    ]
    assert plan["all_boundary_fallback"] is False


def test_removed_graph_target_needs_machine_readable_retirement_disposition():
    path = "unexpected.bin"
    base = envelope([path], "base", [
        obligation(path, "graph-target:removed-target", "python-control-plane", preferred=["removed-target"])
    ])
    candidate = envelope([path], "candidate", [])
    with pytest.raises(graph.GraphError, match="target_retirements"):
        graph.build_graph_plan([path], base_envelope=base, candidate_envelope=candidate)


def test_missing_base_engine_or_incompatible_arbiter_fails_all_boundaries():
    path = "scripts/test_impact_graph.py"
    candidate = graph.build_legacy_envelope([path], provenance="candidate")
    unavailable = graph.build_graph_plan([path], base_envelope=None, candidate_envelope=candidate)
    incompatible = graph.build_graph_plan(
        [path], base_envelope={**candidate, "provenance": "base", "obligations": [
            {**item, "provenance": "base"} for item in candidate["obligations"]
        ]}, candidate_envelope=candidate, arbiter_compatible=False,
    )
    assert unavailable["all_boundary_fallback"] is True
    assert incompatible["all_boundary_fallback"] is True
    assert unavailable["selected_groups"] == list(graph.BOUNDARIES)


def test_arbiter_self_change_uses_base_arbiter_union_not_candidate_union(monkeypatch):
    path = "scripts/test_impact_arbiter.py"
    target_id = "repo-pytest:ci/tests/test_pr_lifecycle.py"
    base = envelope([path], "base", [
        obligation(path, f"graph-target:{target_id}", "python-control-plane", preferred=[target_id])
    ])
    candidate = envelope([path], "candidate", [])
    trusted = graph.arbiter.union_envelopes(base, candidate)

    # Simulate the exact Review blocker: candidate arbiter stays format-compatible
    # but maliciously drops every BASE-only obligation.
    monkeypatch.setattr(graph.arbiter, "union_envelopes", lambda *_: {
        "format": graph.arbiter.UNION_FORMAT,
        "base_engine_identity": "a" * 64,
        "candidate_engine_identity": "a" * 64,
        "base_obligation_digest": "0" * 64,
        "candidate_obligation_digest": "0" * 64,
        "union_digest": "0" * 64,
        "semantic_keys": [],
        "obligations": [],
    })

    plan = graph.build_graph_plan(
        [path], base_envelope=base, candidate_envelope=candidate,
        base_arbiter_union=trusted, base_paths=[path], candidate_paths=[path],
    )
    selected = {
        item["id"] for item in [*plan["selected_targets"], *plan["hosted_required_targets"]]
    }
    assert target_id in selected
    assert plan["obligation_union"] == trusted
    assert plan["all_boundary_fallback"] is False

    missing = graph.build_graph_plan(
        [path], base_envelope=base, candidate_envelope=candidate,
        base_paths=[path], candidate_paths=[path],
    )
    assert missing["all_boundary_fallback"] is True
    assert missing["all_boundary_fallback_reasons"] == [
        "base-arbiter-union-unavailable-for-self-change"
    ]


def test_execution_guard_runtime_edge_selects_real_mutation_workspace():
    path = "dish/test_selection/execution_guard.py"
    base = graph.build_legacy_envelope([path], provenance="base")
    candidate = graph.build_legacy_envelope([path], provenance="candidate")
    plan = graph.build_graph_plan([path], base_envelope=base, candidate_envelope=candidate)
    assert "harness:mutation-real-workspace" in plan["impact_fingerprint"]["target_ids"]
    target = next(item for item in plan["selected_targets"] if item["id"] == "harness:mutation-real-workspace")
    assert "subprocess" in target["requirements"]


def test_local_fast_reports_large_targets_as_hosted_required():
    path = "unknown.bin"
    base = graph.build_legacy_envelope([path], provenance="base")
    candidate = graph.build_legacy_envelope([path], provenance="candidate")
    plan = graph.build_graph_plan(
        [path], base_envelope=base, candidate_envelope=candidate, profile="LOCAL_FAST"
    )
    assert plan["selected_targets"] == []
    assert {item["id"] for item in plan["hosted_required_targets"]} == set(graph.ALL_FALLBACKS.values())


def test_fingerprint_is_deterministic_and_advisory_only():
    paths = ["scripts/pr_lifecycle.py"]
    base = graph.build_legacy_envelope(paths, provenance="base")
    candidate = graph.build_legacy_envelope(paths, provenance="candidate")
    first = graph.build_graph_plan(paths, base_envelope=base, candidate_envelope=candidate)
    second = graph.build_graph_plan(paths, base_envelope=base, candidate_envelope=candidate)
    assert first["impact_fingerprint"] == second["impact_fingerprint"]
    fingerprint = first["impact_fingerprint"]
    assert fingerprint["format"] == "dish-impact-fingerprint-v1"
    assert not ({"conflict", "owner", "scheduler", "merge"} & set(fingerprint))


def test_predicted_path_fingerprint_is_explicit_not_inferred_from_prose():
    plan = graph.build_graph_plan(
        ["scripts/pr_lifecycle.py"],
        base_envelope=graph.build_legacy_envelope(["scripts/pr_lifecycle.py"], provenance="base"),
        candidate_envelope=graph.build_legacy_envelope(["scripts/pr_lifecycle.py"], provenance="candidate"),
    )
    plan["impact_fingerprint"]["input_mode"] = "explicit_predicted_paths"
    assert plan["impact_fingerprint"]["paths"] == ["scripts/pr_lifecycle.py"]
    assert plan["impact_fingerprint"]["input_mode"] == "explicit_predicted_paths"


def test_historical_replay_including_selector_miss_passes():
    result = graph.replay()
    assert result["passed"] is True
    selector_miss = next(item for item in result["cases"] if item["id"] == "31955770608")
    assert "harness:mutation-real-workspace" in selector_miss["selected_targets"]
