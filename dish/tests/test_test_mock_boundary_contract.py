from __future__ import annotations

import ast
from pathlib import Path


def _test_python_files():
    root = Path(__file__).parent
    yield from sorted(root.rglob("*.py"))


def test_workflow_contract_tests_do_not_bypass_service_authority_with_noop_mocks():
    violations = []

    for path in _test_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or len(node.args) < 3:
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and function.attr == "setattr"
                and isinstance(function.value, ast.Name)
                and function.value.id == "monkeypatch"
            ):
                continue
            target = node.args[1]
            if not isinstance(target, ast.Constant) or not isinstance(target.value, str):
                continue
            if target.value == "_release":
                violations.append((path.relative_to(Path(__file__).parent), node.lineno, target.value))
            if target.value == "_assert_mutation_ready":
                replacement = node.args[2]
                if (
                    isinstance(replacement, ast.Lambda)
                    and isinstance(replacement.body, ast.Constant)
                    and replacement.body.value is None
                ):
                    violations.append((path.relative_to(Path(__file__).parent), node.lineno, target.value))

    assert violations == []
