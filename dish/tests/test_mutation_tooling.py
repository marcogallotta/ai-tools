from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.mutation_cases import CASES, STAGE_A_CASES
from tests.mutation_runner import (
    ROOT,
    apply_mutation,
    classify_pytest_exit,
    pytest_selection_expression,
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
