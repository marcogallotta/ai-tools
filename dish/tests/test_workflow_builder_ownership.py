from __future__ import annotations

import ast
from pathlib import Path

from tests.support.ast_contracts import call_name

from tests.support.workflow_builder_registry import WORKFLOW_BUILDER_CONTRACTS


TESTS_ROOT = Path(__file__).parent
DIRECT_STATE_CALLS = {
    "confirm_task_content",
    "create_operation",
    "create_verification_cycle",
    "create_abandonment_attempt_in_transaction",
    "declare_operation_step",
    "complete_operation_step",
}
WRITE_SQL = ("INSERT INTO", "UPDATE OPERATIONS", "UPDATE VERIFICATION_CYCLES")



def _shared_builder_candidates() -> set[str]:
    candidates: set[str] = set()
    paths = [
        *sorted((TESTS_ROOT / "support").glob("*.py")),
        *sorted(TESTS_ROOT.glob("_*.py")),
    ]
    for path in paths:
        if path.name in {"workflow_builder_registry.py", "__init__.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for function in tree.body:
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            direct_call = any(
                isinstance(node, ast.Call) and call_name(node) in DIRECT_STATE_CALLS
                for node in ast.walk(function)
            )
            write_sql = any(
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and any(fragment in node.value.upper() for fragment in WRITE_SQL)
                for node in ast.walk(function)
            )
            if direct_call or write_sql:
                candidates.add(
                    f"{path.relative_to(TESTS_ROOT)}::{function.name}"
                )
    return candidates


def _test_functions() -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    functions = {}
    for path in sorted(TESTS_ROOT.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                functions[f"{path.name}::{node.name}"] = node
    return functions


def _has_marker(function: ast.FunctionDef | ast.AsyncFunctionDef, marker: str) -> bool:
    for decorator in function.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if not isinstance(target, ast.Attribute) or target.attr != marker:
            continue
        if (
            isinstance(target.value, ast.Attribute)
            and isinstance(target.value.value, ast.Name)
            and target.value.value.id == "pytest"
            and target.value.attr == "mark"
        ):
            return True
    return False


def test_every_shared_direct_state_builder_has_explicit_ownership():
    assert _shared_builder_candidates() == set(WORKFLOW_BUILDER_CONTRACTS)


def test_workflow_state_builders_have_marked_producer_equivalence_owners():
    functions = _test_functions()
    violations = []
    for builder, contract in WORKFLOW_BUILDER_CONTRACTS.items():
        assert contract.classification in {"workflow_state", "persistence_shape"}
        assert contract.rationale.strip()
        if contract.classification == "persistence_shape":
            assert contract.producer_equivalence_tests == ()
            continue
        if not contract.producer_equivalence_tests:
            violations.append(f"{builder}: missing producer-equivalence owner")
            continue
        for owner in contract.producer_equivalence_tests:
            function = functions.get(owner)
            if function is None:
                violations.append(f"{builder}: missing test {owner}")
            elif not _has_marker(function, "producer_equivalence"):
                violations.append(f"{builder}: {owner} lacks @pytest.mark.producer_equivalence")
    assert violations == []
