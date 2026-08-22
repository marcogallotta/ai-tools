from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import jsonschema

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "integration_certification_plan.py"
SCHEMA = ROOT / "ci" / "integration-certification-plan.schema.json"
SPEC = importlib.util.spec_from_file_location("integration_certification_plan", SCRIPT)
assert SPEC and SPEC.loader
planner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = planner
SPEC.loader.exec_module(planner)

CANDIDATE = "a" * 40
BASE = "b" * 40
MERGE_BASE = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def build(paths: list[str], **kwargs):
    return planner.build_repository_plan(
        paths,
        candidate_sha=CANDIDATE,
        base_sha=BASE,
        merge_base_sha=MERGE_BASE,
        semantic_review_complete=True,
        **kwargs,
    )


def test_cli_requires_exact_identity_and_complete_paths():
    completed = subprocess.run([
        sys.executable, str(SCRIPT),
        "--candidate-sha", CANDIDATE, "--base-sha", BASE,
        "--merge-base-sha", MERGE_BASE,
        "--path", "tools/asana", "--path", "ci/repository-bundle.md",
    ], cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["format"] == "repository-certification-plan-v2"
    assert payload["identity"] == {
        "candidate_sha": CANDIDATE, "base_sha": BASE, "merge_base_sha": MERGE_BASE,
    }
    assert payload["changed_paths"] == ["ci/repository-bundle.md", "tools/asana"]
    assert payload["legacy_adapter_paths"] == payload["changed_paths"]


def test_output_conforms_to_schema_and_is_deterministic():
    first = build(["tools/asana", "ci/repository-bundle.md", "tools/asana"])
    second = build(["ci/repository-bundle.md", "tools/asana"])
    assert first == second
    jsonschema.Draft202012Validator(json.loads(SCHEMA.read_text())).validate(first)
    source_paths = [item["path"] for item in first["policy_identity"]["sources"]]
    assert source_paths == [
        "ci/integration-certification-policy.json",
        "ci/integration-certification-plan.schema.json",
        "scripts/integration_certification_plan.py",
        "scripts/test_impact_graph.py",
        "scripts/test_impact_arbiter.py",
        "ci/test-impact/targets.json",
        "ci/test-impact/edges.json",
        "ci/test-impact/replay.json",
        "dish/test_selection/ownership.csv",
        "dish/test_selection/ownership/00.csv",
        "dish/test_selection/ownership/01.csv",
        "dish/test_selection/ownership/02.csv",
        "dish/test_selection/model.py",
        "dish/test_selection/planner.py",
    ]


def test_documentation_only_dish_path_remains_no_execution():
    plan = build(["dish/frontend/README.md"])
    assert plan["dish_selector"]["lanes"] == []
    assert plan["selected_targets"] == []
    assert plan["selected_groups"] == []
    assert plan["all_boundary_fallback"] is False


def test_unmigrated_dish_path_preserves_legacy_targets_visibly():
    path = "dish/dish_tool/application_service.py"
    plan = build([path])
    assert plan["legacy_adapter_paths"] == [path]
    assert any(item["id"].endswith("tests/test_application_service.py") for item in plan["selected_targets"])
    assert "python-control-plane" in plan["selected_groups"]


def test_authoritative_lifecycle_mapping_removes_unrelated_global_fanout():
    plan = build(["scripts/pr_lifecycle.py"])
    assert plan["legacy_adapter_paths"] == []
    assert plan["selected_groups"] == ["python-control-plane"]
    assert [item["id"] for item in plan["selected_targets"]] == [
        "repo-pytest:ci/tests/test_pr_lifecycle.py"
    ]
    assert plan["retired_legacy_obligations"]
    assert {item["reason"] for item in plan["retired_legacy_obligations"]} == {"incidental_broad_coverage"}


def test_semantic_addition_is_additive_boundary_fallback():
    plan = build(["dish/frontend/README.md"], semantic_additions=["browser acceptance"])
    assert plan["selected_groups"] == ["frontend-static", "browser-acceptance"]
    assert [item["id"] for item in plan["selected_targets"]] == [
        "fallback:browser-acceptance", "fallback:frontend-static"
    ]


def test_native_owner_target_keeps_native_runtime_identity():
    plan = build(["dish/tests/postgresql/native/test_migration_status.py"])
    native = [item for item in plan["selected_targets"] if item["execution_boundary"] == "native-postgresql"]
    assert any(item["selector"] == "tests/postgresql/native/test_migration_status.py" for item in native)
    assert all("postgresql" in item["requirements"] for item in native)


def test_unknown_path_fails_closed_to_all_boundaries():
    plan = build(["unexpected-surface.bin"])
    assert plan["classifications"][0]["classification"] == "unclassified"
    assert plan["all_boundary_fallback"] is True
    assert plan["selected_groups"] == list(planner.impact_graph.BOUNDARIES)


def test_graph_self_change_without_literal_base_engine_fails_closed():
    plan = build(["scripts/test_impact_graph.py"])
    assert plan["all_boundary_fallback"] is True
    assert plan["all_boundary_fallback_reasons"] == ["base-engine-obligation-envelope-unavailable"]


def test_local_fast_records_large_targets_as_hosted_required():
    plan = build(["unexpected-surface.bin"], profile="LOCAL_FAST")
    assert plan["selected_targets"] == []
    assert {item["id"] for item in plan["hosted_required_targets"]} == set(planner.impact_graph.ALL_FALLBACKS.values())


def test_impact_fingerprint_is_derived_advisory_output():
    plan = build(["scripts/pr_lifecycle.py"])
    fingerprint = plan["impact_fingerprint"]
    assert fingerprint["format"] == "dish-impact-fingerprint-v1"
    assert fingerprint["paths"] == ["scripts/pr_lifecycle.py"]
    assert fingerprint["target_ids"] == ["repo-pytest:ci/tests/test_pr_lifecycle.py"]
    assert not ({"conflict", "owner", "scheduler", "merge"} & set(fingerprint))


def test_replay_corpus_is_a_selector_miss_backstop():
    replay = planner.impact_graph.replay()
    assert replay["passed"] is True
    assert {case["id"] for case in replay["cases"]} >= {"31912433743", "31955770608"}


def test_known_single_boundary_fallback_emits_visible_selector_gap():
    plan = build(["tools/asana"])
    assert plan["selector_classifications"] == [{
        "path": "tools/asana",
        "classification": "KNOWN_BOUNDARY_FALLBACK",
        "retained_boundaries": ["python-control-plane"],
    }]
    assert len(plan["selector_gaps"]) == 1
    assert plan["selector_gaps"][0]["missing_reason"] == "missing-exact-authoritative-mapping"


def test_unclassified_repository_path_is_true_unknown_not_base_union():
    plan = build(["unexpected-surface.bin"])
    assert plan["selector_classifications"] == [{
        "path": "unexpected-surface.bin",
        "classification": "TRUE_UNKNOWN_ALL_BOUNDARY",
        "retained_boundaries": list(planner.impact_graph.BOUNDARIES),
    }]
    assert plan["selector_gaps"] == []
    assert plan["all_boundary_fallback"] is True


def test_pr182_lifecycle_path_narrows_by_exact_mapping_only():
    plan = build(["scripts/pr_lifecycle_controller.py"])
    assert plan["selector_classifications"][0]["classification"] == "EXACT_PROVEN_TARGET"
    assert plan["selected_groups"] == ["python-control-plane"]
    assert [item["id"] for item in plan["selected_targets"]] == [
        "repo-pytest:ci/tests:lifecycle-control-plane"
    ]
    assert plan["selector_gaps"] == []


def test_cross_boundary_certification_orchestrator_does_not_narrow_from_python_path_class():
    plan = build(["scripts/integration_certification.py"])
    assert plan["selector_classifications"][0]["classification"] == "BASE_OBLIGATION_UNION"
    assert plan["selected_groups"] == list(planner.impact_graph.BOUNDARIES)
    assert len(plan["selector_gaps"]) == 1
