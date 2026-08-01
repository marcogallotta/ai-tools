"""Historical submission storage must not remain an executable workflow engine."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PRODUCTION_SYMBOLS = {
    "create_submission",
    "transition_submission",
    "begin_write_attempt",
    "finish_write_attempt",
    "validate_recovery_window",
    "recover_write_attempt",
    "latest_change_diff_telemetry",
    "latest_successful_rejection_reason",
    "assert_expected_identity",
    "assert_live_matches_confirmed",
    "authorize_governed_change",
    "manifest_for_submission",
    "VerificationCycleRecord",
    "WriteAttempt",
}


def test_legacy_mutation_symbols_are_absent_from_production():
    found: dict[str, list[str]] = {}
    for package in ("dish_tool", "dish_service"):
        for path in (ROOT / package).rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            names = {
                node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name in FORBIDDEN_PRODUCTION_SYMBOLS
            }
            if names:
                found[str(path.relative_to(ROOT))] = sorted(names)
    assert found == {}


def test_legacy_submission_storage_is_documented_as_read_only():
    architecture = (ROOT / "docs" / "architecture.md").read_text(encoding="utf-8")
    normalized = " ".join(architecture.split())
    assert "production exposes no API that creates, transitions, or recovers a legacy submission" in normalized
