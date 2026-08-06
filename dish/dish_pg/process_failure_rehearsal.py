"""Rerunnable native-PostgreSQL process-failure rehearsal for validation-plan §1.

The package owns a dedicated Docker Compose PostgreSQL instance and invokes a
literal inventory of real command and worker subprocess tests. It never
substitutes SQLite, PGlite, mocks, or in-process calls for native/process
evidence. Scripted implementation completeness is reported separately from
native execution and certification.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 55442
DEFAULT_DATABASE = "dish_section1_process_failure_test"
DEFAULT_COMPOSE_TIMEOUT_SECONDS = 120.0
DEFAULT_PYTEST_TIMEOUT_SECONDS = 900.0
DEFAULT_CLEANUP_TIMEOUT_SECONDS = 120.0
DEFAULT_TERMINATION_GRACE_SECONDS = 5.0
COMPOSE_PROBE_TIMEOUT_SECONDS = 15.0
POSTGRES_IMAGE = "postgres:17.10"
DATABASE_NAME_PATTERN = re.compile(r"^dish_[a-z0-9_]*_test$")
POSTGRES_URL_USERINFO_PATTERN = re.compile(
    r"(?P<prefix>\bpostgres(?:ql)?(?:\+[A-Za-z0-9_.-]+)?://)[^/\s@]+@"
)

PROCESS_TEST_INVENTORY = (
    "tests/postgresql/native/test_process_failure_command.py::test_command_process_commit_before_response_replays_without_duplicate_mutation",
    "tests/postgresql/native/test_process_failure_command.py::test_command_process_disconnect_before_commit_fails_closed_and_recovers",
    "tests/postgresql/native/test_process_failure_projection.py::test_process_failure_before_claim",
    "tests/postgresql/native/test_process_failure_projection.py::test_process_failure_after_claim_before_durable_intent",
    "tests/postgresql/native/test_process_failure_projection.py::test_process_failure_after_durable_intent_before_external_call",
    "tests/postgresql/native/test_process_failure_projection.py::test_process_failure_after_ambiguous_external_response",
    "tests/postgresql/native/test_process_failure_projection.py::test_process_failure_after_settlement_before_shutdown",
    "tests/postgresql/native/test_process_failure_takeover.py::test_process_takeover_is_lease_gated_fenced_and_task_local",
    "tests/postgresql/native/test_process_failure_supervision.py::test_long_running_projection_worker_is_supervised_and_restarted",
    "tests/postgresql/native/test_process_failure_supervision.py::test_reconciliation_worker_is_supervised_and_restarted",
    "tests/postgresql/native/test_process_failure_reconciliation.py::test_reconciliation_process_loss_after_durable_run_creation_resumes_exact_run",
    "tests/postgresql/native/test_process_failure_reconciliation.py::test_reconciliation_process_loss_after_partial_corpus_resumes_without_duplicate_items",
    "tests/postgresql/native/test_process_failure_disconnect.py::test_projection_worker_fails_clearly_across_postgresql_disconnect",
    "tests/postgresql/native/test_process_failure_disconnect.py::test_reconciliation_worker_writes_nothing_while_postgresql_is_down",
)

NODE_REQUIREMENTS = {
    PROCESS_TEST_INVENTORY[0]: "command_commit_before_response_and_exact_replay",
    PROCESS_TEST_INVENTORY[1]: "command_disconnect_active_transaction_and_recovery",
    PROCESS_TEST_INVENTORY[2]: "projection_before_claim",
    PROCESS_TEST_INVENTORY[3]: "projection_after_claim_before_intent",
    PROCESS_TEST_INVENTORY[4]: "projection_after_intent_before_call",
    PROCESS_TEST_INVENTORY[5]: "projection_after_ambiguous_response",
    PROCESS_TEST_INVENTORY[6]: "projection_after_settlement",
    PROCESS_TEST_INVENTORY[7]: "worker_takeover_and_fencing",
    PROCESS_TEST_INVENTORY[8]: "long_running_projection_supervision_and_restart",
    PROCESS_TEST_INVENTORY[9]: "reconciliation_worker_supervision_and_restart",
    PROCESS_TEST_INVENTORY[10]: "reconciliation_loss_after_durable_run_creation",
    PROCESS_TEST_INVENTORY[11]: "reconciliation_loss_after_partially_recorded_corpus",
    PROCESS_TEST_INVENTORY[12]: "postgresql_disconnect_projection_worker",
    PROCESS_TEST_INVENTORY[13]: "postgresql_disconnect_reconciliation_worker",
}

NODE_SCENARIOS = {
    PROCESS_TEST_INVENTORY[0]: "command-commit-before-response-replay",
    PROCESS_TEST_INVENTORY[1]: "command-disconnect-active-transaction",
    PROCESS_TEST_INVENTORY[2]: "projection-before-claim",
    PROCESS_TEST_INVENTORY[3]: "projection-after-claim-before-intent",
    PROCESS_TEST_INVENTORY[4]: "projection-after-intent-before-call",
    PROCESS_TEST_INVENTORY[5]: "projection-after-ambiguous-response",
    PROCESS_TEST_INVENTORY[6]: "projection-after-settlement",
    PROCESS_TEST_INVENTORY[7]: "worker-takeover-and-fencing",
    PROCESS_TEST_INVENTORY[8]: "long-running-projection-supervision-restart",
    PROCESS_TEST_INVENTORY[9]: "reconciliation-worker-supervision-restart",
    PROCESS_TEST_INVENTORY[10]: "reconciliation-loss-after-durable-run",
    PROCESS_TEST_INVENTORY[11]: "reconciliation-loss-after-partial-corpus",
    PROCESS_TEST_INVENTORY[12]: "postgresql-disconnect-projection-worker",
    PROCESS_TEST_INVENTORY[13]: "postgresql-disconnect-reconciliation-worker",
}

NOT_IMPLEMENTED_SCENARIOS: tuple[str, ...] = ()

CONTENTION_POLICY_NOTE = (
    "The implemented IntegrityError contention mappings are confined to request admission "
    "(WorkflowRepository.admit_request) and actor-lease acquisition "
    "(WorkflowAuthorityService.acquire_actor_lease); their native ten-way races are covered "
    "elsewhere and are not repeated. No retry policy is currently defined for PostgreSQL "
    "SQLSTATE 40P01 or 40001 on the command or worker paths, so §1 requires no invented exercise "
    "or generic retry framework for those cases."
)


class RehearsalConfigurationError(ValueError):
    """Unsafe or incomplete rehearsal configuration."""


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def write_json_atomic(path: Path, value: object) -> None:
    destination = path.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(_canonical_bytes(value))
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_database_name(value: str) -> str:
    name = value.strip().lower()
    if not DATABASE_NAME_PATTERN.fullmatch(name) or "prod" in name or "production" in name:
        raise RehearsalConfigurationError(
            "database name must match dish_<isolated-name>_test and must not contain prod/production"
        )
    return name


def _safe_port(value: int) -> int:
    if value < 1024 or value > 65535:
        raise RehearsalConfigurationError("PostgreSQL port must be between 1024 and 65535")
    return value


def _safe_project(value: str) -> str:
    project = value.strip().lower()
    if not re.fullmatch(r"dish-section1-[a-z0-9-]+", project):
        raise RehearsalConfigurationError(
            "compose project must match dish-section1-<lowercase-safe-suffix>"
        )
    return project


def _safe_timeout(value: float, *, name: str) -> float:
    timeout = float(value)
    if not (timeout > 0.0 and timeout < float("inf")):
        raise RehearsalConfigurationError(f"{name} must be finite and positive")
    return timeout


def redact_evidence_text(value: str) -> str:
    """Remove PostgreSQL URL user information before evidence is persisted."""
    return POSTGRES_URL_USERINFO_PATTERN.sub(r"\g<prefix><redacted>@", value)


def redact_command_for_evidence(command: Sequence[str]) -> list[str]:
    """Return a persistence-safe copy without changing the executed command."""
    return [redact_evidence_text(str(value)) for value in command]


def redact_evidence_log(path: Path) -> None:
    """Sanitize PostgreSQL credentials that a child may echo into its text log."""
    if not path.is_file():
        return
    original = path.read_text(encoding="utf-8", errors="replace")
    redacted = redact_evidence_text(original)
    if redacted == original:
        return
    path.write_text(redacted, encoding="utf-8")
    os.chmod(path, 0o600)
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def notify_process_barrier(label: str, payload: dict[str, Any] | None = None) -> None:
    """Publish a deterministic process boundary and wait for the test controller."""
    socket_path = os.environ.get("DISH_SECTION1_BARRIER_SOCKET", "").strip()
    if not socket_path:
        raise RuntimeError("missing required rehearsal barrier socket")
    message = {"label": label, "pid": os.getpid(), "payload": payload or {}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall(json.dumps(message, sort_keys=True).encode("utf-8") + b"\n")
        received = b""
        while not received.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                raise RuntimeError(f"barrier {label!r} closed without release")
            received += chunk
    response = json.loads(received.decode("utf-8"))
    if response != {"action": "continue", "label": label}:
        raise RuntimeError(f"barrier {label!r} received invalid release {response!r}")


def _close_log(handle: Any) -> None:
    if handle.closed:
        return
    handle.flush()
    os.fsync(handle.fileno())
    handle.close()


def _signal_process_group(process: subprocess.Popen[Any], sig: signal.Signals) -> bool:
    try:
        os.killpg(process.pid, sig)
    except ProcessLookupError:
        return False
    return True


def run_external_command(
    command: Sequence[str],
    *,
    cwd: Path,
    env: dict[str, str],
    log_path: Path,
    timeout_seconds: float,
    termination_grace_seconds: float,
    label: str,
    record_path: Path | None = None,
) -> dict[str, Any]:
    """Run one external command with a bounded process-group lifecycle and durable log."""
    timeout_seconds = _safe_timeout(timeout_seconds, name=f"{label} timeout")
    termination_grace_seconds = _safe_timeout(
        termination_grace_seconds, name=f"{label} termination grace"
    )
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("w", encoding="utf-8")
    os.chmod(log_path, 0o600)
    process: subprocess.Popen[str] | None = None
    completion_state = "spawn_failed"
    termination_state = "not_started"
    final_exit_status: int | None = None
    failure: str | None = None
    timed_out = False
    diagnostics: list[str] = []
    try:
        try:
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=env,
                text=True,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            failure = f"{type(exc).__name__}: {exc}"
            diagnostics.append(f"external command spawn failed: {failure}")
        else:
            termination_state = "none"
            try:
                final_exit_status = process.wait(timeout=timeout_seconds)
                completion_state = "completed"
            except subprocess.TimeoutExpired:
                timed_out = True
                completion_state = "timed_out"
                failure = f"command exceeded finite timeout of {timeout_seconds:g} seconds"
                diagnostics.append(f"{failure}; terminating process group {process.pid}")
                if _signal_process_group(process, signal.SIGTERM):
                    termination_state = "sigterm"
                parent_exited = False
                try:
                    final_exit_status = process.wait(timeout=termination_grace_seconds)
                    parent_exited = True
                except subprocess.TimeoutExpired:
                    diagnostics.append(
                        "process group did not exit after SIGTERM; escalating to SIGKILL"
                    )
                if _signal_process_group(process, signal.SIGKILL):
                    termination_state = "sigkill"
                if not parent_exited:
                    try:
                        final_exit_status = process.wait(timeout=termination_grace_seconds)
                    except subprocess.TimeoutExpired:
                        termination_state = "sigkill_unreaped"
                        failure = (
                            f"{failure}; process group remained unreaped for an additional "
                            f"{termination_grace_seconds:g} seconds after SIGKILL"
                        )
    finally:
        _close_log(handle)
        redact_evidence_log(log_path)

    if diagnostics:
        with log_path.open("a", encoding="utf-8") as diagnostic_handle:
            if log_path.stat().st_size:
                diagnostic_handle.write("\n")
            for line in diagnostics:
                diagnostic_handle.write(line + "\n")
            diagnostic_handle.flush()
            os.fsync(diagnostic_handle.fileno())
    output = log_path.read_bytes()
    record = {
        "format": "dish-external-command-record-v1",
        "label": label,
        "command": redact_command_for_evidence(command),
        "pid": None if process is None else process.pid,
        "process_group_id": None if process is None else process.pid,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "timeout_seconds": timeout_seconds,
        "termination_grace_seconds": termination_grace_seconds,
        "timed_out": timed_out,
        "completion_state": completion_state,
        "termination_state": termination_state,
        "final_exit_status": final_exit_status,
        "duration_seconds": round(time.monotonic() - started, 3),
        "log_path": str(log_path),
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "failure": failure,
    }
    if record_path is not None:
        write_json_atomic(record_path, record)
    return record


def _command_succeeded(record: dict[str, Any]) -> bool:
    return (
        record["completion_state"] == "completed"
        and record["final_exit_status"] == 0
        and not record["timed_out"]
    )


def _command_failure(record: dict[str, Any]) -> str:
    if record.get("failure"):
        return str(record["failure"])
    return (
        f"command completed with state={record.get('completion_state')} "
        f"exit={record.get('final_exit_status')} log={record.get('log_path')}"
    )


def _find_compose_command(
    *,
    cwd: Path,
    env: dict[str, str],
    evidence: Path,
    termination_grace_seconds: float,
) -> tuple[list[str] | None, list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    candidates: list[list[str]] = []
    docker = shutil.which("docker")
    if docker is not None:
        candidates.append([docker, "compose"])
    legacy = shutil.which("docker-compose")
    if legacy is not None:
        candidates.append([legacy])
    for index, candidate in enumerate(candidates, start=1):
        record = run_external_command(
            [*candidate, "version"],
            cwd=cwd,
            env=env,
            log_path=evidence / f"compose-probe-{index}.log",
            timeout_seconds=COMPOSE_PROBE_TIMEOUT_SECONDS,
            termination_grace_seconds=termination_grace_seconds,
            label=f"compose-probe-{index}",
        )
        records.append(record)
        if _command_succeeded(record):
            return candidate, records
    return None, records


def _compose_payload(*, database: str, port: int, user: str, password: str) -> str:
    return (
        "services:\n"
        "  postgres:\n"
        f"    image: {POSTGRES_IMAGE}\n"
        "    environment:\n"
        f"      POSTGRES_DB: {database}\n"
        f"      POSTGRES_USER: {user}\n"
        f"      POSTGRES_PASSWORD: {password}\n"
        "    ports:\n"
        f"      - \"127.0.0.1:{port}:5432\"\n"
        "    healthcheck:\n"
        f"      test: [\"CMD-SHELL\", \"pg_isready -U {user} -d {database}\"]\n"
        "      interval: 1s\n"
        "      timeout: 3s\n"
        "      retries: 60\n"
        "    volumes:\n"
        "      - pgdata:/var/lib/postgresql/data\n"
        "volumes:\n"
        "  pgdata:\n"
    )


def _probe_native(dsn: str) -> dict[str, Any]:
    engine = create_engine(dsn, future=True, pool_pre_ping=True)
    try:
        if engine.dialect.name != "postgresql":
            raise RehearsalConfigurationError("rehearsal target is not PostgreSQL")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT current_database(), current_user, "
                    "current_setting('server_version'), version(), "
                    "inet_server_addr()::text, inet_server_port()"
                )
            ).one()
        full = str(row[3])
        lowered = full.lower()
        if any(token in lowered for token in ("pglite", "webassembly", "emscripten", "wasm32")):
            raise RehearsalConfigurationError("rehearsal target is not native PostgreSQL")
        return {
            "dialect": engine.dialect.name,
            "driver": engine.dialect.driver,
            "database": str(row[0]),
            "user": str(row[1]),
            "server_version": str(row[2]),
            "server_version_full": full,
            "server_address": None if row[4] is None else str(row[4]),
            "server_port": None if row[5] is None else int(row[5]),
        }
    finally:
        engine.dispose()


def _parse_junit(path: Path) -> tuple[dict[str, dict[str, Any]], list[str]]:
    cases: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    if not path.is_file():
        return cases, [f"JUnit file is missing: {path}"]
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as exc:
        return cases, [f"JUnit file is unreadable: {type(exc).__name__}: {exc}"]
    for case in root.iter("testcase"):
        classname = case.attrib.get("classname", "")
        name = case.attrib.get("name", "")
        nodeid = classname.replace(".", "/") + ".py::" + name
        if nodeid in cases:
            errors.append(f"JUnit contains duplicate testcase {nodeid}")
            continue
        status = "passed"
        detail = None
        for tag, mapped in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            child = case.find(tag)
            if child is not None:
                status = mapped
                detail = (child.attrib.get("message") or child.text or "").strip()
                break
        try:
            duration = float(case.attrib.get("time", "0") or 0)
        except ValueError:
            errors.append(f"JUnit testcase {nodeid} has invalid duration")
            duration = 0.0
        cases[nodeid] = {
            "status": status,
            "duration_seconds": duration,
            "detail": detail,
        }
    return cases, errors


def _terminate_incomplete_process_groups(
    directory: Path,
    *,
    timeout_seconds: float,
) -> list[str]:
    """SIGKILL any worker group left running after pytest and finalize its record."""
    errors: list[str] = []
    if not directory.is_dir():
        return errors
    timeout_seconds = _safe_timeout(timeout_seconds, name="orphan worker cleanup timeout")
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("completion_state") != "running":
            continue
        pid = payload.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            errors.append(f"running process record lacks a valid pid: {path}")
            continue
        try:
            pidfd = os.pidfd_open(pid)
        except (AttributeError, OSError) as exc:
            errors.append(
                f"could not open pidfd for incomplete process group {pid}: {type(exc).__name__}: {exc}"
            )
            continue
        try:
            try:
                os.killpg(pid, signal.SIGKILL)
            except ProcessLookupError:
                errors.append(
                    f"incomplete process group {pid} disappeared before final exit status was recorded"
                )
                continue
            ready, _writable, _exceptional = select.select([pidfd], [], [], timeout_seconds)
            if not ready:
                payload.update(
                    {
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "final_exit_status": None,
                        "completion_state": "timed_out",
                        "termination_state": "sigkill_unreaped",
                        "detail": (
                            "pytest ended before this worker finalized; SIGKILL was sent but the "
                            f"process did not exit within {timeout_seconds:g} seconds"
                        ),
                    }
                )
                write_json_atomic(path, payload)
                errors.append(f"incomplete process group {pid} remained after SIGKILL: {path}")
                continue
            payload.update(
                {
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "final_exit_status": -int(signal.SIGKILL),
                    "completion_state": "terminated",
                    "termination_state": "sigkill",
                    "detail": "pytest ended before finalization; runner terminated the worker group",
                }
            )
            write_json_atomic(path, payload)
        finally:
            os.close(pidfd)
    return errors


def _read_json_files(directory: Path) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    if not directory.is_dir():
        return values
    for path in sorted(directory.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            values.append({"path": str(path), "read_error": f"{type(exc).__name__}: {exc}"})
        else:
            values.append({"path": str(path), "payload": payload})
    return values


def _section1_pytest_command(*, python: str, junit: Path) -> list[str]:
    """Build the literal §1 run without invoking the repository-wide lane selector."""

    # ``--native-postgresql`` is a governed full-repository selector. This package owns a
    # smaller literal inventory after independently proving the dedicated native server, so
    # it uses ``--postgresql`` with exact node IDs instead of violating lane collection rules.
    return [
        python,
        "-m",
        "pytest",
        "--postgresql",
        "--junitxml",
        str(junit),
        "-q",
        *PROCESS_TEST_INVENTORY,
    ]


def _requirements_from_cases(
    cases: dict[str, dict[str, Any]],
    *,
    unavailable_reason: str | None = None,
) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    for nodeid in PROCESS_TEST_INVENTORY:
        result = cases.get(nodeid)
        if result is not None:
            status = result["status"]
            detail = result["detail"]
        elif unavailable_reason is not None:
            status = "blocked_by_unavailable_native_infrastructure"
            detail = unavailable_reason
        else:
            status = "not_run"
            detail = None
        requirements.append(
            {
                "requirement": NODE_REQUIREMENTS[nodeid],
                "nodeid": nodeid,
                "scenario": NODE_SCENARIOS[nodeid],
                "implementation_status": "implemented",
                "status": status,
                "detail": detail,
            }
        )
    requirements.append(
        {
            "requirement": "deadlock_and_serialization_policy",
            "implementation_status": "not_applicable_no_defined_policy",
            "status": "not_exercised_no_defined_policy",
            "covered_elsewhere_not_repeated": [
                "dish_pg/workflow.py:WorkflowRepository.admit_request IntegrityError -> ContentionLost",
                "dish_pg/workflow.py:WorkflowAuthorityService.acquire_actor_lease IntegrityError -> ContentionLost",
                "tests/postgresql/native/test_stage_a_concurrency.py ten-way request/lease races",
            ],
            "currently_undefined_policy_paths": [
                "dish_pg/database.py:session_scope (40P01/40001 rollback and re-raise)",
                "dish_pg/command_port.py:PostgresCommandPort.execute",
                "dish_pg/projection_worker.py:ProjectionWorker.run_once",
                "dish_pg/reconciliation_worker.py:ReconciliationWorker.run_once",
            ],
            "detail": CONTENTION_POLICY_NOTE,
        }
    )
    return requirements


def _test_summary(cases: dict[str, dict[str, Any]]) -> dict[str, Any]:
    statuses = {
        nodeid: ("not_run" if nodeid not in cases else str(cases[nodeid]["status"]))
        for nodeid in PROCESS_TEST_INVENTORY
    }
    return {
        "inventory_count": len(PROCESS_TEST_INVENTORY),
        "executed_count": sum(status != "not_run" and status != "skipped" for status in statuses.values()),
        "passed_count": sum(status == "passed" for status in statuses.values()),
        "failed_count": sum(status in {"failed", "error"} for status in statuses.values()),
        "skipped_count": sum(status == "skipped" for status in statuses.values()),
        "not_run_count": sum(status == "not_run" for status in statuses.values()),
        "statuses": statuses,
    }


def _validate_evidence(
    *,
    cases: dict[str, dict[str, Any]],
    junit_errors: list[str],
    scenario_records: list[dict[str, Any]],
    process_records: list[dict[str, Any]],
    command_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    errors = list(junit_errors)
    unknown_cases = sorted(set(cases) - set(PROCESS_TEST_INVENTORY))
    if unknown_cases:
        errors.append(f"JUnit contains unexpected process tests: {unknown_cases}")

    scenarios_by_node: dict[str, list[dict[str, Any]]] = {}
    scenario_names: dict[str, str] = {}
    valid_scenarios = 0
    for record in scenario_records:
        path = str(record.get("path"))
        if "read_error" in record:
            errors.append(f"scenario artifact unreadable at {path}: {record['read_error']}")
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            errors.append(f"scenario artifact is not an object: {path}")
            continue
        nodeid = payload.get("nodeid")
        scenario = payload.get("scenario")
        if payload.get("format") != "dish-section1-scenario-evidence-v2":
            errors.append(f"scenario artifact has invalid format: {path}")
            continue
        if not isinstance(nodeid, str) or nodeid not in NODE_SCENARIOS:
            errors.append(f"scenario artifact has unknown nodeid: {path}")
            continue
        if scenario != NODE_SCENARIOS[nodeid]:
            errors.append(
                f"scenario artifact {path} names {scenario!r}; expected {NODE_SCENARIOS[nodeid]!r}"
            )
            continue
        if payload.get("completion_state") != "scenario_assertions_completed":
            errors.append(f"scenario artifact is not complete: {path}")
            continue
        if not isinstance(payload.get("evidence"), dict):
            errors.append(f"scenario artifact evidence is not an object: {path}")
            continue
        if nodeid in scenarios_by_node:
            errors.append(f"duplicate scenario artifact for {nodeid}")
        if scenario in scenario_names:
            errors.append(
                f"duplicate scenario name {scenario!r} in {scenario_names[scenario]} and {path}"
            )
        scenarios_by_node.setdefault(nodeid, []).append(record)
        scenario_names[str(scenario)] = path
        valid_scenarios += 1

    processes_by_node: dict[str, list[dict[str, Any]]] = {}
    process_ids: dict[str, str] = {}
    valid_processes = 0
    for record in process_records:
        path = str(record.get("path"))
        if "read_error" in record:
            errors.append(f"process record unreadable at {path}: {record['read_error']}")
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            errors.append(f"process record is not an object: {path}")
            continue
        if payload.get("format") != "dish-section1-process-record-v2":
            errors.append(f"process record has invalid format: {path}")
            continue
        process_id = payload.get("process_id")
        nodeid = payload.get("nodeid")
        final_exit_status = payload.get("final_exit_status")
        completion_state = payload.get("completion_state")
        termination_state = payload.get("termination_state")
        if not isinstance(process_id, str) or not process_id:
            errors.append(f"process record lacks process_id: {path}")
            continue
        if process_id in process_ids:
            errors.append(f"duplicate process_id {process_id!r} in {process_ids[process_id]} and {path}")
        process_ids[process_id] = path
        if not isinstance(nodeid, str) or nodeid not in NODE_SCENARIOS:
            errors.append(f"process record has unknown nodeid: {path}")
            continue
        if not isinstance(final_exit_status, int) or isinstance(final_exit_status, bool):
            errors.append(f"process record lacks final integer exit status: {path}")
            continue
        if completion_state not in {"completed", "terminated", "timed_out"}:
            errors.append(f"process record has incomplete completion state {completion_state!r}: {path}")
            continue
        if termination_state not in {"none", "sigterm", "sigkill", "sigkill_unreaped"}:
            errors.append(f"process record has invalid termination state {termination_state!r}: {path}")
            continue
        expected_termination_states = {
            "completed": {"none"},
            "terminated": {"sigterm", "sigkill"},
            "timed_out": {"sigterm", "sigkill", "sigkill_unreaped"},
        }
        if termination_state not in expected_termination_states[completion_state]:
            errors.append(
                f"process record has inconsistent completion/termination states "
                f"{completion_state!r}/{termination_state!r}: {path}"
            )
            continue
        processes_by_node.setdefault(nodeid, []).append(record)
        valid_processes += 1

    valid_external_commands = 0
    for record in command_records or []:
        path = str(record.get("path"))
        if "read_error" in record:
            errors.append(f"external command record unreadable at {path}: {record['read_error']}")
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            errors.append(f"external command record is not an object: {path}")
            continue
        if payload.get("format") != "dish-external-command-record-v1":
            errors.append(f"external command record has invalid format: {path}")
            continue
        if payload.get("completion_state") not in {"completed", "timed_out", "spawn_failed"}:
            errors.append(f"external command record has invalid completion state: {path}")
            continue
        if payload.get("completion_state") != "spawn_failed":
            final_exit_status = payload.get("final_exit_status")
            if not isinstance(final_exit_status, int) or isinstance(final_exit_status, bool):
                errors.append(f"external command record lacks final integer exit status: {path}")
                continue
        if (
            payload.get("completion_state") != "completed"
            or payload.get("final_exit_status") != 0
            or payload.get("timed_out") is not False
        ):
            errors.append(
                f"external command record reports failure: state={payload.get('completion_state')} "
                f"exit={payload.get('final_exit_status')} timed_out={payload.get('timed_out')}: {path}"
            )
            continue
        valid_external_commands += 1

    for nodeid in PROCESS_TEST_INVENTORY:
        status = "not_run" if nodeid not in cases else str(cases[nodeid]["status"])
        executed = status not in {"not_run", "skipped"}
        scenario_count = len(scenarios_by_node.get(nodeid, []))
        process_count = len(processes_by_node.get(nodeid, []))
        if executed:
            if scenario_count != 1:
                errors.append(
                    f"executed test {nodeid} requires exactly one valid scenario artifact; found {scenario_count}"
                )
            if process_count < 1:
                errors.append(f"executed test {nodeid} has no valid final process record")
        else:
            if scenario_count:
                errors.append(f"{status} test {nodeid} unexpectedly has scenario evidence")
            if process_count:
                errors.append(f"{status} test {nodeid} unexpectedly has process records")
        if status != "passed" and scenario_count:
            errors.append(
                f"scenario artifact for {nodeid} is inconsistent with JUnit status {status!r}"
            )
        if status == "passed" and any(
            record["payload"].get("completion_state") == "timed_out"
            for record in processes_by_node.get(nodeid, [])
        ):
            errors.append(f"passed JUnit test {nodeid} has a timed-out process record")

    return {
        "ok": not errors,
        "errors": errors,
        "valid_scenario_count": valid_scenarios,
        "valid_process_count": valid_processes,
        "valid_external_command_count": valid_external_commands,
        "expected_scenario_count": _test_summary(cases)["executed_count"],
    }


def _base_report(args: argparse.Namespace, *, run_evidence: Path) -> dict[str, Any]:
    return {
        "format": "dish-postgresql-process-failure-rehearsal-v4",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "scope": "rerunnable-process-failure-package-for-database-backend-postgresql-test-plan-section-1",
        "delivery_classification": (
            "complete_section1_scripted_package"
            if not NOT_IMPLEMENTED_SCENARIOS
            else "incomplete_section1_scripted_package"
        ),
        "section1_scripted_package_complete": not NOT_IMPLEMENTED_SCENARIOS,
        "section1_implementation_status": (
            "complete" if not NOT_IMPLEMENTED_SCENARIOS else "incomplete"
        ),
        "section1_implemented": not NOT_IMPLEMENTED_SCENARIOS,
        "section1_certified": False,
        "not_implemented_scenarios": list(NOT_IMPLEMENTED_SCENARIOS),
        "implemented_scenario_count": len(PROCESS_TEST_INVENTORY),
        "required_scenario_count": len(PROCESS_TEST_INVENTORY) + len(NOT_IMPLEMENTED_SCENARIOS),
        "native_execution_blocked_scenarios": [],
        "native_execution_blocker": None,
        "command_process_requirements_blocked": False,
        "remaining_native_scenarios_blocked": False,
        "native_postgresql_required": True,
        "separate_os_processes_required": True,
        "source_contract_substitutions_allowed": False,
        "database_name": args.database_name,
        "host_port": args.port,
        "compose_project": args.compose_project,
        "postgres_image": POSTGRES_IMAGE,
        "run_evidence_dir": str(run_evidence),
        "timeouts": {
            "compose_seconds": args.compose_timeout_seconds,
            "pytest_seconds": args.pytest_timeout_seconds,
            "cleanup_seconds": args.cleanup_timeout_seconds,
            "termination_grace_seconds": args.termination_grace_seconds,
            "compose_probe_seconds": COMPOSE_PROBE_TIMEOUT_SECONDS,
        },
        "test_inventory": list(PROCESS_TEST_INVENTORY),
        "test_inventory_count": len(PROCESS_TEST_INVENTORY),
        "production_asana_touched": False,
        "production_profile_touched": False,
        "public_action_route_touched": False,
    }


def _finalize(report: dict[str, Any]) -> None:
    report.pop("report_sha256", None)
    report["report_sha256"] = hashlib.sha256(_canonical_bytes(report)).hexdigest()


COMMAND_CHILD_MODE = "_command-child"
COMMAND_CHILD_CONFIG_FORMAT = "dish-section1-command-child-config-v1"
COMMAND_CHILD_RESULT_FORMAT = "dish-section1-command-child-result-v1"
COMMAND_CHILD_SCENARIOS = frozenset(
    {"normal", "after_execution_before_commit", "after_commit_before_response"}
)


def _command_child_main(argv: list[str]) -> int:
    """Execute one real PostgreSQL command transaction in a disposable child process."""
    if len(argv) != 1:
        print("command child requires exactly one configuration path", file=sys.stderr)
        return 64
    config_path = Path(argv[0]).expanduser().resolve(strict=True)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(
            f"command child configuration is unreadable: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 64
    if not isinstance(config, dict) or config.get("format") != COMMAND_CHILD_CONFIG_FORMAT:
        print("command child configuration has an invalid format", file=sys.stderr)
        return 64
    try:
        output = Path(str(config["output"])).expanduser().resolve(strict=False)
        scenario = str(config.get("scenario", "normal"))
        command_name = str(config["command_name"])
        action_token = str(config["action_token"])
        private_token = str(config["private_token"])
        body = dict(config["body"])
        owner_id = str(config["owner_id"])
        now = datetime.fromisoformat(str(config["now"]))
    except (KeyError, TypeError, ValueError) as exc:
        print(
            f"command child configuration is incomplete: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 64
    if scenario not in COMMAND_CHILD_SCENARIOS:
        print(f"command child scenario is unsupported: {scenario!r}", file=sys.stderr)
        return 64

    from sqlalchemy.orm import Session, sessionmaker

    from dish_pg.command_port import PostgresCommandPort
    from dish_pg.protocol import PostgresProtocolService, ScopedBearerAuthenticator

    dsn = os.environ.get("DISH_SECTION1_COMMAND_DSN", "").strip()
    if not dsn:
        print("command child is missing its PostgreSQL DSN", file=sys.stderr)
        return 64
    engine = create_engine(dsn, future=True, pool_pre_ping=True)
    factory = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )
    session = factory()
    try:
        service = PostgresProtocolService(
            PostgresCommandPort(
                session,
                cursor_secret=b"dish-section1-command-process-secret",
            ),
            ScopedBearerAuthenticator(
                action_token=action_token,
                private_token=private_token,
            ),
        )
        result = service.handle(
            command_name=command_name,
            authorization=f"Bearer {action_token}",
            body_loader=lambda: body,
            owner_id=owner_id,
            now=now,
            route_scope="action",
        )
        if scenario == "after_execution_before_commit":
            notify_process_barrier(
                "after_command_execution_before_commit",
                {
                    "result": result,
                    "result_sha256": hashlib.sha256(_canonical_bytes(result)).hexdigest(),
                },
            )
        session.commit()
        if scenario == "after_commit_before_response":
            notify_process_barrier(
                "after_authoritative_commit_before_response",
                {
                    "result": result,
                    "result_sha256": hashlib.sha256(_canonical_bytes(result)).hexdigest(),
                },
            )
        write_json_atomic(
            output,
            {
                "format": COMMAND_CHILD_RESULT_FORMAT,
                "status": "success",
                "result": result,
                "result_sha256": hashlib.sha256(_canonical_bytes(result)).hexdigest(),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return 0
    except Exception as exc:
        try:
            session.rollback()
        except Exception:
            pass
        write_json_atomic(
            output,
            {
                "format": COMMAND_CHILD_RESULT_FORMAT,
                "status": "error",
                "error_type": type(exc).__name__,
                "error": redact_evidence_text(str(exc)),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        print(f"{type(exc).__name__}: {redact_evidence_text(str(exc))}", file=sys.stderr)
        return 1
    finally:
        session.close()
        engine.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-process-failure")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--database-name", default=DEFAULT_DATABASE)
    parser.add_argument("--database-user", default="dish")
    parser.add_argument("--database-password", default="dish")
    parser.add_argument("--compose-project", default=f"dish-section1-{os.getpid()}")
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


def _unavailable_report_fields(
    *,
    infrastructure_error: str,
    cases: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current_cases = {} if cases is None else cases
    blocked_requirements = [
        NODE_REQUIREMENTS[nodeid]
        for nodeid in PROCESS_TEST_INVENTORY
        if nodeid not in current_cases
    ]
    return {
        "postgresql_identity": None,
        "requirements": _requirements_from_cases(
            current_cases, unavailable_reason=infrastructure_error
        ),
        "test_summary": _test_summary(current_cases),
        "scenario_evidence": [],
        "processes": [],
        "external_commands": [],
        "worker_external_commands": [],
        "evidence_validation": {
            "status": "not_run_blocked_by_unavailable_native_infrastructure",
            "ok": False,
            "errors": [infrastructure_error],
            "valid_scenario_count": 0,
            "valid_process_count": 0,
            "valid_external_command_count": 0,
            "expected_scenario_count": 0,
        },
        "native_execution_blocked_scenarios": blocked_requirements,
        "native_execution_blocker": infrastructure_error,
        "remaining_native_scenarios_blocked": bool(blocked_requirements),
        "process_failure_rehearsal_status": (
            "blocked_by_unavailable_native_infrastructure"
        ),
        "process_failure_native_evidence_validated": False,
        "worker_process_rehearsal_status": (
            "blocked_by_unavailable_native_infrastructure"
        ),
        "worker_process_native_evidence_validated": False,
        "section1_certified": False,
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.database_name = _safe_database_name(args.database_name)
        args.port = _safe_port(args.port)
        args.compose_project = _safe_project(args.compose_project)
        args.compose_timeout_seconds = _safe_timeout(
            args.compose_timeout_seconds, name="compose timeout"
        )
        args.pytest_timeout_seconds = _safe_timeout(
            args.pytest_timeout_seconds, name="pytest timeout"
        )
        args.cleanup_timeout_seconds = _safe_timeout(
            args.cleanup_timeout_seconds, name="cleanup timeout"
        )
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
    for key in list(env):
        if "ASANA" in key.upper():
            env.pop(key, None)

    compose_command, probe_records = _find_compose_command(
        cwd=ROOT,
        env=env,
        evidence=run_evidence,
        termination_grace_seconds=args.termination_grace_seconds,
    )
    commands: list[dict[str, Any]] = list(probe_records)
    report["commands"] = commands
    if compose_command is None:
        probe_failures = [_command_failure(item) for item in probe_records]
        detail = "Docker Compose is unavailable"
        if probe_failures:
            detail += "; probes failed: " + " | ".join(probe_failures)
        infrastructure_error = (
            detail
            + "; a dedicated native PostgreSQL process was not started, and SQLite/PGlite "
            "were not substituted"
        )
        report.update(
            {
                "status": "blocked",
                "ok": False,
                "infrastructure_error": infrastructure_error,
                **_unavailable_report_fields(
                    infrastructure_error=infrastructure_error
                ),
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

    compose_file = run_evidence / "compose.section1.yaml"
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
            "DISH_SECTION1_EXTERNAL_COMMAND_TIMEOUT_SECONDS": str(
                args.compose_timeout_seconds
            ),
            "DISH_SECTION1_TERMINATION_GRACE_SECONDS": str(
                args.termination_grace_seconds
            ),
            "PSYCOPG_IMPL": env.get("PSYCOPG_IMPL", "python"),
        }
    )
    identity: dict[str, Any] | None = None
    junit = run_evidence / "pytest-section1.xml"
    startup_ok = False
    return_code = 1
    cleanup_errors: list[str] = []
    try:
        up = run_external_command(
            [*compose, "up", "-d", "--wait", "--wait-timeout", "90"],
            cwd=ROOT,
            env=env,
            log_path=run_evidence / "postgresql-up-first.log",
            timeout_seconds=args.compose_timeout_seconds,
            termination_grace_seconds=args.termination_grace_seconds,
            label="postgresql-compose-up",
        )
        commands.append(up)
        if not _command_succeeded(up):
            infrastructure_error = (
                "dedicated PostgreSQL Compose startup failed: " + _command_failure(up)
            )
            report.update(
                {
                    "status": "blocked",
                    "ok": False,
                    "infrastructure_error": infrastructure_error,
                    **_unavailable_report_fields(
                        infrastructure_error=infrastructure_error
                    ),
                }
            )
            return_code = 3
        else:
            startup_ok = True
            identity = _probe_native(dsn)
            if identity["database"] != args.database_name or identity["server_port"] != 5432:
                raise RehearsalConfigurationError(
                    "connected PostgreSQL identity does not match the dedicated Compose target"
                )
            pytest_command = _section1_pytest_command(
                python=args.python,
                junit=junit,
            )
            pytest_run = run_external_command(
                pytest_command,
                cwd=ROOT,
                env=env,
                log_path=run_evidence / "pytest-section1-first.log",
                timeout_seconds=args.pytest_timeout_seconds,
                termination_grace_seconds=args.termination_grace_seconds,
                label="pytest-section1-process-rehearsal",
            )
            commands.append(pytest_run)
            orphan_cleanup_errors = _terminate_incomplete_process_groups(
                run_evidence / "processes",
                timeout_seconds=args.termination_grace_seconds,
            )
            cases, junit_errors = _parse_junit(junit)
            junit_errors.extend(orphan_cleanup_errors)
            scenario_records = _read_json_files(run_evidence / "scenarios")
            process_records = _read_json_files(run_evidence / "processes")
            worker_command_records = _read_json_files(run_evidence / "commands")
            evidence_validation = _validate_evidence(
                cases=cases,
                junit_errors=junit_errors,
                scenario_records=scenario_records,
                process_records=process_records,
                command_records=worker_command_records,
            )
            requirements = _requirements_from_cases(cases)
            summary = _test_summary(cases)
            process_failures = [
                item
                for item in requirements[: len(PROCESS_TEST_INVENTORY)]
                if item["status"] != "passed"
            ]
            process_ok = (
                _command_succeeded(pytest_run)
                and not process_failures
                and evidence_validation["ok"]
            )
            if process_ok:
                status, return_code = "pass", 0
                process_status = "passed"
            else:
                status, return_code = "fail", 1
                process_status = "failed"
            report.update(
                {
                    "status": status,
                    "ok": process_ok,
                    "section1_certified": process_ok,
                    "native_execution_blocked_scenarios": [],
                    "native_execution_blocker": None,
                    "remaining_native_scenarios_blocked": False,
                    "infrastructure_error": None,
                    "postgresql_identity": identity,
                    "requirements": requirements,
                    "test_summary": summary,
                    "scenario_evidence": scenario_records,
                    "processes": process_records,
                    "external_commands": worker_command_records,
                    "worker_external_commands": worker_command_records,
                    "evidence_validation": evidence_validation,
                    "process_failure_rehearsal_status": process_status,
                    "process_failure_native_evidence_validated": process_ok,
                    "worker_process_rehearsal_status": process_status,
                    "worker_process_native_evidence_validated": process_ok,
                    "pytest": {
                        "final_exit_status": pytest_run["final_exit_status"],
                        "completion_state": pytest_run["completion_state"],
                        "termination_state": pytest_run["termination_state"],
                        "timed_out": pytest_run["timed_out"],
                        "junit_path": str(junit),
                        "cases": cases,
                        "junit_errors": junit_errors,
                    },
                }
            )
    except (OSError, RehearsalConfigurationError, SQLAlchemyError, subprocess.SubprocessError) as exc:
        infrastructure_error = f"{type(exc).__name__}: {exc}"
        if not startup_ok:
            report.update(
                {
                    "status": "blocked",
                    "ok": False,
                    "infrastructure_error": infrastructure_error,
                    **_unavailable_report_fields(
                        infrastructure_error=infrastructure_error
                    ),
                }
            )
            return_code = 3
        else:
            report.update(
                {
                    "status": "fail",
                    "ok": False,
                    "section1_certified": False,
                    "infrastructure_error": infrastructure_error,
                    "postgresql_identity": identity,
                    "requirements": _requirements_from_cases({}),
                    "test_summary": _test_summary({}),
                    "scenario_evidence": [],
                    "processes": [],
                    "external_commands": [],
                    "worker_external_commands": [],
                    "evidence_validation": {
                        "status": "execution_failed_before_evidence_validation",
                        "ok": False,
                        "errors": [infrastructure_error],
                        "valid_scenario_count": 0,
                        "valid_process_count": 0,
                        "valid_external_command_count": 0,
                        "expected_scenario_count": 0,
                    },
                    "process_failure_rehearsal_status": "failed",
                    "process_failure_native_evidence_validated": False,
                    "worker_process_rehearsal_status": "failed",
                    "worker_process_native_evidence_validated": False,
                }
            )
            return_code = 1
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
        report["commands"] = commands
        report["cleanup_errors"] = cleanup_errors
        if cleanup_errors and report.get("status") in {"pass", "fail"}:
            report["status"] = "fail"
            report["process_failure_native_evidence_validated"] = False
            report["process_failure_rehearsal_status"] = "failed"
            report["worker_process_native_evidence_validated"] = False
            report["worker_process_rehearsal_status"] = "failed"
            report["section1_certified"] = False
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
    if len(sys.argv) > 1 and sys.argv[1] == COMMAND_CHILD_MODE:
        raise SystemExit(_command_child_main(sys.argv[2:]))
    raise SystemExit(main())
