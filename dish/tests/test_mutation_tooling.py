from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

from tests.mutation_cases import CASES, STAGE_A_CASES
from tests.mutation_runner import (
    EPHEMERAL_GIT_BRANCH,
    ROOT,
    _copy_workspace,
    _initialize_workspace_git,
    apply_mutation,
    classify_pytest_exit,
    pytest_selection_expression,
    run_case,
)


def test_curated_mutations_have_one_source_site_and_real_test_nodes():
    for case in CASES:
        source = ROOT / case.target
        assert source.read_text(encoding="utf-8").count(case.before) == 1
        for node_id in case.tests:
            path_text, function_name = node_id.split("::", 1)
            path = ROOT / path_text
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
            assert function_name in names


def test_apply_mutation_replaces_exactly_one_site(tmp_path):
    path = tmp_path / "target.py"
    path.write_text("before\n", encoding="utf-8")
    case = type("Case", (), {"mutation_id": "probe", "target": "target.py", "before": "before", "after": "after"})()
    apply_mutation(path, case)
    assert path.read_text(encoding="utf-8") == "after\n"


@pytest.mark.parametrize(
    ("returncode", "expected"),
    [(0, "survived"), (1, "killed"), (2, "infrastructure_error"), (5, "infrastructure_error")],
)
def test_pytest_exit_classification_distinguishes_survivors_from_runner_errors(
    returncode, expected
):
    assert classify_pytest_exit(returncode) == expected


def test_mutation_selection_collects_full_suite_but_targets_registered_functions():
    case = CASES[0]
    expression = pytest_selection_expression(case)
    assert expression == "test_completed_start_request_replays_full_stored_result"
    assert "tests/" not in expression


def test_stage_a_mutation_lane_is_small_and_postgresql_owned():
    assert 1 <= len(STAGE_A_CASES) <= 6
    assert len({case.mutation_id for case in STAGE_A_CASES}) == len(STAGE_A_CASES)
    assert all(case.mutation_id.startswith("stage-a-") for case in STAGE_A_CASES)
    assert all(case.target.startswith("dish_pg/") for case in STAGE_A_CASES)
    assert all(
        node_id.startswith("tests/postgresql/")
        for case in STAGE_A_CASES
        for node_id in case.tests
    )


def test_stage_a_cli_selection_uses_only_stage_a_cases(monkeypatch, tmp_path):
    from tests import mutation_runner

    captured = {}

    def fake_run(cases, *, artifacts):
        captured["cases"] = cases
        captured["artifacts"] = artifacts
        return 0

    monkeypatch.setattr(mutation_runner, "run", fake_run)
    assert mutation_runner.main(["--stage-a", "--artifacts", str(tmp_path)]) == 0
    assert captured["cases"] is STAGE_A_CASES
    assert captured["artifacts"] == tmp_path


def test_copied_mutation_workspace_has_committed_non_main_git_identity(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _copy_workspace(workspace)

    head = _initialize_workspace_git(workspace)

    branch = subprocess.run(
        ["git", "-C", str(workspace), "branch", "--show-current"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "-C", str(workspace), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    assert branch == EPHEMERAL_GIT_BRANCH
    assert branch != "main"
    assert re.fullmatch(r"[0-9a-f]{40}", head)
    assert status == ""


def test_run_case_executes_through_real_guarded_temporary_workspace():
    result = run_case(STAGE_A_CASES[0])

    assert result["outcome"] == "killed"
    assert "REFUSED:" not in result["output"]


def test_run_case_without_git_identity_is_refused_by_execution_guard(monkeypatch):
    from tests import mutation_runner

    monkeypatch.setattr(
        mutation_runner, "_initialize_workspace_git", lambda _workspace: None
    )
    result = mutation_runner.run_case(STAGE_A_CASES[0])

    assert result["outcome"] == "infrastructure_error"
    assert (
        "cannot verify repository/worktree identity; execution refused" in result["output"]
    )
