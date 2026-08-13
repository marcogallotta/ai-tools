from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from test_selection.model import load_policy
from test_selection.planner import build_plan
from test_selection.preflight import (
    PreflightCheck,
    PreflightResult,
    run_preflight,
    select_preflight_contract_tests,
)
from test_selection.validator import ValidationResult

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "test_selection" / "ownership.csv"


def test_result_has_stable_human_and_json_shape_without_downstream_execution_claims() -> None:
    result = PreflightResult(
        changed_paths=("dish_tool/review_queue.py",),
        selected_downstream_lanes=("smoke",),
        checks=(
            PreflightCheck("ownership-map", True, "valid"),
            PreflightCheck("cheap-contracts", True, "none selected"),
        ),
    )

    payload = json.loads(result.to_json())
    assert payload == {
        "format": "dish-test-preflight-v1",
        "passed": True,
        "changed_paths": ["dish_tool/review_queue.py"],
        "selected_downstream_lanes": ["smoke"],
        "contract_tests": [],
        "checks": [
            {"name": "ownership-map", "passed": True, "reason": "valid"},
            {"name": "cheap-contracts", "passed": True, "reason": "none selected"},
        ],
    }
    text = result.to_text()
    assert "Dish test preflight: PASS" in text
    assert "Selected downstream lanes (not run by preflight):" in text
    assert "smoke" in text


def test_structural_validation_failure_stops_before_selection_or_pytest(monkeypatch) -> None:
    import test_selection.preflight as preflight

    monkeypatch.setattr(
        preflight,
        "validate_policy",
        lambda **_kwargs: ValidationResult(
            row_count=1,
            expected_repo_paths=2,
            collection_checked=False,
            errors=("unclassified in-scope repository paths",),
            warnings=(),
        ),
    )
    monkeypatch.setattr(
        preflight,
        "build_plan",
        lambda *_args, **_kwargs: pytest.fail("planner must not run after structural failure"),
    )
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("pytest must not run after structural failure"),
    )

    result = run_preflight(["dish_tool/review_queue.py"])

    assert result.passed is False
    assert result.selected_downstream_lanes == ()
    assert [check.name for check in result.checks] == ["ownership-map"]


def test_governed_baseline_sources_select_only_tagged_cheap_contracts() -> None:
    policy = load_policy(POLICY)
    plan = build_plan(["dish_service/admin_cli.py"], policy_path=POLICY)

    selected = select_preflight_contract_tests(
        changed_paths=["dish_service/admin_cli.py"],
        plan=plan,
        policy=policy,
    )

    assert "tests/postgresql/test_stage1_baseline_contract.py" in selected
    assert "tests/postgresql/test_stage_a_acceptance_runner.py" in selected
    assert "tests/postgresql/test_stage_a_release_decomposition.py" not in selected
    assert not any(path.startswith("tests/postgresql/native/") for path in selected)
    assert not any(path.startswith("frontend/tests/") for path in selected)


def test_any_test_surface_selects_global_mock_boundary_contract() -> None:
    policy = load_policy(POLICY)
    plan = build_plan(["tests/test_lease_authority.py"], policy_path=POLICY)

    selected = select_preflight_contract_tests(
        changed_paths=["tests/test_lease_authority.py"],
        plan=plan,
        policy=policy,
    )

    assert "tests/test_test_mock_boundary_contract.py" in selected
    assert "tests/test_lease_authority.py" not in selected


def test_qualification_targets_are_bounded_by_changed_surface() -> None:
    from test_selection.preflight import parallel_safe_qualification_targets_for_changes

    all_reviewed = parallel_safe_qualification_targets_for_changes(["tests/conftest.py"])
    assert all_reviewed
    assert parallel_safe_qualification_targets_for_changes(["docs/testing.md"]) == ()
    first = all_reviewed[0]
    assert parallel_safe_qualification_targets_for_changes([first]) == (first,)


def test_shared_fixture_qualification_regression_stops_before_contract_pytest(monkeypatch) -> None:
    import test_selection.preflight as preflight

    monkeypatch.setattr(
        preflight,
        "parallel_safe_qualification_targets_for_changes",
        lambda *_args, **_kwargs: ("tests/test_commands.py",),
    )
    monkeypatch.setattr(
        preflight,
        "parallel_safe_qualification_reason",
        lambda *_args, **_kwargs: "shared fixtures/helpers changed since parallel review",
    )
    monkeypatch.setattr(
        preflight.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("contract pytest must not run after qualification failure"),
    )

    result = run_preflight(["tests/conftest.py"])

    assert result.passed is False
    check = result.checks[-1]
    assert check.name == "parallel-safe-qualification"
    assert check.passed is False
    assert "shared fixtures/helpers changed" in check.reason
    assert result.selected_downstream_lanes


def test_clean_preflight_runs_only_selected_contract_files_and_not_governed_lanes(monkeypatch) -> None:
    import test_selection.preflight as preflight

    calls: list[list[str]] = []

    class Completed:
        returncode = 0
        stdout = "2 passed\n"

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return Completed()

    monkeypatch.setattr(preflight.subprocess, "run", fake_run)
    monkeypatch.setattr(
        preflight,
        "parallel_safe_qualification_targets_for_changes",
        lambda *_args, **_kwargs: (),
    )

    result = run_preflight(["docs/database-backend-stage-a-baseline.json"])

    assert result.passed is True
    assert len(calls) == 1
    command = calls[0]
    assert command[:4] == [preflight.sys.executable, "-m", "pytest", "-q"]
    assert set(command[4:]) == {
        "tests/postgresql/test_stage1_baseline_contract.py",
        "tests/postgresql/test_stage_a_acceptance_runner.py",
    }
    joined = " ".join(command)
    assert "dish-pg-native-certification" not in joined
    assert "playwright" not in joined
    assert "frontend/tests/browser" not in joined
    assert joined != ".venv/bin/python -m pytest"
    assert "ordinary full suite" not in result.contract_tests
    assert "native PostgreSQL certification" in result.selected_downstream_lanes


def test_cli_json_is_machine_readable_for_environment_light_documentation_change() -> None:
    completed = subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            str(ROOT / "scripts" / "dish-test-preflight"),
            "--path",
            "docs/testing.md",
            "--json",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["format"] == "dish-test-preflight-v1"
    assert payload["passed"] is True
    assert payload["changed_paths"] == ["docs/testing.md"]


def test_cli_allow_empty_keeps_global_ci_preflight_valid() -> None:
    completed = subprocess.run(
        [
            str(ROOT / ".venv" / "bin" / "python"),
            str(ROOT / "scripts" / "dish-test-preflight"),
            "--path",
            "",
            "--allow-empty",
            "--json",
        ],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["changed_paths"] == []
    assert payload["passed"] is True
    assert payload["selected_downstream_lanes"] == []
