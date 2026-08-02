"""Structural ownership contracts for PostgreSQL runtime lanes."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.pglite
ROOT = Path(__file__).resolve().parents[3]


def _module_markers(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    markers: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in {"pglite", "native_postgresql"}:
            continue
        value = node.value
        if isinstance(value, ast.Attribute) and value.attr == "mark":
            markers.add(node.attr)
    return markers


def test_runtime_test_modules_are_explicitly_classified() -> None:
    runtime_roots = {
        ROOT / "tests" / "postgresql" / "pglite": "pglite",
        ROOT / "tests" / "postgresql" / "native": "native_postgresql",
    }
    for directory, expected in runtime_roots.items():
        if not directory.exists():
            continue
        for path in directory.glob("test_*.py"):
            assert expected in _module_markers(path), f"{path.relative_to(ROOT)} lacks {expected}"


def test_pglite_is_not_part_of_certification_acceptance() -> None:
    acceptance = (ROOT / "scripts" / "dish-pg-acceptance").read_text()
    assert "--pglite" not in acceptance
    assert "dish-pg-pglite" not in acceptance


def test_pglite_report_is_explicitly_non_certifying() -> None:
    source = (ROOT / "scripts" / "dish-pg-pglite").read_text()
    assert '"certification_evidence": False' in source
    assert '"native_postgresql_certified": False' in source
