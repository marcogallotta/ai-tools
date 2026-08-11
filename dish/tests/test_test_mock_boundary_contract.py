from __future__ import annotations

import ast
from pathlib import Path


REVIEWED_PRIVATE_FAULT_SEAMS = {
    ("test_small_correction_lineage.py", "_validate_semantic_evidence"): (
        "Semantic-evidence verifier failure boundary."
    ),
    ("test_restore_restart_and_rollback_durability.py", "_snapshot_to"): (
        "Filesystem snapshot interruption boundary."
    ),
    ("test_restore_restart_and_rollback_durability.py", "_fsync_directory"): (
        "Durability flush failure boundary."
    ),
    ("test_submission_concurrency_atomicity.py", "_finalize_successful_lease"): (
        "Lease-finalization race boundary."
    ),
    ("test_small_correction_recovery_and_diagnostics.py", "_validate_semantic_evidence"): (
        "Semantic-evidence recovery failure boundary."
    ),
    ("test_flake_tooling.py", "_run_command"): "External command execution boundary.",
    ("test_flake_tooling.py", "_write_summary"): "Atomic summary persistence boundary.",
    ("test_planning_intent_confirmation.py", "_build_agent_application"): (
        "Application construction boundary for intent confirmation."
    ),
    ("test_service_semantic_error_classification.py", "_assert_mutation_ready"): (
        "Mutation-readiness rejection boundary; no-op replacements remain forbidden below."
    ),
    ("test_transport.py", "__init__"): "Transport client construction boundary.",
    ("test_committed_success_boundaries.py", "_write_emergency_repair"): (
        "Emergency-repair persistence failure boundary."
    ),
    ("test_material_change_grammar.py", "_signed_identity"): (
        "Signed-identity generation boundary."
    ),
    ("test_dish_admin_expire_lease_authority.py", "_linux_process_start"): (
        "Operating-system process identity boundary."
    ),
    ("postgresql/test_process_failure_execution_boundaries.py", "_start_child"): (
        "Exact subprocess-spawn boundary used to prove DSNs remain environment-only."
    ),
    ("postgresql/test_production_shaped_rehearsal.py", "_source_identity"): (
        "Received-source identity boundary for isolated orchestration tests."
    ),
    (
        "postgresql/test_production_shaped_rehearsal.py",
        "_deployment_configuration_identity",
    ): "Deployment-manifest identity boundary.",
    ("postgresql/test_production_shaped_rehearsal.py", "_checkout_identity"): (
        "Honest-checkout identity boundary before native availability is assessed."
    ),
    ("postgresql/test_production_shaped_runtime_contracts.py", "_wait_health"): (
        "Real service-health transport boundary."
    ),
    ("postgresql/test_production_shaped_runtime_contracts.py", "_http_json"): (
        "Real HTTP request/response transport boundary."
    ),
    ("postgresql/test_recovery_review_corrections.py", "_verified_source_identity"): (
        "Verified received-source identity boundary for cleanup reporting."
    ),
    ("postgresql/test_recovery_review_corrections.py", "_run"): (
        "Bounded recovery orchestration failure boundary."
    ),
    ("postgresql/test_runtime_wiring_review_corrections.py", "_find_compose_command"): (
        "External Docker Compose discovery boundary."
    ),
    ("postgresql/test_runtime_wiring_review_corrections.py", "_probe_native"): (
        "Native PostgreSQL connectivity probe boundary."
    ),
    ("postgresql/test_runtime_wiring_rehearsal.py", "_run_postgresql_test_runtime"): (
        "PostgreSQL service-runtime dispatch boundary for wiring selection tests."
    ),
    ("postgresql/test_runtime_wiring_rehearsal.py", "_run_configured_service"): (
        "Legacy configured-service dispatch boundary for wiring selection tests."
    ),
    ("test_admin_round1b.py", "_command_abandon_operation"): (
        "Post-revocation abandonment failure boundary for durable kill fencing."
    ),
    ("test_admin_round1b.py", "_commit_kill_revocation"): (
        "Durable kill-revocation commit boundary for crash/replay tests."
    ),
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

    stale = set(REVIEWED_PRIVATE_FAULT_SEAMS) - observed
    assert all(reason.strip() for reason in REVIEWED_PRIVATE_FAULT_SEAMS.values())
    assert not violations, "unreviewed private test seams:\n" + "\n".join(violations)
    assert not stale, f"remove stale private-seam allowlist entries: {sorted(stale)}"
