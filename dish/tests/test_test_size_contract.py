from __future__ import annotations

import ast
from pathlib import Path


MAX_TEST_FILE_LINES = 500
MAX_TEST_FUNCTION_LINES = 100


def _test_function_lines(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    decorator_lines = [decorator.lineno for decorator in node.decorator_list]
    start = min([node.lineno, *decorator_lines])
    return node.end_lineno - start + 1


def test_test_modules_stay_below_the_review_size_ceiling():
    oversized = []
    for path in sorted(Path(__file__).parent.rglob("test_*.py")):
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > MAX_TEST_FILE_LINES:
            oversized.append(f"{path.relative_to(Path(__file__).parent)}: {lines} lines")
    assert oversized == [], (
        "split test modules by stable behavior ownership before they exceed "
        f"{MAX_TEST_FILE_LINES} lines: {oversized}"
    )


def test_individual_tests_stay_below_the_review_size_ceiling():
    oversized = []
    for path in sorted(Path(__file__).parent.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            lines = _test_function_lines(node)
            if lines > MAX_TEST_FUNCTION_LINES:
                oversized.append(f"{path.relative_to(Path(__file__).parent)}::{node.name}: {lines} lines")
    assert oversized == [], (
        "extract named setup, fault, execution, and oracle phases before a test "
        f"exceeds {MAX_TEST_FUNCTION_LINES} lines: {oversized}"
    )
