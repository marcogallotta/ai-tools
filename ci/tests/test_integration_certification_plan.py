from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "integration_certification_plan.py"
SCHEMA = ROOT / "ci" / "integration-certification-plan.schema.json"
POLICY = ROOT / "ci" / "integration-certification-policy.json"
SPEC = importlib.util.spec_from_file_location("integration_certification_plan", SCRIPT)
assert SPEC and SPEC.loader
planner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = planner
SPEC.loader.exec_module(planner)

CANDIDATE = "a" * 40
BASE = "b" * 40
MERGE_BASE = "c" * 40
ALL_GROUPS = [
    "python-control-plane",
    "frontend-static",
    "native-postgresql",
    "browser-acceptance",
]


def build(paths: list[str], **kwargs):
    return planner.build_repository_plan(
        paths,
        candidate_sha=CANDIDATE,
        base_sha=BASE,
        merge_base_sha=MERGE_BASE,
        semantic_review_complete=True,
        **kwargs,
    )


def test_cli_requires_explicit_identity_and_complete_supplied_path_set():
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--candidate-sha",
            CANDIDATE,
            "--base-sha",
            BASE,
            "--merge-base-sha",
            MERGE_BASE,
            "--path",
            "tools/asana",
            "--path",
            "ci/repository-bundle.md",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["identity"] == {
        "candidate_sha": CANDIDATE,
        "base_sha": BASE,
        "merge_base_sha": MERGE_BASE,
    }
    assert payload["changed_paths"] == ["ci/repository-bundle.md", "tools/asana"]
    assert payload["force_full"] is False
    assert payload["selected_groups"] == ["python-control-plane"]


def test_output_conforms_to_committed_schema_and_is_deterministic():
    first = build(["tools/asana", "ci/repository-bundle.md", "tools/asana"])
    second = build(["ci/repository-bundle.md", "tools/asana"])
    assert first == second
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator(schema).validate(first)
    source_paths = [item["path"] for item in first["policy_identity"]["sources"]]
    assert source_paths == [
        "ci/integration-certification-policy.json",
        "ci/integration-certification-plan.schema.json",
        "scripts/integration_certification_plan.py",
        "dish/test_selection/ownership.csv",
        "dish/test_selection/model.py",
        "dish/test_selection/planner.py",
    ]


def test_dish_paths_delegate_to_existing_selector_without_repo_path_duplication():
    plan = build(["dish/frontend/fixtures/stage1-board.js"])
    assert plan["classifications"] == [
        {
            "path": "dish/frontend/fixtures/stage1-board.js",
            "scope": "dish",
            "classification": "dish-selector",
        }
    ]
    assert plan["dish_selector"]["changed_paths"] == ["frontend/fixtures/stage1-board.js"]
    assert plan["dish_selector"]["lanes"] == ["frontend check"]
    assert plan["selected_lanes"] == ["frontend check"]
    assert plan["selected_groups"] == ["frontend-static"]
    assert plan["force_full"] is False


def test_browser_lane_is_adapter_addressable_without_changing_dish_selector_semantics():
    plan = build(
        ["dish/frontend/README.md"],
        semantic_additions=["browser acceptance"],
    )
    assert "browser acceptance" in plan["selected_lanes"]
    assert plan["selected_groups"] == ["browser-acceptance"]
    assert plan["force_full"] is False


def test_native_postgresql_lane_maps_to_native_execution_group():
    plan = build(["dish/dish_pg/migration_status.py"])
    assert "native PostgreSQL certification" in plan["selected_lanes"]
    assert plan["selected_groups"] == ["native-postgresql"]
    assert plan["force_full"] is False


def test_unresolved_dish_semantic_predicates_force_full_until_review_disposes_them():
    unresolved = planner.build_repository_plan(
        ["dish/dish_tool/application_service.py"],
        candidate_sha=CANDIDATE,
        base_sha=BASE,
        merge_base_sha=MERGE_BASE,
    )
    assert unresolved["dish_selector"]["conditional_reviews"]
    assert unresolved["force_full"] is True
    assert "unresolved-dish-semantic-review" in unresolved["force_full_reasons"]
    assert unresolved["selected_groups"] == ALL_GROUPS

    resolved = build(["dish/dish_tool/application_service.py"])
    assert resolved["force_full"] is False
    assert resolved["selected_groups"] == ["python-control-plane"]


def test_certification_self_governance_forces_all_groups():
    plan = build(["scripts/integration_certification_plan.py"])
    assert plan["force_full"] is True
    assert plan["selected_groups"] == ALL_GROUPS
    assert any(
        reason.startswith("certification-planner-self-change:")
        for reason in plan["force_full_reasons"]
    )
    assert {
        "repository control-plane",
        "ordinary full suite",
        "frontend static",
        "native PostgreSQL certification",
        "browser acceptance",
    }.issubset(plan["selected_lanes"])


@pytest.mark.parametrize(
    "path",
    [
        ".github/workflows/ci.yml",
        "scripts/pr_gate.py",
        "scripts/pr_lifecycle.py",
        "dish/test_selection/ownership.csv",
        "dish/scripts/dish-test-plan",
    ],
)
def test_known_certification_authority_changes_force_full(path: str):
    plan = build([path])
    assert plan["force_full"] is True
    assert plan["selected_groups"] == ALL_GROUPS


def test_unclassified_dish_path_fails_closed_to_full_certification():
    plan = build(["dish/new_unmapped_surface.py"])
    assert plan["classifications"] == [
        {
            "path": "dish/new_unmapped_surface.py",
            "scope": "dish",
            "classification": "dish-selector-failed-closed",
        }
    ]
    assert plan["force_full"] is True
    assert plan["selected_groups"] == ALL_GROUPS
    assert any(
        reason.startswith("dish-selector-failed-closed:unclassified changed paths")
        for reason in plan["force_full_reasons"]
    )


def test_unknown_repository_path_fails_closed_to_full_certification():
    plan = build(["unexpected-surface.bin"])
    assert plan["classifications"] == [
        {
            "path": "unexpected-surface.bin",
            "scope": "repository",
            "classification": "unclassified",
        }
    ]
    assert plan["force_full"] is True
    assert plan["selected_groups"] == ALL_GROUPS
    assert plan["force_full_reasons"] == [
        "unclassified-repository-path:unexpected-surface.bin"
    ]


def test_ambiguous_repository_policy_match_fails_closed(tmp_path: Path):
    policy = json.loads(POLICY.read_text(encoding="utf-8"))
    policy["repository_rules"].append(
        {
            "name": "second-tools-owner",
            "pattern": "tools/**",
            "lanes": ["repository control-plane"],
            "groups": ["python-control-plane"],
        }
    )
    policy_path = tmp_path / "policy.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")

    plan = planner.build_repository_plan(
        ["tools/asana"],
        candidate_sha=CANDIDATE,
        base_sha=BASE,
        merge_base_sha=MERGE_BASE,
        semantic_review_complete=True,
        policy_path=policy_path,
        schema_path=SCHEMA,
    )
    assert plan["classifications"] == [
        {"path": "tools/asana", "scope": "repository", "classification": "ambiguous"}
    ]
    assert plan["force_full"] is True
    assert plan["selected_groups"] == ALL_GROUPS
    assert plan["force_full_reasons"] == [
        "ambiguous-repository-path:tools/asana:second-tools-owner,tools"
    ]


def test_noncanonical_paths_and_unknown_semantic_additions_are_rejected():
    with pytest.raises(planner.CertificationPlanError, match="canonical repository-relative"):
        build(["dish/../scripts/pr_gate.py"])
    with pytest.raises(planner.CertificationPlanError, match="unknown semantic"):
        build(["tools/asana"], semantic_additions=["skip expensive things"])
