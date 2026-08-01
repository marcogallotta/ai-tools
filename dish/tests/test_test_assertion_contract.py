from __future__ import annotations

import ast
from pathlib import Path


def _is_pytest_raises(node: ast.AST, exception: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
        and node.func.attr == "raises"
        and bool(node.args)
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == exception
    )


def _is_rule_lookup(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    key = node.slice
    return isinstance(key, ast.Constant) and key.value == "rule"


def _attribute_path(node: ast.AST) -> str | None:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def _captured_rule_is_compared_exactly(
    function: ast.FunctionDef | ast.AsyncFunctionDef, capture: str
) -> bool:
    aliases = {capture}
    changed = True
    while changed:
        changed = False
        for node in ast.walk(function):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if _attribute_path(value) not in {f"{name}.value" for name in aliases}:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    changed = True

    exact_paths = {f"{capture}.value.rule"} | {
        f"{alias}.rule" for alias in aliases if alias != capture
    }
    for node in ast.walk(function):
        if not isinstance(node, ast.Compare) or not any(
            isinstance(operator, ast.Eq) for operator in node.ops
        ):
            continue
        values = [node.left, *node.comparators]
        if any(_attribute_path(value) in exact_paths for value in values):
            return True
        for value in values:
            if not (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "getattr"
                and len(value.args) >= 2
                and _attribute_path(value.args[0]) in (
                    aliases | {f"{alias}.value" for alias in aliases}
                )
                and isinstance(value.args[1], ast.Constant)
                and value.args[1].value == "rule"
            ):
                continue
            return True
    return False


def test_tests_use_exact_exception_and_dish_rule_oracles():
    root = Path(__file__).parent
    violations: list[str] = []

    for path in sorted(root.rglob("test_*.py")):
        if path == Path(__file__):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and (
                _is_pytest_raises(node, "Exception")
                or _is_pytest_raises(node, "BaseException")
            ):
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

        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(function):
                if not isinstance(node, ast.With):
                    continue
                for item in node.items:
                    if not _is_pytest_raises(item.context_expr, "DishRuleError"):
                        continue
                    if not isinstance(item.optional_vars, ast.Name):
                        violations.append(
                            f"{path.relative_to(root)}:{node.lineno}: DishRuleError must be captured and its exact rule asserted"
                        )
                        continue
                    if not _captured_rule_is_compared_exactly(
                        function, item.optional_vars.id
                    ):
                        violations.append(
                            f"{path.relative_to(root)}:{node.lineno}: captured DishRuleError must assert one exact .rule"
                        )

    assert not violations, "weak test oracles found:\n" + "\n".join(violations)
