from __future__ import annotations

import ast
from pathlib import Path


REVIEWED_PRIVATE_FAULT_SEAMS = {
    ("test_small_correction_lineage.py", "_validate_semantic_evidence"),
    ("test_restore_restart_and_rollback_durability.py", "_snapshot_to"),
    ("test_restore_restart_and_rollback_durability.py", "_fsync_directory"),
    ("test_submission_concurrency_atomicity.py", "_finalize_successful_lease"),
    ("test_small_correction_recovery_and_diagnostics.py", "_validate_semantic_evidence"),
    ("test_flake_tooling.py", "_run_command"),
    ("test_flake_tooling.py", "_write_summary"),
    ("test_planning_intent_confirmation.py", "_build_agent_application"),
    ("test_service_semantic_error_classification.py", "_assert_mutation_ready"),
    ("test_transport.py", "__init__"),
    ("test_committed_success_boundaries.py", "_write_emergency_repair"),
    ("test_material_change_grammar.py", "_signed_identity"),
    ("test_dish_admin_expire_lease_authority.py", "_linux_process_start"),
}


def _test_python_files():
    root = Path(__file__).parent
    yield from sorted(root.rglob("*.py"))


def _private_target(node: ast.Call) -> str | None:
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        value = node.args[1].value
        if isinstance(value, str) and value.startswith("_"):
            return value
    if node.args and isinstance(node.args[0], ast.Constant):
        value = node.args[0].value
        if isinstance(value, str):
            target = value.rsplit(".", 1)[-1]
            if target.startswith("_"):
                return target
    return None


def test_private_fault_injection_seams_are_explicitly_reviewed():
    root = Path(__file__).parent
    observed: set[tuple[str, str]] = set()
    violations: list[str] = []

    for path in _test_python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if not (
                isinstance(function, ast.Attribute)
                and function.attr == "setattr"
                and isinstance(function.value, ast.Name)
                and function.value.id in {"monkeypatch", "killed", "dead_process"}
            ):
                continue
            target = _private_target(node)
            if target is None:
                continue
            key = (str(path.relative_to(root)), target)
            observed.add(key)
            if key not in REVIEWED_PRIVATE_FAULT_SEAMS:
                violations.append(
                    f"{key[0]}:{node.lineno}: private seam {target!r} is not reviewed"
                )
            if target == "_release":
                violations.append(
                    f"{key[0]}:{node.lineno}: workflow release authority must not be bypassed"
                )
            if target == "_assert_mutation_ready" and len(node.args) >= 3:
                replacement = node.args[2]
                if (
                    isinstance(replacement, ast.Lambda)
                    and isinstance(replacement.body, ast.Constant)
                    and replacement.body.value is None
                ):
                    violations.append(
                        f"{key[0]}:{node.lineno}: mutation authority must not be replaced by a no-op"
                    )

    stale = REVIEWED_PRIVATE_FAULT_SEAMS - observed
    assert not violations, "unreviewed private test seams:\n" + "\n".join(violations)
    assert not stale, f"remove stale private-seam allowlist entries: {sorted(stale)}"
