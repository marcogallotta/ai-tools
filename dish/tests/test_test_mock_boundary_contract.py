from __future__ import annotations

import ast
from pathlib import Path


REVIEWED_PRIVATE_FAULT_SEAMS = {
    ("postgresql/test_scheduled_backup.py", "_regular_file"): (
        "Device-boundary stat forgery for local/off-device same-filesystem detection."
    ),
    ("postgresql/test_scheduled_backup.py", "_directory"): (
        "Device-boundary stat forgery for off-device root detection."
    ),
    ("postgresql/test_scheduled_backup.py", "_prepare_local_root"): (
        "Local backup root preparation boundary."
    ),
    ("postgresql/test_scheduled_backup.py", "_prepare_off_device_root"): (
        "Off-device backup root preparation boundary."
    ),
    ("postgresql/test_scheduled_backup.py", "_prepare_roots"): (
        "Combined local/off-device root preparation boundary for health checks."
    ),
    ("postgresql/test_scheduled_backup.py", "_query_source_identity"): (
        "Source database identity query boundary."
    ),
    ("postgresql/test_scheduled_backup.py", "_git_head"): (
        "Source commit identity boundary."
    ),
    ("postgresql/test_scheduled_backup.py", "_tool_version"): (
        "pg_dump/pg_restore tool version identity boundary."
    ),
    ("postgresql/test_scheduled_backup.py", "_run"): (
        "External pg_dump/pg_restore subprocess execution boundary."
    ),
    ("postgresql/test_scheduled_backup.py", "_copy_off_device"): (
        "Off-device copy failure boundary; verifies retention never runs before a successful copy."
    ),
    ("postgresql/test_scheduled_backup.py", "_prune_retention"): (
        "Retention pruning boundary; verifies pruning never runs before a successful copy."
    ),
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
    ("postgresql/test_backup_restore_rehearsal_tooling.py", "_git_head"): (
        "Repository-head identity boundary for backup and restore rehearsal evidence."
    ),
    ("postgresql/test_backup_restore_rehearsal_tooling.py", "_tool_version"): (
        "PostgreSQL tool-version identity boundary for rehearsal evidence."
    ),
    ("postgresql/test_backup_restore_rehearsal_tooling.py", "_query_identity"): (
        "Database-query identity boundary for rehearsal evidence."
    ),
    ("postgresql/test_backup_restore_rehearsal_tooling.py", "_fingerprint"): (
        "Backup fingerprint generation boundary."
    ),
    ("postgresql/test_backup_restore_rehearsal_tooling.py", "_compare"): (
        "Backup comparison boundary."
    ),
    ("postgresql/test_backup_restore_rehearsal_tooling.py", "_run"): (
        "External rehearsal command execution boundary."
    ),
    ("postgresql/test_backup_restore_rehearsal_tooling.py", "_copy_off_device"): (
        "Off-device backup copy boundary."
    ),
    ("postgresql/test_cutover_activation_rehearsal.py", "_find_compose_command"): (
        "External Docker Compose discovery boundary."
    ),
    ("postgresql/test_dark_launch_shadow_worker.py", "_translate_workflow_identifiers"): (
        "Shadow workflow-identity translation boundary."
    ),
    ("postgresql/test_dark_launch_shadow_worker.py", "_ensure_shadow_run"): (
        "Shadow-run creation boundary."
    ),
    ("postgresql/test_dark_launch_shadow_worker.py", "_target_authority_state"): (
        "Shadow target authority-state capture boundary."
    ),
    ("postgresql/test_dark_launch_shadow_worker.py", "_target_response_payload"): (
        "Shadow target response-payload capture boundary."
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
    ("postgresql/test_postgres_runtime_validation_http.py", "__init__"): (
        "Asana backend construction boundary for the canonical PostgreSQL Action no-Asana proof."
    ),
    ("postgresql/test_stage4_command_port.py", "__init__"): (
        "Asana backend construction boundary for PostgreSQL-native command no-Asana proofs "
        "(safe-reclaim and proposal/apply-proposal)."
    ),
    ("postgresql/test_runtime_wiring_rehearsal.py", "_run_postgresql_test_runtime"): (
        "PostgreSQL service-runtime dispatch boundary for wiring selection tests."
    ),
    ("postgresql/test_runtime_wiring_rehearsal.py", "_run_configured_service"): (
        "Legacy configured-service dispatch boundary for wiring selection tests."
    ),
    ("postgresql/test_production_reset.py", "_database_exists"): (
        "Database-existence boundary for production-reset recreate and resume fencing tests."
    ),
    ("postgresql/test_production_reset.py", "_load_database_definition"): (
        "Catalog-definition capture boundary for production-reset recreate ordering tests."
    ),
    ("postgresql/test_production_reset.py", "_maintenance_actor_identity"): (
        "Maintenance-role identity boundary for production-reset authorization tests."
    ),
    ("postgresql/test_production_reset.py", "_load_reset_guard"): (
        "Production-reset guard identity boundary for reset lineage and resume mismatch tests."
    ),
    ("postgresql/test_production_reset.py", "_check_non_session_drop_blockers"): (
        "Non-session drop-blocker boundary for deterministic production-reset recreate ordering tests."
    ),
    ("postgresql/test_production_reset.py", "_active_sessions"): (
        "Active-session discovery boundary for deterministic production-reset recreate ordering tests."
    ),
    ("postgresql/test_test_generation_rollover.py", "_prepare_candidate"): (
        "First-admission candidate fixture boundary used to construct a same-shape non-incident "
        "candidate for rollover signature refusal."
    ),
    ("postgresql/native/test_routine_migration.py", "_repository_script"): (
        "Alembic repository-graph fault-injection boundary used to prove a known "
        "non-ancestor database revision fails before mutation."
    ),
    ("test_admin_round1b.py", "_command_abandon_operation"): (
        "Post-revocation abandonment failure boundary for durable kill fencing."
    ),
    ("test_admin_round1b.py", "_commit_kill_revocation"): (
        "Durable kill-revocation commit boundary for crash/replay tests."
    ),
    ("test_admin_human_rendering.py", "_utc_now"): (
        "Administrative rendering clock boundary."
    ),
    ("test_admin_human_rendering.py", "_localize"): (
        "Administrative rendering timezone-localization boundary."
    ),
    ("test_shadow_capture.py", "_execute_locked"): (
        "Shadow-spool locked execution boundary."
    ),
    ("postgresql/test_postgres_runtime_validation_http.py", "__init__"): (
        "Non-construction guard proving the canonical PostgreSQL Action HTTP path never"
        " constructs AsanaBackend."
    ),
    ("postgresql/test_stage4_command_port.py", "__init__"): (
        "Non-construction guard proving PostgreSQL safe-reclaim and proposal-apply command"
        " paths never construct AsanaBackend."
    ),
    ("postgresql/test_test_dual_stack_comparator.py", "_request_json"): (
    "HTTP transport boundary for deterministic comparator health/OpenAPI preflight tests."
),
    ("postgresql/test_test_dual_stack_comparator.py", "_command_request"): (
    "Action command transport boundary for deterministic route-identity and comparator scenario tests."
),
    ("postgresql/test_test_dual_stack_comparator.py", "_route_preflight"): (
    "Comparator preflight stub used only by scenario/evidence tests; isolation preflight has dedicated contract coverage."
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
