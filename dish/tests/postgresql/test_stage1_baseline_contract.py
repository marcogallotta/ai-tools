from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = ROOT / "docs/database-backend-stage-a-baseline.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _literal_assignments(path: Path) -> dict[str, Any]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, Any] = {}

    def evaluate(node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            return values[node.id]
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            result = [evaluate(item) for item in node.elts]
            if isinstance(node, ast.Tuple):
                return tuple(result)
            if isinstance(node, ast.Set):
                return set(result)
            return result
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return evaluate(node.left) | evaluate(node.right)
        raise ValueError(f"unsupported literal expression: {ast.dump(node)}")

    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            values[target.id] = evaluate(statement.value)
        except (KeyError, ValueError):
            continue
    return values


def _baseline() -> dict[str, Any]:
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def test_frozen_command_inventory_matches_current_surfaces() -> None:
    baseline = _baseline()
    action_values = _literal_assignments(ROOT / "dish_service/command_spec.py")
    admin_values = _literal_assignments(ROOT / "dish_tool/admin_cli.py")

    assert list(action_values["ACTION_COMMANDS"]) == baseline["action_commands"]
    assert sorted(admin_values["_ADMIN_COMMANDS"]) == sorted(baseline["admin_commands"])

    expected_treatments = set(baseline["action_commands"]) | set(baseline["admin_commands"])
    expected_treatments.add("planning-intent-settlement")
    treatments = baseline["target_treatments"]
    assert isinstance(treatments, dict)
    assert set(treatments) == expected_treatments
    assert all(
        re.fullmatch(r"(?:retain|retire|add):[A-Z]", treatment)
        for treatment in treatments.values()
    )


def test_frozen_sqlite_authority_inventory_matches_schema() -> None:
    baseline = _baseline()
    source = (ROOT / "dish_tool/database_schema.py").read_text(encoding="utf-8")
    actual: list[str] = []
    for name in re.findall(
        r"CREATE TABLE(?: IF NOT EXISTS)?\s+([A-Za-z0-9_]+)", source
    ):
        if name not in actual:
            actual.append(name)
    for old_name, new_name in re.findall(
        r"ALTER TABLE\s+([A-Za-z0-9_]+)\s+RENAME TO\s+([A-Za-z0-9_]+)", source
    ):
        if old_name in actual:
            actual[actual.index(old_name)] = new_name
    assert actual == baseline["sqlite_tables"]


def test_frozen_governing_sources_have_exact_hashes() -> None:
    baseline = _baseline()
    actual = {
        relative: _sha256(ROOT / relative)
        for relative in baseline["governing_source_sha256"]
    }
    assert actual == baseline["governing_source_sha256"]


def test_frozen_characterization_corpus_has_exact_hashes() -> None:
    baseline = _baseline()
    actual = {
        str(path.relative_to(ROOT)): _sha256(path)
        for path in sorted((ROOT / "tests").glob("test_*.py"))
    }
    assert actual == baseline["characterization_test_sha256"]
