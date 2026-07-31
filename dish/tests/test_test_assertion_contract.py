from __future__ import annotations

import ast
from pathlib import Path


def _is_pytest_raises_exception(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
        and node.func.attr == "raises"
        and bool(node.args)
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in {"Exception", "BaseException"}
    )


def _is_rule_lookup(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    key = node.slice
    return isinstance(key, ast.Constant) and key.value == "rule"


def test_tests_use_exact_exception_and_dish_rule_oracles():
    root = Path(__file__).parent
    violations: list[str] = []

    for path in sorted(root.rglob("test_*.py")):
        if path == Path(__file__):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_pytest_raises_exception(node):
                violations.append(
                    f"{path.relative_to(root)}:{node.lineno}: pytest.raises must name the exact exception type"
                )
            if (
                isinstance(node, ast.Compare)
                and len(node.ops) == 1
                and isinstance(node.ops[0], ast.In)
                and _is_rule_lookup(node.left)
                and isinstance(node.comparators[0], (ast.Set, ast.List, ast.Tuple))
                and len(node.comparators[0].elts) > 1
            ):
                violations.append(
                    f"{path.relative_to(root)}:{node.lineno}: Dish rule assertions must name one exact rule"
                )

    assert not violations, "weak test oracles found:\n" + "\n".join(violations)
