"""Structural ownership contract for workflow action authority."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRODUCTION_ROOTS = (ROOT / "dish_tool", ROOT / "dish_service")


def _production_files():
    for root in PRODUCTION_ROOTS:
        yield from root.rglob("*.py")


def test_result_formatting_does_not_derive_workflow_legality():
    results = (ROOT / "dish_tool" / "results.py").read_text(encoding="utf-8")
    tree = ast.parse(results, filename="dish_tool/results.py")

    names = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "allowed_actions_for_state" not in names
    assert "ALLOWED_ACTIONS_BY_STATE" not in results

    result_function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "result_envelope"
    )
    calls = {
        node.func.id
        for node in ast.walk(result_function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "legal_actions" not in calls
    assert "phase_candidate_actions" not in calls


def test_no_secondary_state_to_action_table_exists():
    offenders = []
    for path in _production_files():
        text = path.read_text(encoding="utf-8")
        if "ALLOWED_ACTIONS_BY_STATE" in text:
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []


def test_persistence_helper_is_named_as_candidate_not_authority():
    source = (ROOT / "dish_tool" / "database.py").read_text(encoding="utf-8")
    assert "def phase_candidate_actions(" in source
    assert "def legal_operation_actions(" not in source


def test_inspect_operation_does_not_expose_phase_candidates_as_legal_actions():
    source = (ROOT / "dish_tool" / "step5.py").read_text(encoding="utf-8")
    tree = ast.parse(source, filename="dish_tool/step5.py")
    inspect_function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "inspect_operation"
    )

    calls = {
        node.func.id
        for node in ast.walk(inspect_function)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "phase_candidate_actions" not in calls

    legal_fields = {
        key.value
        for node in ast.walk(inspect_function)
        if isinstance(node, ast.Dict)
        for key in node.keys
        if isinstance(key, ast.Constant)
        and isinstance(key.value, str)
        and key.value.startswith("legal_")
    }
    assert legal_fields == set()


def test_production_uses_authoritative_transaction_and_workflow_primitives():
    root = Path(__file__).resolve().parents[1]
    production = list((root / "dish_tool").glob("*.py")) + list((root / "dish_service").glob("*.py"))
    text = "\n".join(path.read_text(encoding="utf-8") for path in production)
    assert "atomic_persistence" not in text
    assert "immediate_persistence" not in text
    assert "WorkflowRepository" not in text
    assert not (root / "dish_tool" / "workflow_repository.py").exists()
