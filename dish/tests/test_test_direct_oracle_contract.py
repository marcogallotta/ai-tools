from __future__ import annotations

import ast
from pathlib import Path


def _has_direct_oracle(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            return True
        if isinstance(child, ast.Call):
            function = child.func
            if isinstance(function, ast.Attribute) and function.attr in {"raises", "fail"}:
                return True
            if isinstance(function, ast.Name) and function.id in {"raises", "fail"}:
                return True
    return False


def test_every_test_has_a_direct_assertion_or_expected_exception():
    missing: list[str] = []
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("test_") and not _has_direct_oracle(node):
                missing.append(f"{path.name}::{node.name}")
    assert missing == [], (
        "every test must state a direct assertion or expected-exception boundary; "
        f"helper-only and implicit must-not-raise tests need an explicit oracle: {missing}"
    )
