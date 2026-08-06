"""Process and PostgreSQL synchronization support for §1 native rehearsals."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from sqlalchemy import create_engine, func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from dish_pg import models
from dish_pg import stage3_models as wf
from dish_pg import stage5_models as tx
from dish_pg.database import session_scope
from dish_pg.transition import ProjectionService
from dish_pg.process_failure_rehearsal import (
    COMMAND_CHILD_CONFIG_FORMAT,
    COMMAND_CHILD_RESULT_FORMAT,
    redact_command_for_evidence,
    redact_evidence_log,
    redact_evidence_text,
    run_external_command,
    write_json_atomic,
)

ROOT = Path(__file__).resolve().parents[3]
ADAPTER = "tests.support.postgresql.process_failure_adapter:DeterministicExternalAdapter"
FETCHER = "tests.support.postgresql.process_failure_adapter:fetch_corpus"
COMPARATOR = "tests.support.postgresql.process_failure_adapter:compare_item"
RECONCILIATION_CHILD_MODE = "_reconciliation-child"
RECONCILIATION_CHILD_CONFIG_FORMAT = "dish-section1-reconciliation-child-config-v1"
RECONCILIATION_CHILD_RESULT_FORMAT = "dish-section1-reconciliation-child-result-v1"
RECONCILIATION_CHILD_SCENARIOS = frozenset(
    {"normal", "after_durable_run_creation", "after_partial_corpus"}
)


class ProcessRehearsalFailure(AssertionError):
    """A process failed to reach or preserve the required boundary."""


@dataclass
class BarrierEvent:
    label: str
    payload: dict[str, Any]
    pid: int
    _connection: socket.socket

    def release(self) -> None:
        self._connection.sendall(
            json.dumps({"action": "continue", "label": self.label}, sort_keys=True).encode()
            + b"\n"
        )
        self._connection.close()

    def close(self) -> None:
        self._connection.close()


class BarrierServer:
    def __init__(self) -> None:
        self.path = Path(f"/tmp/dish-s1-{uuid.uuid4().hex}.sock")
        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

    def __enter__(self) -> "BarrierServer":
        self._server.bind(str(self.path))
        self._server.listen(1)
        return self

    def wait(self, expected: str, *, timeout: float = 30.0) -> BarrierEvent:
        self._server.settimeout(timeout)
        connection, _address = self._server.accept()
        connection.settimeout(timeout)
        received = b""
        while not received.endswith(b"\n"):
            chunk = connection.recv(4096)
            if not chunk:
                connection.close()
                raise ProcessRehearsalFailure("barrier connection closed before one complete event")
            received += chunk
        payload = json.loads(received.decode("utf-8"))
        if payload.get("label") != expected:
            connection.close()
            raise ProcessRehearsalFailure(
                f"expected barrier {expected!r}, received {payload.get('label')!r}"
            )
        return BarrierEvent(
            label=expected,
            payload=dict(payload.get("payload") or {}),
            pid=int(payload["pid"]),
            _connection=connection,
        )

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self._server.close()
        self.path.unlink(missing_ok=True)


@dataclass
class ChildProcess:
    process: subprocess.Popen[str]
    command: list[str]
    log_path: Path
    manifest_path: Path
    manifest: dict[str, Any]
    _log_handle: Any

    def _close_log(self) -> None:
        if self._log_handle.closed:
            return
        self._log_handle.flush()
        os.fsync(self._log_handle.fileno())
        self._log_handle.close()
        redact_evidence_log(self.log_path)

    def _record_final(
        self,
        *,
        final_exit_status: int,
        completion_state: str,
        termination_state: str,
        detail: str | None = None,
    ) -> None:
        self._close_log()
        self.manifest.update(
            {
                "completed_at": datetime.now(timezone.utc).isoformat(),
                "final_exit_status": final_exit_status,
                "completion_state": completion_state,
                "termination_state": termination_state,
                "detail": detail,
            }
        )
        write_json_atomic(self.manifest_path, self.manifest)

    def _signal_group(self, sig: signal.Signals) -> bool:
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            return False
        return True

    def wait(self, *, expected: int | None = 0, timeout: float = 30.0) -> int:
        if not (timeout > 0.0 and timeout < float("inf")):
            raise ValueError("child wait timeout must be finite and positive")
        try:
            returncode = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            termination_state = "sigterm" if self._signal_group(signal.SIGTERM) else "none"
            parent_exited = False
            try:
                returncode = self.process.wait(timeout=10.0)
                parent_exited = True
            except subprocess.TimeoutExpired:
                pass
            if self._signal_group(signal.SIGKILL):
                termination_state = "sigkill"
            if not parent_exited:
                try:
                    returncode = self.process.wait(timeout=10.0)
                except subprocess.TimeoutExpired as unreaped:
                    self._close_log()
                    self.manifest.update(
                        {
                            "completed_at": datetime.now(timezone.utc).isoformat(),
                            "final_exit_status": None,
                            "completion_state": "timed_out",
                            "termination_state": "sigkill_unreaped",
                            "detail": (
                                f"child exceeded {timeout} seconds and remained unreaped after "
                                "SIGTERM/SIGKILL"
                            ),
                        }
                    )
                    write_json_atomic(self.manifest_path, self.manifest)
                    raise ProcessRehearsalFailure(
                        f"child process group remained unreaped; log={self.log_path}"
                    ) from unreaped
            self._record_final(
                final_exit_status=returncode,
                completion_state="timed_out",
                termination_state=termination_state,
                detail=f"child exceeded finite timeout of {timeout} seconds",
            )
            raise ProcessRehearsalFailure(
                f"child did not exit within {timeout} seconds; process group terminated; "
                f"exit={returncode}; log={self.log_path}"
            ) from exc
        self._record_final(
            final_exit_status=returncode,
            completion_state="completed",
            termination_state="none",
        )
        if expected is not None and returncode != expected:
            raise ProcessRehearsalFailure(
                f"child exited {returncode}, expected {expected}; log={self.log_path}"
            )
        return returncode

    def kill(self, *, timeout: float = 10.0) -> int:
        if not (timeout > 0.0 and timeout < float("inf")):
            raise ValueError("child kill timeout must be finite and positive")
        termination_state = "none"
        if self.process.poll() is None and self._signal_group(signal.SIGKILL):
            termination_state = "sigkill"
        try:
            returncode = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            self._close_log()
            self.manifest.update(
                {
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "final_exit_status": None,
                    "completion_state": "timed_out",
                    "termination_state": "sigkill_unreaped",
                    "detail": f"explicit process-group kill remained unreaped for {timeout} seconds",
                }
            )
            write_json_atomic(self.manifest_path, self.manifest)
            raise ProcessRehearsalFailure(
                f"killed child process group remained unreaped; log={self.log_path}"
            ) from exc
        self._record_final(
            final_exit_status=returncode,
            completion_state="terminated" if termination_state != "none" else "completed",
            termination_state=termination_state,
            detail=(
                "intentional deterministic boundary termination"
                if termination_state != "none"
                else None
            ),
        )
        return returncode

    def terminate(self, *, timeout: float = 20.0) -> int:
        """Request bounded graceful process-group shutdown, then force if needed."""

        if not (timeout > 0.0 and timeout < float("inf")):
            raise ValueError("child terminate timeout must be finite and positive")
        termination_state = "none"
        if self.process.poll() is None and self._signal_group(signal.SIGTERM):
            termination_state = "sigterm"
        try:
            returncode = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if self._signal_group(signal.SIGKILL):
                termination_state = "sigkill"
            try:
                returncode = self.process.wait(timeout=10.0)
            except subprocess.TimeoutExpired as exc:
                self._close_log()
                self.manifest.update(
                    {
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "final_exit_status": None,
                        "completion_state": "timed_out",
                        "termination_state": "sigkill_unreaped",
                        "detail": "graceful shutdown remained unreaped after SIGTERM/SIGKILL",
                    }
                )
                write_json_atomic(self.manifest_path, self.manifest)
                raise ProcessRehearsalFailure(
                    f"terminated child process group remained unreaped; log={self.log_path}"
                ) from exc
        self._record_final(
            final_exit_status=returncode,
            completion_state="terminated" if termination_state != "none" else "completed",
            termination_state=termination_state,
            detail=(
                "bounded graceful shutdown requested"
                if termination_state == "sigterm"
                else "graceful shutdown escalated to SIGKILL"
                if termination_state == "sigkill"
                else None
            ),
        )
        return returncode


def _evidence_root(fallback: Path) -> Path:
    value = os.environ.get("DISH_SECTION1_EVIDENCE_DIR")
    root = Path(value) if value else fallback
    root.mkdir(parents=True, exist_ok=True)
    return root


def _child_environment(
    *,
    barrier: BarrierServer | None,
    ledger: Path,
    scenario: str,
    overrides: dict[str, str] | None = None,
) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("DISH_") or "ASANA" in key.upper():
            env.pop(key, None)
    pythonpath = [str(ROOT)]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(pythonpath),
            "DISH_PROFILE": "test",
            "DISH_SECTION1_EXTERNAL_LEDGER": str(ledger),
            "DISH_SECTION1_SCENARIO": scenario,
            "PSYCOPG_IMPL": env.get("PSYCOPG_IMPL", "python"),
        }
    )
    if barrier is not None:
        env["DISH_SECTION1_BARRIER_SOCKET"] = str(barrier.path)
    else:
        env.pop("DISH_SECTION1_BARRIER_SOCKET", None)
    env.update(overrides or {})
    return env


def _start_child(
    command: list[str],
    *,
    tmp_path: Path,
    barrier: BarrierServer | None,
    ledger: Path,
    scenario: str,
    label: str,
    env_overrides: dict[str, str] | None = None,
) -> ChildProcess:
    evidence = _evidence_root(tmp_path)
    logs = evidence / "process-logs"
    manifests = evidence / "processes"
    logs.mkdir(parents=True, exist_ok=True)
    manifests.mkdir(parents=True, exist_ok=True)
    identity = uuid.uuid4().hex
    log_path = logs / f"{label}-{identity}.log"
    log_handle = log_path.open("w", encoding="utf-8")
    os.chmod(log_path, 0o600)
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        env=_child_environment(
            barrier=barrier,
            ledger=ledger,
            scenario=scenario,
            overrides=env_overrides,
        ),
        text=True,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    current_test = os.environ.get("PYTEST_CURRENT_TEST", "").split(" ", 1)[0]
    manifest = {
        "format": "dish-section1-process-record-v2",
        "process_id": identity,
        "nodeid": current_test,
        "label": label,
        "pid": process.pid,
        "process_group_id": process.pid,
        "command": redact_command_for_evidence(command),
        "scenario": scenario,
        "log_path": str(log_path),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "final_exit_status": None,
        "completion_state": "running",
        "termination_state": "none",
        "detail": None,
    }
    manifest_path = manifests / f"{label}-{identity}.json"
    write_json_atomic(manifest_path, manifest)
    return ChildProcess(process, command, log_path, manifest_path, manifest, log_handle)


def start_postgresql_proxy(
    *,
    dsn: str,
    tmp_path: Path,
    listen_port: int,
    label: str,
) -> tuple[ChildProcess, str]:
    """Start a loopback TCP proxy and return its process plus proxied DSN."""

    url = make_url(dsn)
    target_host = url.host or "127.0.0.1"
    target_port = int(url.port or 5432)
    ready_file = tmp_path / f"{label}-ready.json"
    command = [
        sys.executable,
        "-m",
        "tests.support.postgresql.tcp_proxy",
        "--listen-host",
        "127.0.0.1",
        "--listen-port",
        str(listen_port),
        "--target-host",
        target_host,
        "--target-port",
        str(target_port),
        "--ready-file",
        str(ready_file),
    ]
    child = _start_child(
        command,
        tmp_path=tmp_path,
        barrier=None,
        ledger=tmp_path / "unused-proxy-ledger.json",
        scenario="postgresql-tcp-proxy",
        label=label,
    )
    for _attempt in range(1000):
        if ready_file.is_file():
            payload = json.loads(ready_file.read_text(encoding="utf-8"))
            if payload.get("pid") == child.process.pid:
                proxied = url.set(host="127.0.0.1", port=listen_port)
                return child, proxied.render_as_string(hide_password=False)
        try:
            returncode = child.process.wait(timeout=0.02)
        except subprocess.TimeoutExpired:
            continue
        child.wait(expected=None)
        raise ProcessRehearsalFailure(
            f"PostgreSQL TCP proxy exited before readiness; exit={returncode}; "
            f"log={child.log_path}"
        )
    child.terminate()
    raise ProcessRehearsalFailure(
        f"PostgreSQL TCP proxy did not become ready; log={child.log_path}"
    )


def start_postgresql_service(
    *,
    dsn: str,
    tmp_path: Path,
    expected_database: str,
    expected_schema_head: str,
    expected_release: str,
    expected_generation_id: uuid.UUID,
    private_port: int,
    action_port: int,
    agent_token: str,
    admin_token: str,
    action_token: str,
) -> ChildProcess:
    """Start the established ``dish-service`` entry point in TEST PostgreSQL mode."""

    state_dir = tmp_path / "service-state"
    command = [
        sys.executable,
        str(ROOT / "dish-service"),
        "--postgresql-test-runtime",
        "--database-url",
        dsn,
        "--expected-database",
        expected_database,
        "--expected-schema-head",
        expected_schema_head,
        "--expected-release",
        expected_release,
        "--expected-generation-id",
        str(expected_generation_id),
        "--cursor-secret",
        "runtime-wiring-cursor-secret-32-bytes",
        "--state-dir",
        str(state_dir),
    ]
    return _start_child(
        command,
        tmp_path=tmp_path,
        barrier=None,
        ledger=tmp_path / "unused-service-ledger.json",
        scenario="postgresql-runtime-service",
        label="postgresql-service",
        env_overrides={
            "DISH_PROFILE": "test",
            "DISH_SERVICE_BIND": "127.0.0.1",
            "DISH_SERVICE_PORT": str(private_port),
            "DISH_ACTION_BIND": "127.0.0.1",
            "DISH_ACTION_PORT": str(action_port),
            "DISH_SERVICE_AGENT_TOKEN": agent_token,
            "DISH_SERVICE_ADMIN_TOKEN": admin_token,
            "DISH_SERVICE_ACTION_TOKEN": action_token,
        },
    )


def start_projection_worker(
    *,
    dsn: str,
    tmp_path: Path,
    ledger: Path,
    worker_id: str,
    scenario: str = "normal",
    barrier: BarrierServer | None = None,
    once: bool = True,
    claim_ttl_seconds: int = 120,
    expected_database: str | None = None,
    expected_schema_head: str | None = None,
    expected_release: str | None = None,
    expected_generation_id: uuid.UUID | None = None,
    identity_output: Path | None = None,
    process_label: str | None = None,
) -> ChildProcess:
    command = [
        sys.executable,
        "-m",
        "dish_pg.projection_worker",
        "--database-url",
        dsn,
        "--worker-id",
        worker_id,
        "--adapter",
        ADAPTER,
        "--claim-ttl-seconds",
        str(claim_ttl_seconds),
        "--idle-seconds",
        "3600",
        "--log-level",
        "INFO",
    ]
    if once:
        command.append("--once")
    identity_values = (
        expected_database,
        expected_schema_head,
        expected_release,
        expected_generation_id,
        identity_output,
    )
    if any(value is not None for value in identity_values):
        if not all(value is not None for value in identity_values):
            raise ValueError("projection worker runtime identity arguments must be supplied together")
        command.extend(
            [
                "--expected-database",
                str(expected_database),
                "--expected-schema-head",
                str(expected_schema_head),
                "--expected-release",
                str(expected_release),
                "--expected-generation-id",
                str(expected_generation_id),
                "--runtime-identity-output",
                str(identity_output),
            ]
        )
    return _start_child(
        command,
        tmp_path=tmp_path,
        barrier=barrier,
        ledger=ledger,
        scenario=scenario,
        label=process_label or f"projection-{worker_id}",
    )


def start_command_process(
    *,
    dsn: str,
    tmp_path: Path,
    run_id: uuid.UUID,
    request_id: uuid.UUID,
    output: Path,
    now: datetime,
    arguments: dict[str, Any],
    scenario: str = "normal",
    barrier: BarrierServer | None = None,
    command_name: str = "create",
    owner_id: str = "owner-1",
) -> ChildProcess:
    evidence = _evidence_root(tmp_path)
    configs = evidence / "command-configs"
    configs.mkdir(parents=True, exist_ok=True)
    identity = uuid.uuid4().hex
    config_path = configs / f"command-{identity}.json"
    write_json_atomic(
        config_path,
        {
            "format": COMMAND_CHILD_CONFIG_FORMAT,
            "output": str(output),
            "scenario": scenario,
            "command_name": command_name,
            "owner_id": owner_id,
            "action_token": "section1-action-token",
            "private_token": "section1-private-token",
            "now": now.isoformat(),
            "body": {
                "client": {"run_id": str(run_id), "request_id": str(request_id)},
                "arguments": dict(arguments),
            },
        },
    )
    command = [
        sys.executable,
        "-m",
        "dish_pg.process_failure_rehearsal",
        "_command-child",
        str(config_path),
    ]
    return _start_child(
        command,
        tmp_path=tmp_path,
        barrier=barrier,
        ledger=tmp_path / "command-unused-ledger.json",
        scenario=scenario,
        label=f"command-{command_name}",
        env_overrides={"DISH_SECTION1_COMMAND_DSN": dsn},
    )


def read_command_result(path: Path, *, expected_status: str = "success") -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") != COMMAND_CHILD_RESULT_FORMAT:
        raise ProcessRehearsalFailure(f"command result has invalid format: {path}")
    if payload.get("status") != expected_status:
        raise ProcessRehearsalFailure(
            f"command result status is {payload.get('status')!r}, expected {expected_status!r}: {path}"
        )
    return payload


def command_snapshot(factory, *, request_id: uuid.UUID) -> dict[str, Any]:
    with session_scope(factory) as session:
        request_count = int(
            session.scalar(
                select(func.count()).select_from(wf.ServiceRequest).where(
                    wf.ServiceRequest.request_id == request_id
                )
            )
            or 0
        )
        outcome = session.scalar(
            select(wf.ServiceRequestOutcome).where(
                wf.ServiceRequestOutcome.request_id == request_id
            )
        )
        executions = list(
            session.scalars(
                select(wf.CommandExecution).where(
                    wf.CommandExecution.request_id == request_id
                )
            )
        )
        execution_ids = [row.execution_id for row in executions]
        if execution_ids:
            tasks = list(
                session.scalars(
                    select(models.DishTask).where(
                        models.DishTask.command_execution_id.in_(execution_ids)
                    )
                )
            )
            task_ids = [row.task_id for row in tasks]
            table_counts = {
                "content_versions": int(
                    session.scalar(
                        select(func.count()).select_from(models.ContentVersion).where(
                            models.ContentVersion.command_execution_id.in_(execution_ids)
                        )
                    )
                    or 0
                ),
                "membership_events": int(
                    session.scalar(
                        select(func.count()).select_from(models.TaskProjectMembershipEvent).where(
                            models.TaskProjectMembershipEvent.command_execution_id.in_(execution_ids)
                        )
                    )
                    or 0
                ),
                "placement_events": int(
                    session.scalar(
                        select(func.count()).select_from(models.TaskSectionPlacementEvent).where(
                            models.TaskSectionPlacementEvent.command_execution_id.in_(execution_ids)
                        )
                    )
                    or 0
                ),
                "completion_events": int(
                    session.scalar(
                        select(func.count()).select_from(models.TaskCompletionEvent).where(
                            models.TaskCompletionEvent.command_execution_id.in_(execution_ids)
                        )
                    )
                    or 0
                ),
                "projection_events": int(
                    session.scalar(
                        select(func.count()).select_from(tx.ProjectionOutboxEvent).where(
                            tx.ProjectionOutboxEvent.command_execution_id.in_(execution_ids)
                        )
                    )
                    or 0
                ),
            }
        else:
            tasks = []
            task_ids = []
            table_counts = {
                "content_versions": 0,
                "membership_events": 0,
                "placement_events": 0,
                "completion_events": 0,
                "projection_events": 0,
            }
    return {
        "request_count": request_count,
        "outcome_count": 0 if outcome is None else 1,
        "execution_count": len(executions),
        "execution_ids": [str(row.execution_id) for row in executions],
        "execution_statuses": [row.status for row in executions],
        "task_count": len(tasks),
        "task_ids": [str(value) for value in task_ids],
        **table_counts,
        "outcome": (
            None
            if outcome is None
            else {
                "class": outcome.outcome_class,
                "code": outcome.result_code,
                "http_status": outcome.http_status,
                "payload": dict(outcome.result_payload),
                "sha256": outcome.result_sha256,
            }
        ),
    }


def start_reconciliation_worker(
    *,
    dsn: str,
    tmp_path: Path,
    ledger: Path,
    generation_id: uuid.UUID,
    corpus_identity: str,
    output: Path,
    scenario: str = "normal",
    barrier: BarrierServer | None = None,
    item_count: int = 1,
    expected_database: str | None = None,
    expected_schema_head: str | None = None,
    expected_release: str | None = None,
    identity_output: Path | None = None,
) -> ChildProcess:
    command = [
        sys.executable,
        "-m",
        "dish_pg.reconciliation_worker",
        "--database-url",
        dsn,
        "--generation-id",
        str(generation_id),
        "--corpus-identity",
        corpus_identity,
        "--fetcher",
        FETCHER,
        "--comparator",
        COMPARATOR,
        "--output",
        str(output),
        "--log-level",
        "INFO",
    ]
    if item_count < 1:
        raise ValueError("reconciliation item_count must be positive")
    identity_values = (
        expected_database,
        expected_schema_head,
        expected_release,
        identity_output,
    )
    if any(value is not None for value in identity_values):
        if not all(value is not None for value in identity_values):
            raise ValueError(
                "reconciliation worker runtime identity arguments must be supplied together"
            )
        command.extend(
            [
                "--expected-database",
                str(expected_database),
                "--expected-schema-head",
                str(expected_schema_head),
                "--expected-release",
                str(expected_release),
                "--expected-generation-id",
                str(generation_id),
                "--runtime-identity-output",
                str(identity_output),
            ]
        )
    return _start_child(
        command,
        tmp_path=tmp_path,
        barrier=barrier,
        ledger=ledger,
        scenario=scenario,
        label="reconciliation",
        env_overrides={"DISH_SECTION1_RECONCILIATION_ITEM_COUNT": str(item_count)},
    )


def start_reconciliation_checkpoint_process(
    *,
    dsn: str,
    tmp_path: Path,
    ledger: Path,
    generation_id: uuid.UUID,
    corpus_identity: str,
    output: Path,
    item_count: int,
    mode: str,
    scenario: str = "normal",
    barrier: BarrierServer | None = None,
    reconciliation_run_id: uuid.UUID | None = None,
    now: datetime | None = None,
) -> ChildProcess:
    """Start the existing §1 child harness at durable reconciliation checkpoints."""

    if item_count < 1:
        raise ValueError("reconciliation item_count must be positive")
    if not corpus_identity.strip():
        raise ValueError("reconciliation corpus_identity must be non-blank")
    if mode not in {"start", "resume"}:
        raise ValueError("reconciliation checkpoint mode must be start or resume")
    if mode == "resume" and reconciliation_run_id is None:
        raise ValueError("resume mode requires reconciliation_run_id")
    if mode == "start" and reconciliation_run_id is not None:
        raise ValueError("start mode must not provide reconciliation_run_id")
    if scenario not in RECONCILIATION_CHILD_SCENARIOS:
        raise ValueError(f"unsupported reconciliation child scenario {scenario!r}")
    evidence = _evidence_root(tmp_path)
    configs = evidence / "reconciliation-configs"
    configs.mkdir(parents=True, exist_ok=True)
    identity = uuid.uuid4().hex
    config_path = configs / f"reconciliation-{identity}.json"
    write_json_atomic(
        config_path,
        {
            "format": RECONCILIATION_CHILD_CONFIG_FORMAT,
            "output": str(output),
            "mode": mode,
            "scenario": scenario,
            "generation_id": str(generation_id),
            "corpus_identity": corpus_identity,
            "item_count": item_count,
            "reconciliation_run_id": (
                None if reconciliation_run_id is None else str(reconciliation_run_id)
            ),
            "now": (now or datetime.now(timezone.utc)).isoformat(),
        },
    )
    command = [
        sys.executable,
        "-m",
        "tests.support.postgresql.process_failure",
        RECONCILIATION_CHILD_MODE,
        str(config_path),
    ]
    return _start_child(
        command,
        tmp_path=tmp_path,
        barrier=barrier,
        ledger=ledger,
        scenario=scenario,
        label=f"reconciliation-checkpoint-{mode}",
        env_overrides={
            "DISH_SECTION1_RECONCILIATION_DSN": dsn,
            "DISH_SECTION1_RECONCILIATION_ITEM_COUNT": str(item_count),
        },
    )


def read_reconciliation_child_result(
    path: Path, *, expected_status: str = "success"
) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("format") != RECONCILIATION_CHILD_RESULT_FORMAT:
        raise ProcessRehearsalFailure(f"reconciliation child result has invalid format: {path}")
    if payload.get("status") != expected_status:
        raise ProcessRehearsalFailure(
            f"reconciliation child status is {payload.get('status')!r}, "
            f"expected {expected_status!r}: {path}"
        )
    return payload


def reconciliation_snapshot(
    factory,
    *,
    generation_id: uuid.UUID,
    corpus_identity: str,
) -> dict[str, Any]:
    with session_scope(factory) as session:
        runs = list(
            session.scalars(
                select(tx.ProjectionReconciliationRun)
                .where(
                    tx.ProjectionReconciliationRun.generation_id == generation_id,
                    tx.ProjectionReconciliationRun.corpus_identity == corpus_identity,
                )
                .order_by(tx.ProjectionReconciliationRun.started_at)
            )
        )
        run_ids = [row.reconciliation_run_id for row in runs]
        items = (
            []
            if not run_ids
            else list(
                session.scalars(
                    select(tx.ProjectionReconciliationItem)
                    .where(
                        tx.ProjectionReconciliationItem.reconciliation_run_id.in_(run_ids)
                    )
                    .order_by(
                        tx.ProjectionReconciliationItem.reconciliation_run_id,
                        tx.ProjectionReconciliationItem.item_identity,
                    )
                )
            )
        )
    items_by_run: dict[uuid.UUID, list[tx.ProjectionReconciliationItem]] = {
        run_id: [] for run_id in run_ids
    }
    for item in items:
        items_by_run[item.reconciliation_run_id].append(item)
    return {
        "run_count": len(runs),
        "item_count": len(items),
        "runs": [
            {
                "reconciliation_run_id": str(row.reconciliation_run_id),
                "status": row.status,
                "expected_items": int(row.expected_items),
                "processed_items": int(row.processed_items),
                "completed": row.completed_at is not None,
                "item_identities": [
                    item.item_identity for item in items_by_run[row.reconciliation_run_id]
                ],
                "item_outcomes": [
                    item.outcome for item in items_by_run[row.reconciliation_run_id]
                ],
            }
            for row in runs
        ],
    }


def read_ledger(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"dispatch_calls": 0, "recovery_observations": 0, "effects": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def expire_claim(factory, event_id: uuid.UUID) -> None:
    with session_scope(factory) as session:
        result = session.execute(
            update(tx.ProjectionOutboxEvent)
            .where(
                tx.ProjectionOutboxEvent.projection_event_id == event_id,
                tx.ProjectionOutboxEvent.state == "claimed",
            )
            .values(claim_expires_at=text("clock_timestamp() - interval '1 second'"))
        )
        if result.rowcount != 1:
            raise ProcessRehearsalFailure("exact claimed event was not eligible for lease advancement")


def event_snapshot(factory, event_ids: list[uuid.UUID]) -> dict[str, Any]:
    with session_scope(factory) as session:
        event_rows = list(
            session.scalars(
                select(tx.ProjectionOutboxEvent).where(
                    tx.ProjectionOutboxEvent.projection_event_id.in_(event_ids)
                )
            )
        )
        event_by_id = {row.projection_event_id: row for row in event_rows}
        events = [event_by_id[event_id] for event_id in event_ids]
        attempts = list(
            session.scalars(
                select(tx.ProjectionAttempt)
                .where(tx.ProjectionAttempt.projection_event_id.in_(event_ids))
                .order_by(tx.ProjectionAttempt.projection_event_id, tx.ProjectionAttempt.attempt_number)
            )
        )
        attempt_ids = [row.attempt_id for row in attempts]
        observations = []
        adjudications = []
        if attempt_ids:
            observations = list(
                session.scalars(
                    select(tx.ProjectionObservation)
                    .where(tx.ProjectionObservation.attempt_id.in_(attempt_ids))
                    .order_by(
                        tx.ProjectionObservation.attempt_id,
                        tx.ProjectionObservation.observation_sequence,
                    )
                )
            )
            adjudications = list(
                session.scalars(
                    select(tx.ProjectionAdjudication)
                    .where(tx.ProjectionAdjudication.attempt_id.in_(attempt_ids))
                    .order_by(
                        tx.ProjectionAdjudication.attempt_id,
                        tx.ProjectionAdjudication.adjudication_sequence,
                    )
                )
            )
        correlations = list(
            session.scalars(
                select(tx.ProjectionCreateCorrelation)
                .where(tx.ProjectionCreateCorrelation.projection_event_id.in_(event_ids))
                .order_by(tx.ProjectionCreateCorrelation.projection_event_id)
            )
        )
    return {
        "events": [
            {
                "event_id": str(row.projection_event_id),
                "state": row.state,
                "claim_owner": row.claim_owner,
                "claim_token": None if row.claim_token is None else str(row.claim_token),
                "claim_revision": row.outbox_revision,
                "aggregate_sequence": row.aggregate_sequence,
                "terminal": row.terminal_at is not None,
            }
            for row in events
        ],
        "attempts": [
            {
                "attempt_id": str(row.attempt_id),
                "event_id": str(row.projection_event_id),
                "number": row.attempt_number,
                "kind": row.attempt_kind,
                "state": row.state,
                "worker_id": row.worker_id,
                "dispatch_identity": row.dispatch_identity,
                "request_identity": row.request_identity,
                "intended_external_id": row.intended_external_id,
                "predecessor_attempt_id": (
                    None if row.predecessor_attempt_id is None else str(row.predecessor_attempt_id)
                ),
                "terminal": row.terminal_at is not None,
            }
            for row in attempts
        ],
        "observations": [
            {
                "observation_id": str(row.observation_id),
                "attempt_id": str(row.attempt_id),
                "sequence": row.observation_sequence,
                "kind": row.observation_kind,
                "observed_applied": row.observed_applied,
                "observed_identity": row.observed_identity,
                "reread_complete": row.reread_complete,
                "evidence": dict(row.evidence),
            }
            for row in observations
        ],
        "adjudications": [
            {
                "adjudication_id": str(row.adjudication_id),
                "attempt_id": str(row.attempt_id),
                "observation_id": str(row.observation_id),
                "sequence": row.adjudication_sequence,
                "outcome": row.outcome,
                "decided_by": row.decided_by,
                "decision_reason": row.decision_reason,
            }
            for row in adjudications
        ],
        "correlations": [
            {
                "event_id": str(row.projection_event_id),
                "marker": row.marker,
                "state": row.state,
                "matched_external_id": row.matched_external_id,
                "match_count": row.match_count,
            }
            for row in correlations
        ],
        "observation_count": len(observations),
        "adjudication_count": len(adjudications),
    }


def write_scenario(
    name: str,
    payload: dict[str, Any],
    *,
    nodeid: str,
    tmp_path: Path,
) -> Path:
    directory = _evidence_root(tmp_path) / "scenarios"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    write_json_atomic(
        path,
        {
            "format": "dish-section1-scenario-evidence-v2",
            "nodeid": nodeid,
            "scenario": name,
            "completion_state": "scenario_assertions_completed",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "evidence": payload,
        },
    )
    return path


def _psycopg_conninfo(dsn: str) -> str:
    url = make_url(dsn).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


class SettlementNotification:
    def __init__(self, *, dsn: str, event_id: uuid.UUID) -> None:
        suffix = uuid.uuid4().hex
        self.channel = f"dish_s1_{suffix}"
        self.function = f"dish_s1_notify_{suffix}"
        self.trigger = f"dish_s1_trigger_{suffix}"
        self.event_id = event_id
        self._dsn = dsn
        engine = create_engine(dsn, future=True)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"""
                    CREATE FUNCTION {self.function}() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN
                      IF NEW.projection_event_id = '{event_id}'::uuid
                         AND NEW.state IN ('applied', 'uncertain', 'blocked', 'superseded')
                         AND OLD.state IS DISTINCT FROM NEW.state THEN
                        PERFORM pg_notify('{self.channel}', NEW.projection_event_id::text);
                      END IF;
                      RETURN NEW;
                    END;
                    $$
                    """
                )
                connection.exec_driver_sql(
                    f"CREATE TRIGGER {self.trigger} AFTER UPDATE ON projection_outbox_events "
                    f"FOR EACH ROW EXECUTE FUNCTION {self.function}()"
                )
        finally:
            engine.dispose()
        self.connection = psycopg.connect(_psycopg_conninfo(dsn), autocommit=True)
        self.connection.execute(f"LISTEN {self.channel}")

    def wait(self, *, timeout: float = 30.0) -> None:
        notification = next(self.connection.notifies(timeout=timeout, stop_after=1), None)
        if notification is None or notification.payload != str(self.event_id):
            raise ProcessRehearsalFailure("settlement commit notification was not observed")

    def close(self) -> None:
        self.connection.close()
        engine = create_engine(self._dsn, future=True)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    f"DROP TRIGGER IF EXISTS {self.trigger} ON projection_outbox_events"
                )
                connection.exec_driver_sql(f"DROP FUNCTION IF EXISTS {self.function}()")
        finally:
            engine.dispose()


def compose_control(action: str, *, timeout: float | None = None) -> dict[str, Any]:
    raw = os.environ.get("DISH_SECTION1_COMPOSE_JSON")
    if not raw:
        raise ProcessRehearsalFailure("rehearsal compose control is unavailable")
    compose = json.loads(raw)
    if action == "stop":
        command = [*compose, "stop", "--timeout", "1", "postgres"]
    elif action == "start":
        command = [*compose, "up", "-d", "--wait", "--wait-timeout", "60", "postgres"]
    else:
        raise ValueError(f"unknown compose action {action!r}")
    configured_timeout = (
        float(os.environ.get("DISH_SECTION1_EXTERNAL_COMMAND_TIMEOUT_SECONDS", "90"))
        if timeout is None
        else timeout
    )
    grace = float(os.environ.get("DISH_SECTION1_TERMINATION_GRACE_SECONDS", "5"))
    evidence = _evidence_root(Path.cwd())
    command_id = uuid.uuid4().hex
    result = run_external_command(
        command,
        cwd=ROOT,
        env=os.environ.copy(),
        log_path=evidence / "compose-control" / f"compose-{action}-{command_id}.log",
        timeout_seconds=configured_timeout,
        termination_grace_seconds=grace,
        label=f"compose-{action}",
        record_path=evidence / "commands" / f"compose-{action}-{command_id}.json",
    )
    if (
        result["completion_state"] != "completed"
        or result["final_exit_status"] != 0
        or result["timed_out"]
    ):
        raise ProcessRehearsalFailure(
            f"compose {action} failed: state={result['completion_state']} "
            f"exit={result['final_exit_status']} timed_out={result['timed_out']} "
            f"failure={result['failure']} log={result['log_path']}"
        )
    return result

def _reconciliation_child_main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("reconciliation child requires exactly one configuration path", file=sys.stderr)
        return 64
    config_path = Path(argv[0]).expanduser().resolve(strict=True)
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"reconciliation child configuration is unreadable: {exc}", file=sys.stderr)
        return 64
    if not isinstance(config, dict) or config.get("format") != RECONCILIATION_CHILD_CONFIG_FORMAT:
        print("reconciliation child configuration has an invalid format", file=sys.stderr)
        return 64
    try:
        output = Path(str(config["output"])).expanduser().resolve(strict=False)
        mode = str(config["mode"])
        scenario = str(config["scenario"])
        generation_id = uuid.UUID(str(config["generation_id"]))
        corpus_identity = str(config["corpus_identity"])
        item_count = int(config["item_count"])
        raw_run_id = config.get("reconciliation_run_id")
        reconciliation_run_id = None if raw_run_id is None else uuid.UUID(str(raw_run_id))
        now = datetime.fromisoformat(str(config["now"]))
    except (KeyError, TypeError, ValueError) as exc:
        print(f"reconciliation child configuration is incomplete: {exc}", file=sys.stderr)
        return 64
    if mode not in {"start", "resume"}:
        print(f"reconciliation child mode is unsupported: {mode!r}", file=sys.stderr)
        return 64
    if scenario not in RECONCILIATION_CHILD_SCENARIOS:
        print(f"reconciliation child scenario is unsupported: {scenario!r}", file=sys.stderr)
        return 64
    if (
        item_count < 1
        or not corpus_identity.strip()
        or (mode == "resume" and reconciliation_run_id is None)
        or (mode == "start" and reconciliation_run_id is not None)
    ):
        print("reconciliation child configuration has invalid progress fields", file=sys.stderr)
        return 64
    dsn = os.environ.get("DISH_SECTION1_RECONCILIATION_DSN", "").strip()
    if not dsn:
        print("reconciliation child is missing its PostgreSQL DSN", file=sys.stderr)
        return 64

    from tests.support.postgresql.process_failure_adapter import compare_item, fetch_corpus

    engine = create_engine(dsn, future=True, pool_pre_ping=True)
    factory = sessionmaker(
        bind=engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
        future=True,
    )
    try:
        corpus = tuple(fetch_corpus(corpus_identity))
        if len(corpus) != item_count:
            raise ProcessRehearsalFailure(
                f"configured item_count {item_count} does not match fetched corpus {len(corpus)}"
            )
        if mode == "start":
            with session_scope(factory) as session:
                run = ProjectionService(session).start_reconciliation(
                    generation_id=generation_id,
                    corpus_identity=corpus_identity,
                    expected_items=len(corpus),
                    started_at=now,
                )
                reconciliation_run_id = run.reconciliation_run_id
            if scenario == "after_durable_run_creation":
                from dish_pg.process_failure_rehearsal import notify_process_barrier

                notify_process_barrier(
                    "after_durable_reconciliation_run_creation",
                    {
                        "reconciliation_run_id": str(reconciliation_run_id),
                        "expected_items": len(corpus),
                        "processed_items": 0,
                    },
                )
        if reconciliation_run_id is None:
            raise ProcessRehearsalFailure("reconciliation run identity was not established")
        for index, item in enumerate(corpus):
            with session_scope(factory) as session:
                record = compare_item(session, generation_id, item)
                ProjectionService(session).record_reconciliation_item(
                    reconciliation_run_id=reconciliation_run_id,
                    item_identity=record.item_identity,
                    entity_kind=record.entity_kind,
                    mapping_id=record.mapping_id,
                    outcome=record.outcome,
                    evidence=record.evidence,
                    recorded_at=now,
                )
            if scenario == "after_partial_corpus" and index == 0:
                from dish_pg.process_failure_rehearsal import notify_process_barrier

                notify_process_barrier(
                    "after_partially_recorded_reconciliation_corpus",
                    {
                        "reconciliation_run_id": str(reconciliation_run_id),
                        "expected_items": len(corpus),
                        "processed_items": 1,
                        "recorded_item_identity": item.item_identity,
                    },
                )
        with session_scope(factory) as session:
            completed = ProjectionService(session).complete_reconciliation(
                reconciliation_run_id=reconciliation_run_id,
                completed_at=now,
            )
            result = {
                "format": RECONCILIATION_CHILD_RESULT_FORMAT,
                "status": "success",
                "mode": mode,
                "scenario": scenario,
                "reconciliation_run_id": str(completed.reconciliation_run_id),
                "run_status": completed.status,
                "expected_items": int(completed.expected_items),
                "processed_items": int(completed.processed_items),
            }
        write_json_atomic(output, result)
        return 0
    except Exception as exc:
        write_json_atomic(
            output,
            {
                "format": RECONCILIATION_CHILD_RESULT_FORMAT,
                "status": "error",
                "error_type": type(exc).__name__,
                "error": redact_evidence_text(str(exc)),
            },
        )
        print(
            f"{type(exc).__name__}: {redact_evidence_text(str(exc))}",
            file=sys.stderr,
        )
        return 1
    finally:
        engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == RECONCILIATION_CHILD_MODE:
        raise SystemExit(_reconciliation_child_main(sys.argv[2:]))
    raise SystemExit("unsupported process-failure support invocation")
