from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.mutation_cases import CASES
from tests.mutation_runner import ROOT, apply_mutation, classify_pytest_exit


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
