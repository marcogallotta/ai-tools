"""Keep statically emitted semantic invariants tied to explicit diagnostics."""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "dish_tool" / "database_schema.py"
DOCUMENTED_DYNAMIC_PREFIXES = ("multiple_unresolved_",)


def _literal_invariants(tree: ast.AST) -> set[str]:
    invariants: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (
            isinstance(function, ast.Name) and function.id == "_semantic_problem"
        ):
            continue
        if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
            value = node.args[1].value
            if isinstance(value, str):
                invariants.add(value)
    return invariants


def _relationship_keys(tree: ast.AST) -> set[str]:
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign):
            continue
        if not isinstance(node.target, ast.Name) or node.target.id != "relationships":
            continue
        assert isinstance(node.value, ast.Dict)
        return {
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
    raise AssertionError("semantic relationship mapping was not found")


def test_every_static_semantic_invariant_has_an_explicit_diagnostic() -> None:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    missing = _literal_invariants(tree) - _relationship_keys(tree)
    assert not missing, f"missing semantic diagnostic specifications: {sorted(missing)}"


def test_dynamic_semantic_invariants_are_explicitly_bounded() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    assert 'f"multiple_unresolved_{table}"' in source
    assert all(
        f'invariant.startswith("{prefix}")' in source
        for prefix in DOCUMENTED_DYNAMIC_PREFIXES
    )
