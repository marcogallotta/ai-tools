"""Maintained TEST-only Stage 6 cutover activation/checkpoint rehearsal.

The runner owns one disposable native PostgreSQL Compose target, executes the
literal activation/checkpoint scenario plus the existing authenticated legacy
writer-fence HTTP proof, and emits one bounded report. It never substitutes
SQLite/PGlite and never reaches production or Asana.
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

from .process_failure_rehearsal import (
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
from .runtime_wiring_rehearsal import (
    _load_single_scenario as _load_runtime_wiring_scenario,
    _validate_evidence as _validate_runtime_wiring_evidence,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 55445
DEFAULT_DATABASE = "dish_stage6_activation_test"
NATIVE_NODE = (
    "tests/postgresql/native/test_cutover_activation_checkpoint_rehearsal.py::"
    "test_stage6_activation_checkpoints_survive_process_death_and_stale_writer_is_fenced"
)
RUNTIME_WIRING_NODE = (
    "tests/postgresql/native/test_runtime_wiring_rehearsal.py::"
    "test_runtime_wiring_rehearsal_across_service_and_worker_processes"
)
FENCE_HTTP_NODE = (
    "tests/postgresql/test_stage6_legacy_writer_fence.py::"
    "test_http_fence_runs_after_authentication_and_before_body_parsing"
)
TEST_NODES = (NATIVE_NODE, RUNTIME_WIRING_NODE, FENCE_HTTP_NODE)
REQUIRED_STATES = (
    "prepared",
    "fenced",
    "activated",
    "rollback_burned",
    "admission_open",
    "first_admission_verified",
    "completed",
)


def _safe_project(value: str) -> str:
    project = value.strip().lower()
    if not re.fullmatch(r"dish-stage6-activation-[a-z0-9-]+", project):
        raise RehearsalConfigurationError(
            "compose project must match dish-stage6-activation-<lowercase-safe-suffix>"
        )
    return project


def _git_identity() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-c", f"safe.directory={ROOT.parent}", "rev-parse", "HEAD"],
            cwd=ROOT,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout.strip() or None


def _finalize(report: dict[str, Any]) -> None:
    report.pop("report_sha256", None)
    report["report_sha256"] = hashlib.sha256(_canonical_bytes(report)).hexdigest()


def _base_report(args: argparse.Namespace, *, run_evidence: Path) -> dict[str, Any]:
    return {
        "format": "dish-postgresql-stage6-activation-rehearsal-v1",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "scope": "database-backend-migration-10.4-stage6-activation-checkpoint-rehearsal",
        "status": "initializing",
        "ok": False,
        "repository_commit": _git_identity(),
        "test_nodes": list(TEST_NODES),
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
        "required_states": list(REQUIRED_STATES),
        "first_attempt": {"status": "initializing", "pytest_exit_status": None},
        "postgresql_server_identity": None,
        "scenario_evidence": None,
        "runtime_wiring_scenario_evidence": None,
        "processes": [],
        "runtime_identity_reports": [],
        "evidence_validation": {"ok": False, "errors": ["not validated"]},
        "commands": [],
        "cleanup_errors": [],
        "manual_cleanup_required": False,
        "manual_cleanup": None,
    }


def _load_scenario(directory: Path) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    matches: list[dict[str, Any]] = []
    for record in _read_json_files(directory):
        if "read_error" in record:
            errors.append(f"unreadable scenario {record['path']}: {record['read_error']}")
            continue
        payload = record.get("payload")
        if isinstance(payload, dict) and payload.get("scenario") == "cutover-activation-checkpoints":
            matches.append(payload)
    if len(matches) != 1:
        errors.append(f"expected exactly one cutover activation scenario, found {len(matches)}")
        return None, errors
    return matches[0], errors


def _validate_evidence(
    *,
    cases: dict[str, dict[str, Any]],
    junit_errors: list[str],
    scenario: dict[str, Any] | None,
    processes: list[dict[str, Any]],
) -> dict[str, Any]:
    errors = list(junit_errors)
    for node in TEST_NODES:
        case = cases.get(node)
        if case is None:
            errors.append(f"JUnit is missing required node {node}")
        elif case.get("status") != "passed":
            errors.append(f"required node {node} status is {case.get('status')!r}")

    clean_processes = [row for row in processes if "read_error" not in row]
    for row in processes:
        if "read_error" in row:
            errors.append(f"unreadable process record {row['path']}: {row['read_error']}")
    if len(clean_processes) < 15:
        errors.append(
            "activation rehearsal did not record the expected checkpoint/stale-writer process boundaries"
        )

    evidence = None if scenario is None else scenario.get("evidence")
    if not isinstance(evidence, dict):
        errors.append("activation scenario evidence is missing")
    else:
        checkpoints = evidence.get("checkpoint_process_death")
        if not isinstance(checkpoints, list):
            errors.append("activation scenario lacks checkpoint process-death evidence")
        else:
            states = [row.get("state") for row in checkpoints if isinstance(row, dict)]
            if states != list(REQUIRED_STATES):
                errors.append(f"checkpoint states are {states!r}, expected {list(REQUIRED_STATES)!r}")
            for row in checkpoints:
                if not isinstance(row, dict):
                    errors.append("checkpoint process-death evidence contains a non-object")
                    continue
                if row.get("recovery_equal") is not True:
                    errors.append(f"checkpoint {row.get('state')} did not recover exact durable state")
                if row.get("terminated_process_exit_code") in {None, 0}:
                    errors.append(f"checkpoint {row.get('state')} did not terminate a process")
        writer = evidence.get("writer_fence") or {}
        if writer.get("stale_process_started_before_engagement") is not True:
            errors.append("stale legacy-writer process was not started before fence engagement")
        if writer.get("stale_process_rejected_after_engagement") is not True:
            errors.append("stale legacy-writer process did not observe the engaged fence")

    return {
        "ok": not errors,
        "errors": errors,
        "process_count": len(clean_processes),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-cutover-activation-rehearsal")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--database-name", default=DEFAULT_DATABASE)
    parser.add_argument("--database-user", default="dish")
    parser.add_argument("--database-password", default="dish")
    parser.add_argument("--compose-project", default=f"dish-stage6-activation-{os.getpid()}")
    parser.add_argument("--python", default=sys.executable)
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
    scrubbed_asana = sorted(key for key in env if "ASANA" in key.upper())
    scrubbed_dish = sorted(key for key in env if key.startswith("DISH_"))
    for key in sorted(set(scrubbed_asana) | set(scrubbed_dish)):
        env.pop(key, None)
    report["scrubbed_asana_environment_keys"] = scrubbed_asana
    report["scrubbed_dish_environment_keys"] = scrubbed_dish

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
                "unavailable_native_evidence": blocker,
                "evidence_validation": {"ok": False, "errors": ["native PostgreSQL unavailable"]},
            }
        )
        _finalize(report)
        write_json_atomic(output, report)
        print(json.dumps({"status": "blocked", "path": str(output), "report_sha256": report["report_sha256"]}, sort_keys=True, separators=(",", ":")))
        return 3

    compose_file = run_evidence / "compose.stage6-activation.yaml"
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
            "DISH_SECTION1_EVIDENCE_DIR": str(run_evidence),
            "DISH_SECTION1_COMPOSE_JSON": json.dumps(compose),
            "DISH_SECTION1_EXTERNAL_COMMAND_TIMEOUT_SECONDS": str(args.compose_timeout_seconds),
            "DISH_SECTION1_TERMINATION_GRACE_SECONDS": str(args.termination_grace_seconds),
            "PSYCOPG_IMPL": env.get("PSYCOPG_IMPL", "python"),
        }
    )

    started = False
    cleanup_errors: list[str] = []
    cleanup_required = False
    return_code = 1
    junit = run_evidence / "pytest-stage6-activation.xml"
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
                    "unavailable_native_evidence": blocker,
                }
            )
            return_code = 3
        else:
            started = True
            report["postgresql_server_identity"] = _probe_native(dsn)
            pytest_run = run_external_command(
                [
                    args.python,
                    "-m",
                    "pytest",
                    "--postgresql",
                    "--junitxml",
                    str(junit),
                    "-q",
                    *TEST_NODES,
                ],
                cwd=ROOT,
                env=env,
                log_path=run_evidence / "pytest-stage6-activation.log",
                timeout_seconds=args.pytest_timeout_seconds,
                termination_grace_seconds=args.termination_grace_seconds,
                label="pytest-stage6-activation-first-attempt",
            )
            commands.append(pytest_run)
            orphan_errors = _terminate_incomplete_process_groups(
                run_evidence / "processes", timeout_seconds=args.termination_grace_seconds
            )
            cases, junit_errors = _parse_junit(junit)
            junit_errors.extend(orphan_errors)
            scenario, scenario_errors = _load_scenario(run_evidence / "scenarios")
            cutover_junit_errors = [*junit_errors, *scenario_errors]
            runtime_scenario, runtime_scenario_errors = _load_runtime_wiring_scenario(
                run_evidence / "scenarios"
            )
            processes = _read_json_files(run_evidence / "processes")
            identities = _read_json_files(run_evidence / "runtime-identities")
            cutover_validation = _validate_evidence(
                cases=cases,
                junit_errors=cutover_junit_errors,
                scenario=scenario,
                processes=processes,
            )
            runtime_validation = _validate_runtime_wiring_evidence(
                cases=cases,
                junit_errors=[*orphan_errors, *runtime_scenario_errors],
                scenario=runtime_scenario,
                process_records=processes,
                identity_records=identities,
            )
            validation = {
                "ok": cutover_validation["ok"] and runtime_validation["ok"],
                "errors": [
                    *cutover_validation["errors"],
                    *(
                        f"runtime wiring: {error}"
                        for error in runtime_validation["errors"]
                    ),
                ],
                "cutover_activation": cutover_validation,
                "runtime_wiring": runtime_validation,
            }
            ok = _command_succeeded(pytest_run) and validation["ok"]
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
                    "scenario_evidence": scenario,
                    "runtime_wiring_scenario_evidence": runtime_scenario,
                    "processes": processes,
                    "runtime_identity_reports": identities,
                    "evidence_validation": validation,
                    "unavailable_native_evidence": None,
                }
            )
            return_code = 0 if ok else 1
    except Exception as exc:  # bounded operator runner records unexpected failures
        report.update(
            {
                "status": "failed" if started else "blocked",
                "ok": False,
                "execution_error": f"{type(exc).__name__}: {exc}",
            }
        )
        return_code = 1 if started else 3
    finally:
        if started:
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

    print(json.dumps({"status": report["status"], "path": str(output), "report_sha256": report["report_sha256"]}, sort_keys=True, separators=(",", ":")))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
