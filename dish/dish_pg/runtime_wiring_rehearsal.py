"""Rerunnable native PostgreSQL runtime-wiring rehearsal for validation-plan §3.

The runner owns one disposable TEST-only PostgreSQL Compose instance, invokes a
literal native pytest node, and emits one bounded report.  It never substitutes
SQLite/PGlite and never propagates Asana environment values to child processes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from .process_failure_rehearsal import (
    COMPOSE_PROBE_TIMEOUT_SECONDS,
    DEFAULT_CLEANUP_TIMEOUT_SECONDS,
    DEFAULT_COMPOSE_TIMEOUT_SECONDS,
    DEFAULT_PYTEST_TIMEOUT_SECONDS,
    DEFAULT_TERMINATION_GRACE_SECONDS,
    POSTGRES_IMAGE,
    RehearsalConfigurationError,
    _canonical_bytes,
    _command_failure,
    _command_succeeded,
    _compose_payload,
    _find_compose_command,
    _parse_junit,
    _probe_native,
    _read_json_files,
    _safe_database_name,
    _safe_port,
    _safe_timeout,
    _terminate_incomplete_process_groups,
    run_external_command,
    write_json_atomic,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 55443
DEFAULT_DATABASE = "dish_section3_runtime_wiring_test"
TEST_NODE = (
    "tests/postgresql/native/test_runtime_wiring_rehearsal.py::"
    "test_runtime_wiring_rehearsal_across_service_and_worker_processes"
)
REQUIRED_PROCESS_LABELS = {
    "postgresql-service",
    "postgresql-tcp-proxy-before-loss",
    "postgresql-tcp-proxy-after-restart",
    "projection-section3-restart-before-death",
    "projection-section3-restart-after-death",
    "projection-section3-restart-noop",
    "projection-section3-takeover-original",
    "projection-section3-takeover-replacement",
    "projection-section3-takeover-noop",
    "projection-section3-downstream-failure",
    "reconciliation",
}
REQUIRED_IDENTITY_ROLES = {
    "projection_worker",
    "reconciliation_worker",
}
REQUIRED_SCENARIOS = (
    "runtime_identity_and_isolation",
    "command_service_boundary",
    "same_logical_worker_restart_after_process_death",
    "different_worker_takeover_after_claim_expiry",
    "stale_original_worker_rejection_after_takeover",
    "external_attempt_settlement_lifecycle",
    "reconciliation_process_boundary",
    "postgresql_loss_fail_closed",
    "downstream_failure_freshness",
    "unsupported_test_service_routes_fail_closed",
)


def _safe_project(value: str) -> str:
    project = value.strip().lower()
    if not re.fullmatch(r"dish-section3-[a-z0-9-]+", project):
        raise RehearsalConfigurationError(
            "compose project must match dish-section3-<lowercase-safe-suffix>"
        )
    return project


def _git_identity() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip()
    return value or None


def _finalize(report: dict[str, Any]) -> None:
    report.pop("report_sha256", None)
    report["report_sha256"] = hashlib.sha256(_canonical_bytes(report)).hexdigest()


def _base_report(args: argparse.Namespace, *, run_evidence: Path) -> dict[str, Any]:
    return {
        "format": "dish-postgresql-runtime-wiring-rehearsal-v2",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "scope": "database-backend-postgresql-test-plan-section-3",
        "status": "initializing",
        "ok": False,
        "received_source": {
            "archive_name": args.received_archive_name,
            "archive_sha256": args.received_archive_sha256,
            "base_identity": args.base_identity,
        },
        "repository_commit": _git_identity(),
        "test_node": TEST_NODE,
        "database_name": args.database_name,
        "host_port": args.port,
        "compose_project": args.compose_project,
        "postgres_image": POSTGRES_IMAGE,
        "run_evidence_dir": str(run_evidence),
        "native_postgresql_required": True,
        "separate_os_processes_required": True,
        "test_only": True,
        "production_profile_reachable": False,
        "production_credentials_reachable": False,
        "production_routes_reachable": False,
        "asana_resources_reachable": False,
        "network_probe_of_production_performed": False,
        "timeouts": {
            "compose_seconds": args.compose_timeout_seconds,
            "pytest_seconds": args.pytest_timeout_seconds,
            "cleanup_seconds": args.cleanup_timeout_seconds,
            "termination_grace_seconds": args.termination_grace_seconds,
            "compose_probe_seconds": COMPOSE_PROBE_TIMEOUT_SECONDS,
        },
        "commands": [],
        "first_attempt": {"status": "initializing", "pytest_exit_status": None},
        "exact_runtime_identity": None,
        "processes": [],
        "runtime_identity_reports": [],
        "scenario_evidence": None,
        "required_scenarios": {
            name: {"status": "initializing", "evidence": None, "blocker": None}
            for name in REQUIRED_SCENARIOS
        },
        "failure_paths": {},
        "evidence_validation": {"ok": False, "errors": ["not validated"]},
        "cleanup_errors": [],
        "manual_cleanup_required": False,
        "manual_cleanup": None,
    }


def _blocked_scenarios(reason: str) -> dict[str, dict[str, Any]]:
    return {
        name: {"status": "blocked", "evidence": None, "blocker": reason}
        for name in REQUIRED_SCENARIOS
    }


def _reported_scenarios(
    evidence: dict[str, Any] | None, *, status: str
) -> dict[str, dict[str, Any]]:
    mapping = {
        "runtime_identity_and_isolation": (
            None
            if evidence is None
            else {
                "service_health": evidence.get("service_health"),
                "runtime_identities": evidence.get("runtime_identities"),
            }
        ),
        "command_service_boundary": None if evidence is None else evidence.get("command_boundary"),
        "same_logical_worker_restart_after_process_death": (
            None if evidence is None else evidence.get("same_logical_worker_restart")
        ),
        "different_worker_takeover_after_claim_expiry": (
            None if evidence is None else evidence.get("different_worker_takeover")
        ),
        "stale_original_worker_rejection_after_takeover": (
            None if evidence is None else evidence.get("stale_original_worker_rejection")
        ),
        "external_attempt_settlement_lifecycle": (
            None if evidence is None else evidence.get("external_attempt_settlement")
        ),
        "reconciliation_process_boundary": (
            None if evidence is None else evidence.get("reconciliation")
        ),
        "postgresql_loss_fail_closed": None if evidence is None else evidence.get("postgresql_loss"),
        "downstream_failure_freshness": (
            None if evidence is None else evidence.get("downstream_failure_projection")
        ),
        "unsupported_test_service_routes_fail_closed": (
            None if evidence is None else evidence.get("unsupported_test_service_routes")
        ),
    }
    return {
        name: {
            "status": status if value is not None else "failed",
            "evidence": value,
            "blocker": None,
        }
        for name, value in mapping.items()
    }


def _load_single_scenario(directory: Path) -> tuple[dict[str, Any] | None, list[str]]:
    records = _read_json_files(directory)
    errors: list[str] = []
    matching: list[dict[str, Any]] = []
    for record in records:
        if "read_error" in record:
            errors.append(f"unreadable scenario {record['path']}: {record['read_error']}")
            continue
        payload = record.get("payload")
        if isinstance(payload, dict) and payload.get("scenario") == "runtime-wiring-section3":
            matching.append(payload)
    if len(matching) != 1:
        errors.append(f"expected exactly one runtime-wiring-section3 scenario, found {len(matching)}")
        return None, errors
    scenario = matching[0]
    if scenario.get("completion_state") != "scenario_assertions_completed":
        errors.append("section 3 scenario did not complete its assertions")
    return scenario, errors


def _validate_evidence(
    *,
    cases: dict[str, dict[str, Any]],
    junit_errors: list[str],
    scenario: dict[str, Any] | None,
    process_records: list[dict[str, Any]],
    identity_records: list[dict[str, Any]],
) -> dict[str, Any]:
    errors = list(junit_errors)
    case = cases.get(TEST_NODE)
    if case is None:
        errors.append("JUnit is missing the literal §3 native node")
    elif case.get("status") != "passed":
        errors.append(f"§3 native node status is {case.get('status')!r}")

    labels: set[str] = set()
    pids: set[int] = set()
    for record in process_records:
        if "read_error" in record:
            errors.append(f"unreadable process record {record['path']}: {record['read_error']}")
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            errors.append(f"invalid process record {record['path']}")
            continue
        label = payload.get("label")
        pid = payload.get("pid")
        if isinstance(label, str):
            labels.add(label)
        if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
            if pid in pids:
                errors.append(f"duplicate process PID {pid}")
            pids.add(pid)
        else:
            errors.append(f"process record lacks a valid PID: {record['path']}")
        command = payload.get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) and part for part in command
        ):
            errors.append(f"process record lacks an exact command vector: {record['path']}")
        if payload.get("completion_state") not in {"completed", "terminated"}:
            errors.append(f"process did not reach a bounded terminal state: {record['path']}")
    missing_labels = sorted(REQUIRED_PROCESS_LABELS - labels)
    if missing_labels:
        errors.append(f"required process labels missing: {missing_labels}")

    roles: set[str] = set()
    identities: list[dict[str, Any]] = []
    for record in identity_records:
        if "read_error" in record:
            errors.append(f"unreadable identity record {record['path']}: {record['read_error']}")
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            errors.append(f"invalid runtime identity record: {record['path']}")
            continue
        role = payload.get("role")
        identity = payload.get("identity")
        if isinstance(role, str):
            roles.add(role)
        if not isinstance(identity, dict):
            errors.append(f"identity payload missing from {record['path']}")
        else:
            identities.append(identity)
    if not REQUIRED_IDENTITY_ROLES.issubset(roles):
        errors.append(f"runtime identity roles missing: {sorted(REQUIRED_IDENTITY_ROLES - roles)}")
    if identities and any(identity != identities[0] for identity in identities[1:]):
        errors.append("worker runtime identities differ")

    evidence = None if scenario is None else scenario.get("evidence")
    required_scenario_keys = {
        "service_health",
        "command_boundary",
        "same_logical_worker_restart",
        "different_worker_takeover",
        "stale_original_worker_rejection",
        "external_attempt_settlement",
        "downstream_failure_projection",
        "unsupported_test_service_routes",
        "reconciliation",
        "postgresql_loss",
        "runtime_identities",
    }
    if not isinstance(evidence, dict):
        errors.append("scenario evidence object is missing")
    else:
        missing = sorted(required_scenario_keys - set(evidence))
        if missing:
            errors.append(f"scenario evidence keys missing: {missing}")
        service_health = evidence.get("service_health") or {}
        health_identity = service_health.get("identity")
        if not isinstance(health_identity, dict):
            errors.append("service health identity is missing")
        elif identities and health_identity != identities[0]:
            errors.append("service and worker runtime identities differ")
        isolation = service_health.get("isolation") or {}
        if service_health.get("profile") != "test":
            errors.append("service did not prove the TEST profile")
        if isolation.get("asana_environment_keys") != []:
            errors.append("service process had reachable Asana environment")
        if isolation.get("supported_http_surfaces") != ["agent"]:
            errors.append("PostgreSQL TEST service exposed unsupported HTTP surfaces")
        if isolation.get("bind_host") != "127.0.0.1" or isolation.get(
            "action_bind_host"
        ) != "127.0.0.1":
            errors.append("service listeners were not both loopback-bound")
        ports = evidence.get("ports") or {}
        observed_ports = {
            value
            for value in ports.values()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        if len(observed_ports) != 3 or observed_ports & {8765, 8766, 8775, 8776, 8786}:
            errors.append("rehearsal ports were incomplete or overlapped known live routes")
        database = evidence.get("database")
        if (
            not isinstance(database, str)
            or not database.startswith("dish_")
            or not database.endswith("_test")
            or "prod" in database.lower()
        ):
            errors.append("scenario did not prove a disposable TEST-only database")

        command_boundary = evidence.get("command_boundary") or {}
        if (command_boundary.get("result") or {}).get("ok") is not True:
            errors.append("command handling did not cross the real service boundary successfully")

        unsupported = evidence.get("unsupported_test_service_routes") or {}
        route_results = unsupported.get("routes") or []
        if unsupported.get("status") != "passed" or unsupported.get("internal_error_count") != 0:
            errors.append("unsupported TEST service routes did not fail closed")
        if len(route_results) < 4 or any(
            route.get("status") != 404
            or route.get("payload") != {"ok": False, "error": "not_found"}
            for route in route_results
        ):
            errors.append("unsupported TEST service routes were not hidden as not-found")

        restart = evidence.get("same_logical_worker_restart") or {}
        before_restart = restart.get("before_process_death") or {}
        after_restart = restart.get("after_same_logical_worker_restart") or {}
        restart_noop = restart.get("no_duplicate_after_post_settlement_restart") or {}
        before_process = before_restart.get("process") or {}
        after_process = after_restart.get("process") or {}
        before_snapshot = before_restart.get("snapshot") or {}
        after_snapshot = after_restart.get("snapshot") or {}
        before_attempts = before_snapshot.get("attempts") or []
        after_attempts = after_snapshot.get("attempts") or []
        if restart.get("status") != "passed" or not restart.get("worker_id"):
            errors.append("same logical worker restart scenario did not pass")
        if before_process.get("pid") == after_process.get("pid"):
            errors.append("same logical worker restart did not use a new process PID")
        if before_process.get("termination_state") != "sigkill":
            errors.append("same logical worker pre-restart process death was not proven")
        if len(before_attempts) != 1 or before_attempts[0].get("state") != "dispatched":
            errors.append("same-worker restart did not preserve one durable external attempt")
        if (before_restart.get("external_ledger") or {}).get("dispatch_calls") != 1:
            errors.append("same-worker restart did not preserve the first external dispatch")
        expected_restart_attempts = [
            ("dispatch", "uncertain", restart.get("worker_id"), True),
            ("recovery", "confirmed", restart.get("worker_id"), True),
        ]
        observed_restart_attempts = [
            (row.get("kind"), row.get("state"), row.get("worker_id"), row.get("terminal"))
            for row in after_attempts
        ]
        if observed_restart_attempts != expected_restart_attempts:
            errors.append("same logical worker restart lifecycle was incomplete")
        restart_ledger = after_restart.get("external_ledger") or {}
        if restart_ledger.get("dispatch_calls") != 1 or restart_ledger.get(
            "recovery_observations"
        ) != 1:
            errors.append("same-worker restart duplicated dispatch or missed recovery observation")
        restart_settlement = after_restart.get("settlement") or {}
        restart_observation = restart_settlement.get("authoritative_external_observation") or {}
        restart_fact = (restart_observation.get("evidence") or {}).get("external_observation") or {}
        restart_adjudication = restart_settlement.get("settlement_adjudication") or {}
        if (
            restart_observation.get("kind") != "marker_search"
            or restart_observation.get("observed_applied") is not True
            or restart_observation.get("reread_complete") is not True
            or restart_fact.get("source") != "external_marker_search"
            or restart_adjudication.get("outcome") != "confirmed"
        ):
            errors.append("same-worker restart lacks authoritative confirmed settlement evidence")
        if restart_noop.get("snapshot_unchanged") is not True or restart_noop.get(
            "external_ledger_unchanged"
        ) is not True:
            errors.append("post-settlement same-worker restart duplicated dispatch or settlement")

        takeover = evidence.get("different_worker_takeover") or {}
        original_worker = takeover.get("original_worker_id")
        replacement_worker = takeover.get("replacement_worker_id")
        original_event = ((takeover.get("original_claim") or {}).get("events") or [{}])[0]
        takeover_event = ((takeover.get("takeover_claim") or {}).get("events") or [{}])[0]
        final_takeover = takeover.get("final_settlement") or {}
        final_attempts = final_takeover.get("attempts") or []
        if (
            takeover.get("status") != "passed"
            or not original_worker
            or not replacement_worker
            or original_worker == replacement_worker
        ):
            errors.append("different-worker takeover scenario did not pass")
        if original_event.get("claim_owner") != original_worker or original_event.get(
            "state"
        ) != "claimed":
            errors.append("original worker claim was not proven before takeover")
        if (
            takeover_event.get("claim_owner") != replacement_worker
            or takeover_event.get("state") != "claimed"
            or not isinstance(takeover_event.get("claim_revision"), int)
            or takeover_event.get("claim_revision", 0) <= original_event.get("claim_revision", 0)
        ):
            errors.append("different-worker takeover after claim expiry was not proven")
        if [
            (row.get("kind"), row.get("state"), row.get("worker_id"), row.get("terminal"))
            for row in final_attempts
        ] != [("dispatch", "confirmed", replacement_worker, True)]:
            errors.append("takeover external-attempt lifecycle was incomplete")
        takeover_ledger = takeover.get("external_ledger") or {}
        if takeover_ledger.get("dispatch_calls") != 1 or takeover_ledger.get(
            "recovery_observations"
        ) != 0:
            errors.append("takeover duplicated dispatch or performed an unexpected recovery read")
        takeover_settlement = takeover.get("settlement") or {}
        takeover_observation = takeover_settlement.get("authoritative_external_observation") or {}
        takeover_fact = (takeover_observation.get("evidence") or {}).get(
            "external_observation"
        ) or {}
        takeover_adjudication = takeover_settlement.get("settlement_adjudication") or {}
        if (
            takeover_observation.get("kind") != "marker_search"
            or takeover_observation.get("observed_applied") is not True
            or takeover_fact.get("source") != "external_marker_search"
            or takeover_adjudication.get("outcome") != "confirmed"
        ):
            errors.append("takeover lacks authoritative confirmed settlement evidence")
        takeover_noop = takeover.get("no_duplicate_after_takeover_restart") or {}
        if takeover_noop.get("snapshot_unchanged") is not True or takeover_noop.get(
            "external_ledger_unchanged"
        ) is not True:
            errors.append("post-takeover worker restart duplicated dispatch or settlement")

        stale = evidence.get("stale_original_worker_rejection") or {}
        if (
            stale.get("status") != "passed"
            or stale.get("original_worker_id") != original_worker
            or stale.get("replacement_worker_id") != replacement_worker
            or stale.get("original_log_recorded_stale_claim_rejection") is not True
            or stale.get("snapshot_unchanged") is not True
            or stale.get("original_worker_attempt_count") != 0
        ):
            errors.append("stale original worker rejection after takeover was not proven")

        settlement = evidence.get("external_attempt_settlement") or {}
        duplicates = settlement.get("no_duplicate_dispatch_or_settlement") or {}
        if settlement.get("status") != "passed":
            errors.append("external-attempt settlement scenario did not pass")
        if not settlement.get("same_logical_worker_restart") or not settlement.get(
            "different_worker_takeover"
        ):
            errors.append("external-attempt settlement evidence omitted a required process path")
        if not duplicates.get("after_same_worker_restart") or not duplicates.get(
            "after_takeover"
        ):
            errors.append("external-attempt duplicate prevention evidence is incomplete")

        downstream = evidence.get("downstream_failure_projection") or {}
        downstream_freshness = downstream.get("projection_freshness") or {}
        downstream_snapshot = downstream.get("snapshot") or {}
        downstream_attempt = (downstream_snapshot.get("attempts") or [{}])[0]
        downstream_adjudication = (downstream_snapshot.get("adjudications") or [{}])[0]
        if (
            downstream.get("status") != "passed"
            or downstream_freshness.get("fresh") is not False
            or downstream_freshness.get("state") != "pending"
            or downstream_attempt.get("state") != "not_applied"
            or downstream_adjudication.get("outcome") != "not_applied"
        ):
            errors.append("downstream failure freshness distinction was not proven")

        reconciliation = evidence.get("reconciliation") or {}
        if reconciliation.get("ok") is not True:
            errors.append("reconciliation did not complete across its worker boundary")
        pg_loss = evidence.get("postgresql_loss") or {}
        if (pg_loss.get("health_while_down") or {}).get("ok") is not False:
            errors.append("service health did not observe PostgreSQL loss")
        if (pg_loss.get("health_after_restart") or {}).get("ok") is not True:
            errors.append("service health did not recover after PostgreSQL path restart")
        mutation = pg_loss.get("mutation_result") or {}
        mutation_errors = mutation.get("errors") or []
        if (
            pg_loss.get("mutation_status") != 503
            or mutation.get("ok") is not False
            or mutation.get("code") != "BACKEND_REJECTED"
            or mutation.get("retryable") is not True
            or not mutation_errors
            or mutation_errors[0].get("rule") != "postgresql_authority_unavailable"
        ):
            errors.append("PostgreSQL-loss fail-closed mutation path was not proven")
        if pg_loss.get("request_absent_after_restart") is not True:
            errors.append("failed mutation request absence after restart was not proven")
        if pg_loss.get("task_absent_after_restart") is not True:
            errors.append("failed mutation task absence after restart was not proven")

    return {
        "ok": not errors,
        "errors": errors,
        "process_count": len(process_records),
        "distinct_pid_count": len(pids),
        "runtime_identity_report_count": len(identity_records),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-runtime-wiring-rehearsal")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--database-name", default=DEFAULT_DATABASE)
    parser.add_argument("--database-user", default="dish")
    parser.add_argument("--database-password", default="dish")
    parser.add_argument("--compose-project", default=f"dish-section3-{os.getpid()}")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--received-archive-name")
    parser.add_argument("--received-archive-sha256")
    parser.add_argument("--base-identity")
    parser.add_argument(
        "--compose-timeout-seconds", type=float, default=DEFAULT_COMPOSE_TIMEOUT_SECONDS
    )
    parser.add_argument("--pytest-timeout-seconds", type=float, default=DEFAULT_PYTEST_TIMEOUT_SECONDS)
    parser.add_argument(
        "--cleanup-timeout-seconds", type=float, default=DEFAULT_CLEANUP_TIMEOUT_SECONDS
    )
    parser.add_argument(
        "--termination-grace-seconds", type=float, default=DEFAULT_TERMINATION_GRACE_SECONDS
    )
    parser.add_argument("--keep-instance", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.database_name = _safe_database_name(args.database_name)
        args.port = _safe_port(args.port)
        args.compose_project = _safe_project(args.compose_project)
        args.compose_timeout_seconds = _safe_timeout(args.compose_timeout_seconds, name="compose timeout")
        args.pytest_timeout_seconds = _safe_timeout(args.pytest_timeout_seconds, name="pytest timeout")
        args.cleanup_timeout_seconds = _safe_timeout(args.cleanup_timeout_seconds, name="cleanup timeout")
        args.termination_grace_seconds = _safe_timeout(
            args.termination_grace_seconds, name="termination grace"
        )
        if not re.fullmatch(r"[a-z][a-z0-9_]*", args.database_user):
            raise RehearsalConfigurationError("database user must be a simple lowercase identifier")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.database_password):
            raise RehearsalConfigurationError("database password must use simple non-shell characters")
        if args.received_archive_sha256 is not None and not re.fullmatch(
            r"[0-9a-f]{64}", args.received_archive_sha256
        ):
            raise RehearsalConfigurationError("received archive SHA-256 must be 64 lowercase hex characters")
    except RehearsalConfigurationError as exc:
        raise SystemExit(str(exc)) from exc

    output = args.output.expanduser().resolve(strict=False)
    evidence_root = args.evidence_dir.expanduser().resolve(strict=False)
    evidence_root.mkdir(parents=True, exist_ok=True)
    os.chmod(evidence_root, 0o700)
    run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    run_evidence = evidence_root / run_id
    run_evidence.mkdir(mode=0o700)
    report = _base_report(args, run_evidence=run_evidence)

    env = os.environ.copy()
    scrubbed_asana_keys = sorted(key for key in env if "ASANA" in key.upper())
    scrubbed_dish_keys = sorted(key for key in env if key.startswith("DISH_"))
    for key in sorted(set(scrubbed_asana_keys) | set(scrubbed_dish_keys)):
        env.pop(key, None)
    report["scrubbed_asana_environment_keys"] = scrubbed_asana_keys
    report["scrubbed_dish_environment_keys"] = scrubbed_dish_keys

    compose_command, probes = _find_compose_command(
        cwd=ROOT,
        env=env,
        evidence=run_evidence,
        termination_grace_seconds=args.termination_grace_seconds,
    )
    commands: list[dict[str, Any]] = list(probes)
    report["commands"] = commands
    if compose_command is None:
        blocker = (
            "Docker Compose is unavailable; no native PostgreSQL process was started and "
            "SQLite/PGlite were not substituted"
        )
        report.update(
            {
                "status": "blocked",
                "first_attempt": {"status": "blocked", "pytest_exit_status": None},
                "required_scenarios": _blocked_scenarios(blocker),
                "unavailable_native_evidence": blocker,
                "evidence_validation": {"ok": False, "errors": ["native PostgreSQL unavailable"]},
            }
        )
        _finalize(report)
        write_json_atomic(output, report)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "path": str(output),
                    "report_sha256": report["report_sha256"],
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 3

    compose_file = run_evidence / "compose.section3.yaml"
    compose_file.write_text(
        _compose_payload(
            database=args.database_name,
            port=args.port,
            user=args.database_user,
            password=args.database_password,
        ),
        encoding="utf-8",
    )
    os.chmod(compose_file, 0o600)
    compose = [*compose_command, "-p", args.compose_project, "-f", str(compose_file)]
    dsn = (
        f"postgresql+psycopg://{args.database_user}:{args.database_password}"
        f"@127.0.0.1:{args.port}/{args.database_name}?connect_timeout=10"
    )
    env.update(
        {
            "DISH_PROFILE": "test",
            "DISH_TEST_POSTGRESQL_DSN": dsn,
            "DISH_SECTION1_COMPOSE_JSON": json.dumps(compose),
            "DISH_SECTION1_EVIDENCE_DIR": str(run_evidence),
            "DISH_SECTION1_EXTERNAL_COMMAND_TIMEOUT_SECONDS": str(args.compose_timeout_seconds),
            "DISH_SECTION1_TERMINATION_GRACE_SECONDS": str(args.termination_grace_seconds),
            "PSYCOPG_IMPL": env.get("PSYCOPG_IMPL", "python"),
        }
    )

    startup_ok = False
    return_code = 1
    junit = run_evidence / "pytest-section3.xml"
    cleanup_errors: list[str] = []
    cleanup_required = False
    try:
        up = run_external_command(
            [*compose, "up", "-d", "--wait", "--wait-timeout", "90"],
            cwd=ROOT,
            env=env,
            log_path=run_evidence / "postgresql-up.log",
            timeout_seconds=args.compose_timeout_seconds,
            termination_grace_seconds=args.termination_grace_seconds,
            label="postgresql-compose-up",
        )
        commands.append(up)
        if not _command_succeeded(up):
            blocker = "dedicated PostgreSQL Compose startup failed: " + _command_failure(up)
            report.update(
                {
                    "status": "blocked",
                    "first_attempt": {"status": "blocked", "pytest_exit_status": None},
                    "required_scenarios": _blocked_scenarios(blocker),
                    "unavailable_native_evidence": blocker,
                }
            )
            return_code = 3
        else:
            startup_ok = True
            report["postgresql_server_identity"] = _probe_native(dsn)
            pytest_command = [
                args.python,
                "-m",
                "pytest",
                "--postgresql",
                "--junitxml",
                str(junit),
                "-q",
                TEST_NODE,
            ]
            pytest_run = run_external_command(
                pytest_command,
                cwd=ROOT,
                env=env,
                log_path=run_evidence / "pytest-section3-first.log",
                timeout_seconds=args.pytest_timeout_seconds,
                termination_grace_seconds=args.termination_grace_seconds,
                label="pytest-section3-runtime-wiring-first-attempt",
            )
            commands.append(pytest_run)
            orphan_errors = _terminate_incomplete_process_groups(
                run_evidence / "processes", timeout_seconds=args.termination_grace_seconds
            )
            cases, junit_errors = _parse_junit(junit)
            junit_errors.extend(orphan_errors)
            scenario, scenario_errors = _load_single_scenario(run_evidence / "scenarios")
            junit_errors.extend(scenario_errors)
            processes = _read_json_files(run_evidence / "processes")
            identities = _read_json_files(run_evidence / "runtime-identities")
            validation = _validate_evidence(
                cases=cases,
                junit_errors=junit_errors,
                scenario=scenario,
                process_records=processes,
                identity_records=identities,
            )
            scenario_evidence = None if scenario is None else scenario.get("evidence")
            runtime_identity = None
            if isinstance(scenario_evidence, dict):
                runtime_identity = (scenario_evidence.get("service_health") or {}).get("identity")
            ok = _command_succeeded(pytest_run) and validation["ok"]
            scenario_status = "passed" if ok else "failed"
            report.update(
                {
                    "status": "passed" if ok else "failed",
                    "ok": ok,
                    "first_attempt": {
                        "status": "passed" if _command_succeeded(pytest_run) else "failed",
                        "pytest_exit_status": pytest_run["final_exit_status"],
                        "completion_state": pytest_run["completion_state"],
                        "timed_out": pytest_run["timed_out"],
                        "junit_path": str(junit),
                        "cases": cases,
                    },
                    "exact_runtime_identity": runtime_identity,
                    "processes": processes,
                    "runtime_identity_reports": identities,
                    "scenario_evidence": scenario,
                    "required_scenarios": _reported_scenarios(
                        scenario_evidence if isinstance(scenario_evidence, dict) else None,
                        status=scenario_status,
                    ),
                    "failure_paths": (
                        {}
                        if not isinstance(scenario_evidence, dict)
                        else {
                            "same_logical_worker_restart_after_process_death": scenario_evidence.get(
                                "same_logical_worker_restart"
                            ),
                            "different_worker_takeover_after_claim_expiry": scenario_evidence.get(
                                "different_worker_takeover"
                            ),
                            "stale_original_worker_rejection_after_takeover": scenario_evidence.get(
                                "stale_original_worker_rejection"
                            ),
                            "external_attempt_settlement_lifecycle": scenario_evidence.get(
                                "external_attempt_settlement"
                            ),
                            "unsupported_test_service_routes_fail_closed": scenario_evidence.get(
                                "unsupported_test_service_routes"
                            ),
                            "postgresql_loss_fail_closed": scenario_evidence.get("postgresql_loss"),
                            "downstream_failure_freshness": scenario_evidence.get(
                                "downstream_failure_projection"
                            ),
                        }
                    ),
                    "evidence_validation": validation,
                    "unavailable_native_evidence": None,
                }
            )
            return_code = 0 if ok else 1
    except (OSError, RehearsalConfigurationError, SQLAlchemyError, subprocess.SubprocessError) as exc:
        blocker = f"{type(exc).__name__}: {exc}"
        report.update(
            {
                "status": "failed" if startup_ok else "blocked",
                "ok": False,
                "first_attempt": (
                    report["first_attempt"]
                    if startup_ok
                    else {"status": "blocked", "pytest_exit_status": None}
                ),
                "required_scenarios": (
                    report["required_scenarios"] if startup_ok else _blocked_scenarios(blocker)
                ),
                "unavailable_native_evidence": None if startup_ok else blocker,
                "execution_error": blocker,
            }
        )
        return_code = 1 if startup_ok else 3
    finally:
        if startup_ok:
            logs = run_external_command(
                [*compose, "logs", "--no-color", "postgres"],
                cwd=ROOT,
                env=env,
                log_path=run_evidence / "postgresql-process.log",
                timeout_seconds=args.cleanup_timeout_seconds,
                termination_grace_seconds=args.termination_grace_seconds,
                label="postgresql-compose-logs",
            )
            commands.append(logs)
            if not _command_succeeded(logs):
                cleanup_errors.append("PostgreSQL log capture failed: " + _command_failure(logs))
        if not args.keep_instance:
            down = run_external_command(
                [*compose, "down", "--volumes", "--remove-orphans"],
                cwd=ROOT,
                env=env,
                log_path=run_evidence / "postgresql-down.log",
                timeout_seconds=args.cleanup_timeout_seconds,
                termination_grace_seconds=args.termination_grace_seconds,
                label="postgresql-compose-down",
            )
            commands.append(down)
            if not _command_succeeded(down):
                cleanup_errors.append("PostgreSQL cleanup failed: " + _command_failure(down))
                cleanup_required = True
        report["commands"] = commands
        report["cleanup_errors"] = cleanup_errors
        report["manual_cleanup_required"] = cleanup_required
        report["manual_cleanup"] = (
            {
                "compose_project": args.compose_project,
                "compose_file": str(compose_file),
                "command": [*compose, "down", "--volumes", "--remove-orphans"],
                "evidence_dir": str(run_evidence),
            }
            if cleanup_required
            else None
        )
        if cleanup_errors:
            report["status"] = "failed"
            report["ok"] = False
            return_code = 1
        _finalize(report)
        write_json_atomic(output, report)

    print(
        json.dumps(
            {
                "status": report["status"],
                "path": str(output),
                "report_sha256": report["report_sha256"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
