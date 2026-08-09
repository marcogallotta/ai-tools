"""Disposable native-PostgreSQL backup, restore, and PITR rehearsal.

This command owns every cluster, port, database, archive, target, and evidence
path it touches.  It never accepts a service DSN.  A successful exit means the
physical backup was independently verified, an independent restore and two PITR
targets were inspected, restored authority was rotated through external control,
and every declared fault failed closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from sqlalchemy import Engine, create_engine, select, text
from sqlalchemy.orm import Session

from . import import_link_models
from . import models
from . import stage3_models as wf
from . import stage5_models as transition_models
from . import stage6_models as release_models
from .bootstrap import (
    HonestCheckout,
    InitialBootstrapSpec,
    SectionSpec,
    SourceBundle,
    bootstrap_initial_generation,
)
from .database import session_factory, session_scope
from .recovery_control import (
    RecoveredPhysicalState,
    RestoreControl,
    RestoreControlError,
    load_restore_control,
    migration_revision_sha256,
    promote_restored_generation,
)
from .release import ALEMBIC_HEAD, ReleaseCandidateService
from .release_evidence import (
    EVIDENCE_ARTIFACT_KINDS,
    REHEARSAL_CHECKPOINT_EVIDENCE_KINDS,
    REQUIRED_EVIDENCE,
    REQUIRED_REHEARSAL_CHECKPOINTS,
    REQUIRED_REHEARSALS,
    sha256_json as release_sha256_json,
)
from .release_validation import active_mapping_membership, reconciliation_corpus_sha256
from .services import CoreAuthorityService, ImportedTaskSpec
from .transition import ProjectionService, SourceImportService
from .workflow import (
    ExecutionSpec,
    MutationAdmissionClosed,
    RequestSpec,
    StaleAuthorityError,
    StoredOutcome,
    WorkflowAuthorityService,
    sha256_json,
)

ROOT = Path(__file__).resolve().parents[1]
REPORT_SCHEMA = "dish-postgresql-recovery-rehearsal-v1"
RESOURCE_PREFIX = "dish-section2-"
REPRESENTATIVE_CONTENT_IDENTITY = hashlib.sha256(b"section2-content").hexdigest()
DEFAULT_PORT_BASE = 56520
PG_REQUIRED = (
    "initdb",
    "pg_ctl",
    "postgres",
    "createdb",
    "pg_basebackup",
    "pg_verifybackup",
    "pg_controldata",
    "psql",
)
SAFE_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")

COMMAND_TIMEOUTS = {
    "git": 10.0,
    "version": 10.0,
    "initdb": 60.0,
    "pg_ctl_start": 45.0,
    "pg_ctl_stop": 30.0,
    "createdb": 30.0,
    "pg_basebackup": 300.0,
    "pg_verifybackup": 120.0,
    "pg_controldata": 20.0,
    "psql": 30.0,
    "alembic": 180.0,
    "restart_reconcile": 180.0,
}
TERMINATION_GRACE_SECONDS = 5.0
SOURCE_IDENTITY_PATHS = (
    "dish_pg/recovery_control.py",
    "dish_pg/recovery_rehearsal.py",
    "dish_pg/workflow.py",
    "dish_pg/bootstrap.py",
    "dish_pg/candidate_manifest.py",
    "dish_pg/candidate_manifest_models.py",
    "dish_pg/database.py",
    "dish_pg/models.py",
    "dish_pg/release.py",
    "dish_pg/release_evidence.py",
    "dish_pg/release_status.py",
    "dish_pg/repositories.py",
    "dish_pg/services.py",
    "dish_pg/stage3_models.py",
    "dish_pg/stage5_models.py",
    "dish_pg/stage6_models.py",
    "dish_pg/transition.py",
    f"dish_pg/migrations/versions/{ALEMBIC_HEAD}.py",
    "scripts/dish-pg-recovery-rehearsal",
    "deploy/postgresql/compose.yaml",
    "requirements.txt",
    "alembic.ini",
)


class RehearsalError(RuntimeError):
    """The recovery rehearsal cannot safely continue."""


class RehearsalBlocked(RehearsalError):
    """Required disposable native infrastructure is unavailable."""

    def __init__(self, message: str, *, missing_commands: Sequence[str] = ()) -> None:
        super().__init__(message)
        self.missing_commands = tuple(missing_commands)


class CommandTimeout(RehearsalError):
    """An external command exceeded its finite execution deadline."""


class InjectedFinalizationFault(RehearsalError):
    """Deterministic interruption after backup rename and before receipt finalization."""


class InjectedRestoreFault(RehearsalError):
    """Deterministic interruption while materializing an independent restore."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_json(path: Path, value: Mapping[str, Any], *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, sort_keys=True, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, mode)
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_safe_owned_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved in {Path("/"), Path.home(), ROOT.resolve(), ROOT.parent.resolve()}:
        raise RehearsalError(f"unsafe rehearsal root: {resolved}")
    if not SAFE_PATH.fullmatch(str(resolved)):
        raise RehearsalError("rehearsal paths may contain only letters, numbers, _, -, ., and /")
    resolved.mkdir(parents=True, exist_ok=True)
    marker = resolved / ".dish-section2-owned"
    if marker.exists():
        try:
            marker_value = marker.read_text(encoding="utf-8")
        except OSError as exc:
            raise RehearsalError("rehearsal ownership marker is unreadable") from exc
        if marker_value != REPORT_SCHEMA + "\n":
            raise RehearsalError("rehearsal ownership marker is invalid")
        return resolved
    occupied = [entry for entry in resolved.iterdir() if entry.name != marker.name]
    if occupied:
        raise RehearsalError("rehearsal root is nonempty and lacks the ownership marker")
    marker.write_text(REPORT_SCHEMA + "\n", encoding="utf-8")
    return resolved


def require_empty_target(path: Path) -> None:
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise RehearsalError(f"restore target contains unexpected state: {path}")
    path.mkdir(parents=True, exist_ok=True)


def discover_pg_bin(explicit: Path | None) -> dict[str, Path]:
    candidates: dict[str, Path] = {}
    missing: list[str] = []
    for name in PG_REQUIRED:
        candidate = explicit / name if explicit is not None else None
        if candidate is not None:
            found = (
                str(candidate)
                if candidate.is_file() and os.access(candidate, os.X_OK)
                else None
            )
        else:
            found = shutil.which(name)
        if found:
            candidates[name] = Path(found).resolve()
        else:
            missing.append(name)
    if missing:
        raise RehearsalBlocked(
            "native PostgreSQL tooling unavailable; missing " + ", ".join(missing)
            + ". Supply --pg-bin containing PostgreSQL 17 server/client binaries.",
            missing_commands=missing,
        )
    return candidates


@dataclass
class CommandEvidence:
    argv: list[str]
    process_group_id: int | None
    started_at: str
    duration_seconds: float
    timeout_seconds: float
    returncode: int | None
    stdout_log: str
    stderr_log: str
    timed_out: bool
    termination: str | None
    cleanup_result: str


class Runner:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.commands: list[CommandEvidence] = []
        self.counter = 0

    @staticmethod
    def _terminate_group(process: subprocess.Popen[object]) -> tuple[str, str]:
        termination = "SIGTERM"
        cleanup = "terminated"
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return "already_exited", "already_exited"
        except OSError as exc:
            return "SIGTERM_failed", f"signal_failed:{exc.errno}"
        try:
            process.wait(timeout=TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            termination = "SIGTERM_then_SIGKILL"
            cleanup = "killed"
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                cleanup = "exited_during_escalation"
            except OSError as exc:
                cleanup = f"kill_signal_failed:{exc.errno}"
            try:
                process.wait(timeout=TERMINATION_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                cleanup = "kill_wait_timeout"
        return termination, cleanup

    def run(
        self,
        argv: Sequence[str | Path],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        inherit_env: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        if timeout_seconds <= 0:
            raise ValueError("command timeout must be positive")
        self.counter += 1
        safe_argv = [str(item) for item in argv]
        stdout_path = self.log_dir / f"{self.counter:03d}-stdout.log"
        stderr_path = self.log_dir / f"{self.counter:03d}-stderr.log"
        started = utc_now()
        before = time.perf_counter()
        timed_out = False
        termination = None
        cleanup_result = "exited"
        returncode: int | None = None
        process_group_id: int | None = None
        with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
            "w", encoding="utf-8"
        ) as stderr_handle:
            try:
                process = subprocess.Popen(
                    safe_argv,
                    text=True,
                    stdout=stdout_handle,
                    stderr=stderr_handle,
                    env=(
                        None
                        if env is None and inherit_env
                        else ({**os.environ, **(env or {})} if inherit_env else dict(env or {}))
                    ),
                    start_new_session=True,
                )
                process_group_id = process.pid
            except OSError as exc:
                stderr_handle.write(str(exc) + "\n")
                cleanup_result = "spawn_failed"
                process = None
            if process is not None:
                try:
                    returncode = process.wait(timeout=timeout_seconds)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    termination, cleanup_result = self._terminate_group(process)
                    returncode = process.returncode
        duration = time.perf_counter() - before
        stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
        evidence = CommandEvidence(
            argv=_redact_argv(safe_argv),
            process_group_id=process_group_id,
            started_at=iso(started),
            duration_seconds=duration,
            timeout_seconds=timeout_seconds,
            returncode=returncode,
            stdout_log=str(stdout_path),
            stderr_log=str(stderr_path),
            timed_out=timed_out,
            termination=termination,
            cleanup_result=cleanup_result,
        )
        self.commands.append(evidence)
        if timed_out:
            raise CommandTimeout(
                f"command timed out after {timeout_seconds:.1f}s: {safe_argv[0]}; "
                f"termination={termination}; stderr={stderr_path}"
            )
        if returncode is None:
            raise RehearsalError(f"command could not start: {safe_argv[0]}; stderr={stderr_path}")
        completed = subprocess.CompletedProcess(safe_argv, returncode, stdout, stderr)
        if check and returncode != 0:
            raise RehearsalError(
                f"command failed ({returncode}): {safe_argv[0]}; stderr={stderr_path}"
            )
        return completed


def _redact_argv(argv: Sequence[str]) -> list[str]:
    redacted: list[str] = []
    for value in argv:
        if "postgresql://" in value or "postgresql+psycopg://" in value:
            redacted.append(re.sub(r"//[^/@]+@", "//<redacted>@", value))
        else:
            redacted.append(value)
    return redacted


def _controldata_system_identifier(
    runner: Runner, pg_controldata: Path, data_dir: Path
) -> str:
    result = runner.run(
        [pg_controldata, data_dir],
        timeout_seconds=COMMAND_TIMEOUTS["pg_controldata"],
        env={"LC_ALL": "C"},
    )
    match = re.search(r"Database system identifier:\s*(\d+)", result.stdout)
    if match is None:
        raise RehearsalError("pg_controldata omitted the database system identifier")
    return match.group(1)


@dataclass
class Cluster:
    name: str
    data_dir: Path
    socket_dir: Path
    port: int
    database: str
    binaries: Mapping[str, Path]
    runner: Runner
    archive_dir: Path | None = None
    started: bool = False

    @property
    def dsn(self) -> str:
        return f"postgresql+psycopg://dish@127.0.0.1:{self.port}/{self.database}"

    def init(self) -> None:
        require_empty_target(self.data_dir)
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        self.runner.run(
            [
                self.binaries["initdb"],
                "-D",
                self.data_dir,
                "--username=dish",
                "--auth=trust",
                "--no-locale",
                "--encoding=UTF8",
            ],
            timeout_seconds=COMMAND_TIMEOUTS["initdb"],
        )
        self._append_config(primary=True)

    def _append_config(self, *, primary: bool) -> None:
        lines = [
            f"port = {self.port}",
            "listen_addresses = '127.0.0.1'",
            f"unix_socket_directories = '{self.socket_dir}'",
            "logging_collector = on",
            f"log_directory = '{self.data_dir / 'log'}'",
            "log_filename = 'postgresql.log'",
            "fsync = on",
            "full_page_writes = on",
        ]
        if primary:
            if self.archive_dir is None:
                raise RehearsalError("primary cluster requires an archive directory")
            helper = self.archive_dir.parent / "archive-wal"
            lines.extend(
                [
                    "wal_level = replica",
                    "archive_mode = on",
                    f"archive_command = '{helper} %p %f'",
                    "archive_timeout = '5s'",
                ]
            )
        else:
            lines.append("archive_mode = off")
        with (self.data_dir / "postgresql.conf").open("a", encoding="utf-8") as handle:
            handle.write("\n# dish Section 2 disposable recovery configuration\n")
            handle.write("\n".join(lines) + "\n")

    def start(self, *, wait_seconds: int = 20, check: bool = True) -> bool:
        result = self.runner.run(
            [
                self.binaries["pg_ctl"],
                "-D",
                self.data_dir,
                "-w",
                "-t",
                str(wait_seconds),
                "start",
            ],
            timeout_seconds=max(COMMAND_TIMEOUTS["pg_ctl_start"], wait_seconds + 10.0),
            check=check,
        )
        self.started = result.returncode == 0
        return self.started

    def stop(self, *, mode: str = "fast") -> None:
        if not (self.data_dir / "postmaster.pid").exists():
            self.started = False
            return
        result = self.runner.run(
            [self.binaries["pg_ctl"], "-D", self.data_dir, "-m", mode, "stop"],
            timeout_seconds=COMMAND_TIMEOUTS["pg_ctl_stop"],
            check=False,
        )
        if result.returncode != 0:
            raise RehearsalError(
                f"failed to stop PostgreSQL cluster {self.name}; "
                f"stderr={self.runner.commands[-1].stderr_log}"
            )
        self.started = False

    def create_database(self) -> None:
        self.runner.run(
            [
                self.binaries["createdb"],
                "-h",
                "127.0.0.1",
                "-p",
                str(self.port),
                "-U",
                "dish",
                self.database,
            ],
            timeout_seconds=COMMAND_TIMEOUTS["createdb"],
        )

    def system_identifier(self) -> str:
        return _controldata_system_identifier(
            self.runner, self.binaries["pg_controldata"], self.data_dir
        )


@dataclass(frozen=True)
class SeedContext:
    generation_id: uuid.UUID
    import_run_id: uuid.UUID
    binding_id: uuid.UUID
    task_id: uuid.UUID
    run_id: uuid.UUID
    lease_id: uuid.UUID
    execution_id: uuid.UUID
    operation_id: uuid.UUID
    projection_epoch_id: uuid.UUID
    candidate_id: uuid.UUID
    dish_release: str
    protocol_release: str
    baseline_transaction_id: int


@dataclass
class Boundary:
    label: str
    lsn: str
    committed_at: str
    transaction_id: int
    expected_labels: list[str]


def _alembic_upgrade(runner: Runner, dsn: str) -> None:
    code = (
        "import os; from alembic import command; from alembic.config import Config; "
        "config=Config(os.environ['DISH_SECTION2_ALEMBIC_CONFIG']); "
        "config.set_main_option('sqlalchemy.url', os.environ['DISH_SECTION2_ALEMBIC_URL']); "
        "command.upgrade(config, 'head')"
    )
    runner.run(
        [sys.executable, "-c", code],
        timeout_seconds=COMMAND_TIMEOUTS["alembic"],
        env={
            "DISH_SECTION2_ALEMBIC_CONFIG": str(ROOT / "alembic.ini"),
            "DISH_SECTION2_ALEMBIC_URL": dsn,
        },
    )


def _engine(dsn: str) -> Engine:
    return create_engine(
        dsn,
        future=True,
        pool_pre_ping=True,
        connect_args={
            "connect_timeout": 5,
            "options": "-c statement_timeout=30000 -c lock_timeout=5000",
        },
    )


def _rehearsal_setup_release_service(session: Session) -> ReleaseCandidateService:
    """Keep fixture timestamps coherent inside the long seed transaction."""
    return ReleaseCandidateService(
        session, clock=lambda: utc_now() + timedelta(seconds=1)
    )


def _record_candidate_acceptance_evidence(
    session: Session,
    candidate: release_models.ReleaseCandidate,
    *,
    evidence_dir: Path,
    recorded_at: datetime,
) -> None:
    """Seed the production release gates that rollback burn must re-evaluate."""
    service = _rehearsal_setup_release_service(session)
    artifact_dir = evidence_dir / "cutover-acceptance"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    def artifact(label: str) -> tuple[str, str]:
        path = artifact_dir / f"{label}.json"
        path.write_text(label + "\n", encoding="utf-8")
        return str(path), sha256_file(path)

    for category, key in REQUIRED_EVIDENCE:
        path, digest = artifact(f"evidence-{category}-{key}")
        service.record_evidence(
            candidate_id=candidate.candidate_id,
            category=category,
            evidence_key=key,
            outcome="pass",
            payload={
                "artifact_kind": EVIDENCE_ARTIFACT_KINDS[(category, key)],
                "artifact_identity": f"section2:{category}:{key}",
                "artifact_path": path,
                "artifact_sha256": digest,
                "source_manifest_sha256": "a" * 64,
                "gate_name": f"{category}:{key}",
                "gate_result": "pass",
            },
            recorded_at=recorded_at,
        )

    for kind in REQUIRED_REHEARSALS:
        rehearsal = service.start_rehearsal(
            candidate_id=candidate.candidate_id,
            rehearsal_kind=kind,
            environment_identity="section2-native-recovery-rehearsal",
            source_manifest_sha256="a" * 64,
            started_at=recorded_at,
        )
        checkpoints: list[dict[str, str]] = []
        for checkpoint_kind in REQUIRED_REHEARSAL_CHECKPOINTS[kind]:
            path, digest = artifact(f"checkpoint-{kind}-{checkpoint_kind}")
            checkpoint = service.record_rehearsal_checkpoint(
                rehearsal_id=rehearsal.rehearsal_id,
                checkpoint_kind=checkpoint_kind,
                payload={
                    "rehearsal_kind": kind,
                    "checkpoint_kind": checkpoint_kind,
                    "evidence_kind": REHEARSAL_CHECKPOINT_EVIDENCE_KINDS[kind][
                        checkpoint_kind
                    ],
                    "artifact_identity": f"section2:{kind}:{checkpoint_kind}",
                    "artifact_path": path,
                    "artifact_sha256": digest,
                    "source_manifest_sha256": "a" * 64,
                    "gate_result": "pass",
                },
                recorded_at=recorded_at,
            )
            checkpoints.append(
                {
                    "checkpoint_kind": checkpoint.checkpoint_kind,
                    "payload_sha256": checkpoint.payload_sha256,
                }
            )
        service.finish_rehearsal(
            rehearsal_id=rehearsal.rehearsal_id,
            passed=True,
            report={
                "rehearsal_kind": kind,
                "source_manifest_sha256": "a" * 64,
                "result": "passed",
                "checkpoint_manifest_sha256": release_sha256_json(checkpoints),
            },
            measured_rpo_seconds=0.0 if kind == "restore" else None,
            measured_rto_seconds=12.5 if kind == "restore" else None,
            completed_at=recorded_at,
        )


def _authorize_release_candidate(
    session: Session,
    candidate: release_models.ReleaseCandidate,
    *,
    approved_at: datetime,
) -> uuid.UUID:
    """Use the production validation/approval path before exercising cutover."""
    service = _rehearsal_setup_release_service(session)
    bundle = service.build_evidence_bundle(
        candidate_id=candidate.candidate_id,
        bundle_kind="release_candidate",
        built_at=approved_at,
    )
    service.validate_candidate(
        candidate_id=candidate.candidate_id,
        evidence_bundle_id=bundle.bundle_id,
        validated_at=approved_at,
    )
    closure = service.record_final_asana_closure(
        candidate_id=candidate.candidate_id,
        capture_manifest_sha256="a" * 64,
        observation_high_water="section2-pre-cutover-high-water",
        watcher_identity="section2-recovery-rehearsal",
        interval_started_at=approved_at,
        closed_through_at=approved_at,
        payload={"purpose": "section2 rollback-burn approval binding"},
        recorded_at=approved_at,
    )
    service.approve_candidate(
        candidate_id=candidate.candidate_id,
        evidence_bundle_id=bundle.bundle_id,
        approver="section2-recovery-rehearsal",
        approval_statement=(
            "Authorize this exact validated release candidate for recovery rehearsal."
        ),
        approval_payload={
            "purpose": "section2 native recovery authorization",
            "candidate_id": str(candidate.candidate_id),
            "evidence_bundle_sha256": bundle.manifest_sha256,
            "final_asana_closure_id": str(closure.closure_id),
            "final_asana_closure_sha256": closure.closure_sha256,
        },
        approved_at=approved_at,
    )
    return closure.closure_id


def _commit_rollback_burn(engine: Engine, context: SeedContext) -> int:
    """Drive the real guarded cutover path through the committed rollback burn."""
    factory = session_factory(engine)
    with session_scope(factory) as session:
        candidate = session.get(release_models.ReleaseCandidate, context.candidate_id)
        if candidate is None or candidate.status != "approved":
            raise RehearsalError(
                "rollback-burn rehearsal requires the exact approved candidate"
            )
        service = (
            ReleaseCandidateService(session)
            if session.get_bind().dialect.name == "postgresql"
            else _rehearsal_setup_release_service(session)
        )
        writer_target = "legacy-service@section2-recovery-rehearsal"
        prepared_at = service._trusted_now()
        fence = service.prepare_writer_fence(
            candidate_id=candidate.candidate_id,
            target_identity=writer_target,
            mechanism="fail-closed-file",
            manifest={"path": "/tmp/dish-section2-recovery-writer-fence.json"},
            prepared_at=prepared_at,
        )
        run = service.prepare_cutover(
            candidate_id=candidate.candidate_id,
            started_at=prepared_at,
        )
        observation = service.record_writer_fence_artifact_observation(
            fence_id=fence.fence_id,
            artifact_generation_identity="section2-recovery-writer-fence-v1",
            canonical_path="/tmp/dish-section2-recovery-writer-fence.json",
            content_sha256=fence.manifest_sha256,
            filesystem_device=1,
            filesystem_inode=(fence.fence_id.int % 2_000_000_000) + 1,
            verification_result="matched",
            observation_contract_version="section2-recovery-rehearsal-v1",
            observed_at=prepared_at,
            recorded_at=prepared_at,
        )
        service.engage_writer_fence(
            fence_id=fence.fence_id,
            artifact_observation_id=observation.observation_id,
            engaged_at=prepared_at,
        )
        writer_inventory = {writer_target}
        service.verify_writer_fence(
            fence_id=fence.fence_id,
            proof={
                "probe_kind": "authenticated_mutation_rejected_before_body_parse",
                "candidate_id": str(candidate.candidate_id),
                "target_identity": fence.target_identity,
                "fence_manifest_sha256": fence.manifest_sha256,
                "request_token_sha256": "f" * 64,
                "http_status": 409,
                "response_code": "CONFLICT",
                "response_rule": "legacy_writer_fenced",
                "response_retryable": False,
                "body_loaded": False,
                "result": "pass",
            },
            verified_at=prepared_at,
            required_writer_inventory=writer_inventory,
        )
        service.mark_fenced(
            cutover_run_id=run.cutover_run_id,
            recorded_at=prepared_at,
            required_writer_inventory=writer_inventory,
        )
        closure = service.record_final_asana_closure(
            candidate_id=candidate.candidate_id,
            capture_manifest_sha256="a" * 64,
            observation_high_water="section2-post-fence-high-water",
            watcher_identity="section2-recovery-rehearsal",
            interval_started_at=prepared_at,
            closed_through_at=prepared_at,
            payload={"purpose": "section2 post-writer-fence final closure"},
            recorded_at=prepared_at,
        )
        service.recertify_candidate(
            candidate_id=candidate.candidate_id,
            closure_id=closure.closure_id,
            approver="section2-recovery-rehearsal",
            recertification_statement="Final closure remains exact after writer fencing.",
            payload={"cutover_run_id": str(run.cutover_run_id)},
            recertified_at=prepared_at,
        )
        service.activate_authority(
            cutover_run_id=run.cutover_run_id,
            final_asana_closure_id=closure.closure_id,
            activated_at=prepared_at,
            required_writer_inventory=writer_inventory,
        )
        burned_at = service._trusted_now()
        service.burn_rollback(
            cutover_run_id=run.cutover_run_id,
            legacy_bundle_id="section2-recovery-rehearsal-rollback-burn",
            burned_at=burned_at,
            required_writer_inventory=writer_inventory,
        )
        transaction_id = (
            int(session.scalar(text("SELECT txid_current()")))
            if session.get_bind().dialect.name == "postgresql"
            else 0
        )
    return transaction_id


def _seed_baseline(engine: Engine, evidence_dir: Path, *, dish_commit: str) -> SeedContext:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    source_path = evidence_dir / "seed-source.ndjson"
    source_path.write_text('{"section2":"baseline"}\n', encoding="utf-8")
    section_id = uuid.uuid5(uuid.NAMESPACE_URL, "dish-section2-section")
    section_gid = "9000000000000002"
    source = SourceBundle(
        path=source_path,
        sha256=sha256_file(source_path),
        record_count=1,
        max_observed_at=utc_now(),
        sections={section_id: section_gid},
    )
    honest_root = evidence_dir / "synthetic-honest-evidence"
    honest_root.mkdir(exist_ok=True)
    honest = HonestCheckout(
        root=honest_root,
        commit="0" * 40,
        protocol_version="section2-protocol",
        schema_version="section2-schema",
        protocol_sha256="1" * 64,
        schema_sha256="2" * 64,
        protocol_files={"section2": {"sha256": "1" * 64}},
    )
    spec = InitialBootstrapSpec(
        dish_commit=dish_commit,
        schema_head=ALEMBIC_HEAD,
        source_generation="section2-disposable-source",
        source_bundle=source,
        honest=honest,
        project_id=uuid.uuid5(uuid.NAMESPACE_URL, "dish-section2-project"),
        project_gid="9000000000000001",
        project_name="Section 2 Recovery",
        sections=(
            SectionSpec(
                section_id=section_id,
                section_gid=section_gid,
                section_name="Recovery Evidence",
                workflow_role="research_queue",
            ),
        ),
    )
    factory = session_factory(engine)
    with session_scope(factory) as session:
        result = bootstrap_initial_generation(session, spec)
        task_id = uuid.uuid5(uuid.NAMESPACE_URL, "dish-section2-task")
        CoreAuthorityService(session).import_task_document(
            generation_id=result.generation_id,
            import_run_id=result.import_run_id,
            contract_binding_id=result.binding_id,
            spec=ImportedTaskSpec(
                task_id=task_id,
                asana_task_gid="9000000000000003",
                title="[ready] PostgreSQL recovery representative state",
                body="Section 2 native PostgreSQL recovery evidence\n---\nStatus: ready\n",
                identity_scheme="section2-sha256-v1",
                content_identity=REPRESENTATIVE_CONTENT_IDENTITY,
                project_ids=(result.project_id,),
                section_id=result.sections[0].section_id,
                completed=False,
                observed_at=utc_now(),
            ),
        )
        projection = ProjectionService(session)
        epoch = projection.activate_epoch(
            generation_id=result.generation_id,
            activation_reason="section2 baseline",
            created_at=utc_now(),
            external_effects_enabled=True,
        )
        if projection.bind_imported_mappings(
            generation_id=result.generation_id,
            bound_at=utc_now(),
        ) != (1, 1, 1):
            raise RehearsalError("baseline import did not produce exact projection mappings")
        run_id = uuid.uuid4()
        workflow = WorkflowAuthorityService(session)
        workflow.register_run(
            run_id=run_id,
            generation_id=result.generation_id,
            owner_id="section2-pre-restore-service",
            agent="service",
            capability_digest=hashlib.sha256(b"section2-pre-restore-capability").digest(),
            registered_at=utc_now(),
        )
        baseline_payload = {"label": "baseline", "revision": 1}
        baseline_request_id = uuid.uuid5(
            uuid.NAMESPACE_URL, "dish-section2-request-baseline"
        )
        workflow.admit_request(
            RequestSpec(
                request_id=baseline_request_id,
                generation_id=result.generation_id,
                run_id=run_id,
                owner_id="section2-pre-restore-service",
                principal_class="service",
                command_name="section2_recovery_marker",
                canonical_payload=baseline_payload,
                protocol_release=result.protocol_release,
                dish_release=result.dish_release,
                admitted_at=utc_now(),
            )
        )
        workflow.repo.record_outcome(
            request_id=baseline_request_id,
            outcome=StoredOutcome(
                outcome_id=uuid.uuid5(
                    uuid.NAMESPACE_URL, "dish-section2-outcome-baseline"
                ),
                outcome_class="success",
                result_code="section2.baseline",
                http_status=200,
                result_payload={"label": "baseline"},
                immutable_success=True,
                recorded_at=utc_now(),
            ),
            execution_id=None,
            audit_event_id=uuid.uuid5(
                uuid.NAMESPACE_URL, "dish-section2-audit-baseline"
            ),
            audit_event_type="section2_recovery_boundary",
            actor="section2-rehearsal",
            audit_payload={"label": "baseline"},
            task_id=task_id,
            operation_id=None,
            obligation_id=uuid.uuid5(
                uuid.NAMESPACE_URL, "dish-section2-obligation-baseline"
            ),
            invocation_metadata={"label": "baseline", "required": True},
        )
        session.flush()
        baseline_terminal_at = utc_now()
        session.add(
            transition_models.ProjectionOutboxEvent(
                projection_event_id=uuid.uuid5(
                    uuid.NAMESPACE_URL, "dish-section2-projection-baseline"
                ),
                generation_id=result.generation_id,
                projection_epoch_id=epoch.projection_epoch_id,
                source_route="service",
                origin="live",
                command_execution_id=None,
                task_id=task_id,
                event_type="reproject",
                aggregate_sequence=1,
                idempotency_key=hashlib.sha256(
                    b"section2-projection-baseline"
                ).hexdigest(),
                intent_payload=baseline_payload,
                intent_sha256=sha256_json(baseline_payload),
                state="applied",
                claim_owner=None,
                claim_token=None,
                claim_expires_at=None,
                outbox_revision=1,
                created_at=utc_now(),
                terminal_at=baseline_terminal_at,
            )
        )
        execution_id = uuid.uuid4()
        workflow.begin_execution(
            ExecutionSpec(
                execution_id=execution_id,
                request_id=baseline_request_id,
                generation_id=result.generation_id,
                task_id=task_id,
                operation_id=None,
                command_name="section2_recovery_marker",
                transaction_profile="E",
                canonical_intent=baseline_payload,
                pinned_inputs={"label": "baseline"},
                contract_binding_id=result.binding_id,
                admitted_at=utc_now(),
            )
        )
        workflow.repo.capture_task_fence(
            execution_id=execution_id,
            generation_id=result.generation_id,
            task_id=task_id,
            at=utc_now(),
        )
        operation_id = uuid.uuid4()
        workflow.create_operation(
            operation_id=operation_id,
            execution_id=execution_id,
            task_id=task_id,
            kind="initial",
            phase="recovery_rehearsal",
            persisted_actions=["section2_recovery_marker"],
            created_at=utc_now(),
        )
        lease_id = uuid.uuid4()
        workflow.acquire_actor_lease(
            lease_id=lease_id,
            execution_id=execution_id,
            operation_id=operation_id,
            run_id=run_id,
            owner_id="section2-pre-restore-service",
            actor_role="researcher",
            actor_attempt_sequence=1,
            issued_at=utc_now(),
            expires_at=utc_now() + timedelta(days=365),
        )
        execution = session.get(wf.CommandExecution, execution_id)
        operation = session.get(wf.WorkflowOperation, operation_id)
        lease = session.get(wf.ServiceLease, lease_id)
        obligation = session.scalar(
            select(wf.InvocationAuditObligation).where(
                wf.InvocationAuditObligation.request_id == baseline_request_id
            )
        )
        if execution is None or operation is None or lease is None or obligation is None:
            raise RehearsalError("baseline recovery-fencing rows are incomplete")
        execution.status = "committed"
        execution.execution_revision += 1
        execution.terminal_at = baseline_terminal_at
        operation.lifecycle = "completed"
        operation.terminal_outcome = "section2-baseline-complete"
        operation.operation_revision += 1
        operation.terminal_at = baseline_terminal_at
        lease.state = "released"
        lease.lease_revision += 1
        lease.terminal_at = baseline_terminal_at
        obligation.state = "fulfilled"
        obligation.terminal_at = baseline_terminal_at
        import_batch_id = uuid.uuid4()
        baseline_id = uuid.uuid4()
        candidate_id = uuid.uuid4()
        source_import = SourceImportService(session)
        imported_at = utc_now()
        source_import.start_batch(
            import_batch_id=import_batch_id,
            generation_id=result.generation_id,
            import_run_id=result.import_run_id,
            source_release="section2-source",
            source_commit="3" * 40,
            source_database_sha256="4" * 64,
            source_sidecars={"purpose": "section2 recovery"},
            ledger_through_commit="5" * 40,
            expected_entities=4,
            started_at=imported_at,
        )
        content_version_id = session.scalar(
            select(models.ContentVersion.content_version_id).where(
                models.ContentVersion.task_id == task_id,
                models.ContentVersion.import_run_id == result.import_run_id,
            )
        )
        if content_version_id is None:
            raise RehearsalError("baseline import omitted representative content provenance")
        imported_targets = (
            ("project", "governed_project", result.project_id, "project_id"),
            ("section", "governed_section", result.sections[0].section_id, "section_id"),
            ("task", "dish_task", task_id, "task_id"),
            ("content", "task_content_version", content_version_id, "content_version_id"),
        )
        for entity_kind, target_type, target_id, target_field in imported_targets:
            evidence = source_import.record_entity(
                import_batch_id=import_batch_id,
                entity_kind=entity_kind,
                source_identity=f"section2:{entity_kind}:{target_id}",
                source_sha256="4" * 64,
                target_entity_type=target_type,
                target_entity_id=target_id,
                provenance={"purpose": "section2 recovery rehearsal"},
                imported_at=imported_at,
            )
            typed_target = {
                "project_id": None,
                "section_id": None,
                "task_id": None,
                "content_version_id": None,
                "request_tombstone_id": None,
            }
            typed_target[target_field] = target_id
            session.add(
                import_link_models.SourceImportNativeLink(
                    link_id=uuid.uuid4(),
                    evidence_id=evidence.evidence_id,
                    import_batch_id=import_batch_id,
                    import_run_id=result.import_run_id,
                    entity_kind=entity_kind,
                    linked_at=imported_at,
                    **typed_target,
                )
            )
        source_import.complete_batch(
            import_batch_id=import_batch_id, completed_at=imported_at
        )
        session.add(
            transition_models.ShadowBaseline(
                shadow_baseline_id=baseline_id,
                generation_id=result.generation_id,
                source_generation_identity="section2-source-generation",
                source_commit="3" * 40,
                baseline_sequence=1,
                status="closed",
                disqualification_reason=None,
                created_at=imported_at,
                terminal_at=imported_at,
            )
        )
        session.flush()
        projection.bind_imported_mappings(
            generation_id=result.generation_id, bound_at=imported_at
        )
        service = _rehearsal_setup_release_service(session)
        candidate = service.create_candidate(
            candidate_id=candidate_id,
            generation_id=result.generation_id,
            source_import_batch_id=import_batch_id,
            shadow_baseline_id=baseline_id,
            projection_epoch_id=epoch.projection_epoch_id,
            source_release="section2-source",
            source_commit="3" * 40,
            ledger_through_commit="5" * 40,
            schema_head=ALEMBIC_HEAD,
            dish_release=result.dish_release,
            honest_release=result.honest_release,
            protocol_release=result.protocol_release,
            openapi_release="section2-openapi",
            routing_release="section2-routing",
            created_at=utc_now(),
        )
        session.flush()
        reconciliation_at = utc_now()
        membership = active_mapping_membership(session, candidate=candidate)
        reconciliation = transition_models.ProjectionReconciliationRun(
            reconciliation_run_id=uuid.uuid4(),
            generation_id=result.generation_id,
            projection_epoch_id=epoch.projection_epoch_id,
            corpus_identity=f"section2-recovery-approval:{candidate_id}",
            candidate_id=candidate_id,
            registry_version_id=result.registry_version_id,
            observation_started_at=reconciliation_at,
            observation_completed_at=reconciliation_at,
            external_snapshot_identity=None,
            external_high_water="section2-recovery-approval-high-water",
            corpus_manifest_sha256=reconciliation_corpus_sha256(
                candidate=candidate, membership=membership
            ),
            scope_complete=True,
            adapter_contract_version="asana-high-water-v1",
            evidence_recorded_at=reconciliation_at,
            status="complete",
            expected_items=len(membership),
            processed_items=len(membership),
            started_at=reconciliation_at,
            completed_at=reconciliation_at,
        )
        session.add(reconciliation)
        session.flush()
        for ordinal, (entity_kind, mapping_id) in enumerate(
            sorted(membership, key=lambda item: (item[0], str(item[1])))
        ):
            session.add(
                transition_models.ProjectionReconciliationItem(
                    reconciliation_item_id=uuid.uuid4(),
                    reconciliation_run_id=reconciliation.reconciliation_run_id,
                    item_identity=f"{ordinal}:{entity_kind}:{mapping_id}",
                    entity_kind=entity_kind,
                    mapping_id=mapping_id,
                    outcome="matched",
                    evidence={"source": "section2 recovery rehearsal"},
                    recorded_at=reconciliation_at,
                )
            )
        session.flush()
        _record_candidate_acceptance_evidence(
            session,
            candidate,
            evidence_dir=evidence_dir,
            recorded_at=reconciliation_at,
        )
        baseline_evidence = {"label": "baseline", "revision": 1}
        session.add(
            release_models.ReleaseEvidenceItem(
                evidence_id=uuid.uuid5(
                    uuid.NAMESPACE_URL, "dish-section2-release-baseline"
                ),
                candidate_id=candidate_id,
                category="postgresql_recovery",
                evidence_key="baseline",
                evidence_revision=1,
                outcome="info",
                payload=baseline_evidence,
                payload_sha256=sha256_json(baseline_evidence),
                recorded_at=utc_now(),
            )
        )
        session.add(
            models.AppliedMigrationEvent(
                migration_event_id=uuid.uuid4(),
                generation_id=result.generation_id,
                revision=ALEMBIC_HEAD,
                predecessor_revision=None,
                migration_code_sha256=migration_revision_sha256(ALEMBIC_HEAD),
                dish_release=result.dish_release,
                initiator="dish-pg-recovery-rehearsal",
                outcome="applied",
                started_at=utc_now(),
                terminal_at=utc_now(),
                details={"alembic_version": ALEMBIC_HEAD, "purpose": "section2 baseline"},
            )
        )
        session.flush()
        _authorize_release_candidate(session, candidate, approved_at=utc_now())
        baseline_transaction_id = (
            int(session.scalar(text("SELECT txid_current()")))
            if session.get_bind().dialect.name == "postgresql"
            else 0
        )
        return SeedContext(
            generation_id=result.generation_id,
            import_run_id=result.import_run_id,
            binding_id=result.binding_id,
            task_id=task_id,
            run_id=run_id,
            lease_id=lease_id,
            execution_id=execution_id,
            operation_id=operation_id,
            projection_epoch_id=epoch.projection_epoch_id,
            candidate_id=candidate_id,
            dish_release=result.dish_release,
            protocol_release=result.protocol_release,
            baseline_transaction_id=baseline_transaction_id,
        )


def _record_bundle(engine: Engine, context: SeedContext, label: str, revision: int) -> int:
    factory = session_factory(engine)
    with session_scope(factory) as session:
        payload = {"label": label, "revision": revision}
        session.add(
            transition_models.ProjectionOutboxEvent(
                projection_event_id=uuid.uuid5(
                    uuid.NAMESPACE_URL, f"dish-section2-projection-{label}"
                ),
                generation_id=context.generation_id,
                projection_epoch_id=context.projection_epoch_id,
                source_route="service",
                origin="live",
                command_execution_id=None,
                task_id=context.task_id,
                event_type="reproject",
                aggregate_sequence=revision,
                idempotency_key=hashlib.sha256(
                    f"section2-projection-{label}".encode()
                ).hexdigest(),
                intent_payload=payload,
                intent_sha256=sha256_json(payload),
                state="pending",
                claim_owner=None,
                claim_token=None,
                claim_expires_at=None,
                outbox_revision=1,
                created_at=utc_now(),
                terminal_at=None,
            )
        )
        session.add(
            release_models.ReleaseEvidenceItem(
                evidence_id=uuid.uuid5(
                    uuid.NAMESPACE_URL, f"dish-section2-release-{label}"
                ),
                candidate_id=context.candidate_id,
                category="postgresql_recovery",
                evidence_key=label,
                evidence_revision=1,
                outcome="info",
                payload=payload,
                payload_sha256=sha256_json(payload),
                recorded_at=utc_now(),
            )
        )
        session.flush()
        transaction_id = (
            int(session.scalar(text("SELECT txid_current()")))
            if session.get_bind().dialect.name == "postgresql"
            else 0
        )
    return transaction_id


def _record_boundary(
    engine: Engine, label: str, expected_labels: list[str], transaction_id: int
) -> Boundary:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT pg_current_wal_flush_lsn()::text, clock_timestamp()")
        ).one()
    return Boundary(
        label=label,
        lsn=row[0],
        committed_at=iso(row[1]),
        transaction_id=transaction_id,
        expected_labels=list(expected_labels),
    )


def _force_archive(engine: Engine, archive_dir: Path, timeout_seconds: float = 20.0) -> str:
    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT pg_walfile_name(pg_current_wal_lsn()), pg_switch_wal()")
        ).one()
        wal = str(row[0])
    deadline = time.monotonic() + timeout_seconds
    target = archive_dir / wal
    while time.monotonic() < deadline:
        if target.is_file() and target.stat().st_size > 0:
            return wal
        time.sleep(0.1)
    raise RehearsalError(f"WAL segment was not archived: {wal}")


def _counts(engine: Engine) -> dict[str, Any]:
    with engine.connect() as connection:
        head = connection.scalar(text("SELECT version_num FROM alembic_version"))
        release_labels = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT evidence_key FROM release_evidence_items "
                    "WHERE category='postgresql_recovery' ORDER BY evidence_key"
                )
            )
        ]
        projection_labels = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT intent_payload ->> 'label' FROM projection_outbox_events "
                    "WHERE intent_payload ->> 'label' IS NOT NULL ORDER BY aggregate_sequence"
                )
            )
        ]
        outcome_labels = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT result_payload ->> 'label' FROM service_request_outcomes "
                    "WHERE result_payload ->> 'label' IS NOT NULL ORDER BY recorded_at"
                )
            )
        ]
        audit_labels = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT required_metadata ->> 'label' FROM invocation_audit_obligations "
                    "WHERE required_metadata ->> 'label' IS NOT NULL ORDER BY created_at"
                )
            )
        ]
        content_identities = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT content_identity FROM task_content_versions "
                    "ORDER BY created_at, content_version_id"
                )
            )
        ]
        migration_rows = [
            {
                "revision": str(row[0]),
                "migration_code_sha256": str(row[1]),
                "outcome": str(row[2]),
            }
            for row in connection.execute(
                text(
                    "SELECT revision, migration_code_sha256, outcome "
                    "FROM applied_migration_events ORDER BY started_at, migration_event_id"
                )
            )
        ]
        return {
            "schema_head": head,
            "authoritative_content": int(
                connection.scalar(text("SELECT count(*) FROM task_content_versions")) or 0
            ),
            "request_outcomes": int(
                connection.scalar(text("SELECT count(*) FROM service_request_outcomes")) or 0
            ),
            "audit_obligations": int(
                connection.scalar(text("SELECT count(*) FROM invocation_audit_obligations"))
                or 0
            ),
            "leases": int(
                connection.scalar(text("SELECT count(*) FROM service_leases")) or 0
            ),
            "projection_state": int(
                connection.scalar(text("SELECT count(*) FROM projection_outbox_events")) or 0
            ),
            "release_evidence": int(
                connection.scalar(text("SELECT count(*) FROM release_evidence_items")) or 0
            ),
            "migration_provenance": int(
                connection.scalar(text("SELECT count(*) FROM applied_migration_events")) or 0
            ),
            "release_labels": release_labels,
            "projection_labels": projection_labels,
            "outcome_labels": outcome_labels,
            "audit_labels": audit_labels,
            "content_identities": content_identities,
            "migration_rows": migration_rows,
        }


def _verify_state(
    engine: Engine,
    expected_labels: Iterable[str],
    absent_labels: Iterable[str],
) -> dict[str, Any]:
    observed = _counts(engine)
    expected = set(expected_labels)
    absent = set(absent_labels)
    release_labels = set(observed["release_labels"])
    projection_labels = set(observed["projection_labels"])
    failures: list[str] = []
    if observed["schema_head"] != ALEMBIC_HEAD:
        failures.append("schema head mismatch")
    if REPRESENTATIVE_CONTENT_IDENTITY not in observed["content_identities"]:
        failures.append("representative authoritative content identity mismatch")
    expected_migration_hash = migration_revision_sha256(ALEMBIC_HEAD)
    if not any(
        row["revision"] == ALEMBIC_HEAD
        and row["migration_code_sha256"] == expected_migration_hash
        and row["outcome"] in {"applied", "stamp"}
        for row in observed["migration_rows"]
    ):
        failures.append("Alembic migration provenance mismatch")
    for key in (
        "authoritative_content",
        "request_outcomes",
        "audit_obligations",
        "leases",
        "projection_state",
        "release_evidence",
        "migration_provenance",
    ):
        if observed[key] < 1:
            failures.append(f"missing {key}")
    for evidence_kind, labels in (
        ("release", release_labels),
        ("projection", projection_labels),
    ):
        if not expected.issubset(labels):
            failures.append(
                f"missing {evidence_kind} labels {sorted(expected - labels)}"
            )
        if labels.intersection(absent):
            failures.append(
                f"unexpected {evidence_kind} labels {sorted(labels.intersection(absent))}"
            )
    for evidence_kind, labels in (
        ("request outcome", set(observed["outcome_labels"])),
        ("audit obligation", set(observed["audit_labels"])),
    ):
        if "baseline" not in labels:
            failures.append(f"missing baseline {evidence_kind} preservation evidence")
        if labels.intersection(absent):
            failures.append(
                f"unexpected {evidence_kind} labels {sorted(labels.intersection(absent))}"
            )
    if failures:
        raise RehearsalError("restored state verification failed: " + "; ".join(failures))
    return observed


def _write_archive_helpers(root: Path, archive_dir: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    archive_dir.mkdir(parents=True, exist_ok=True)
    archive = root / "archive-wal"
    restore = root / "restore-wal"
    archive.write_text(
        "#!/usr/bin/env python3\n"
        "import filecmp\n"
        "import os\n"
        "import shutil\n"
        "import signal\n"
        "import sys\n"
        "from pathlib import Path\n"
        "signal.alarm(30)\n"
        f"archive_dir = Path({str(archive_dir)!r})\n"
        "source = Path(sys.argv[1])\n"
        "destination = archive_dir / sys.argv[2]\n"
        "if destination.exists():\n"
        "    raise SystemExit(0 if filecmp.cmp(source, destination, shallow=False) else 1)\n"
        "temporary = destination.with_name(f'{destination.name}.tmp.{os.getpid()}')\n"
        "with source.open('rb') as source_handle, temporary.open('wb') as target_handle:\n"
        "    shutil.copyfileobj(source_handle, target_handle)\n"
        "    target_handle.flush()\n"
        "    os.fsync(target_handle.fileno())\n"
        "os.replace(temporary, destination)\n"
        "file_fd = os.open(destination, os.O_RDONLY)\n"
        "try:\n"
        "    os.fsync(file_fd)\n"
        "finally:\n"
        "    os.close(file_fd)\n"
        "directory_fd = os.open(archive_dir, os.O_RDONLY)\n"
        "try:\n"
        "    os.fsync(directory_fd)\n"
        "finally:\n"
        "    os.close(directory_fd)\n",
        encoding="utf-8",
    )
    restore.write_text(
        "#!/usr/bin/env python3\n"
        "import shutil\n"
        "import signal\n"
        "import sys\n"
        "from pathlib import Path\n"
        "signal.alarm(30)\n"
        f"archive_dir = Path({str(archive_dir)!r})\n"
        "source = archive_dir / sys.argv[1]\n"
        "destination = Path(sys.argv[2])\n"
        "shutil.copyfile(source, destination)\n",
        encoding="utf-8",
    )
    archive.chmod(0o700)
    restore.chmod(0o700)
    return archive, restore


@dataclass(frozen=True)
class BackupEvidence:
    system_identifier: str
    manifest_sha256: str
    evidence_sha256: str
    timeline_id: int
    start_lsn: str
    end_lsn: str
    pg_version: str

    def as_json(self) -> dict[str, object]:
        return asdict(self)


def _backup_evidence(path: Path, *, system_identifier: str) -> BackupEvidence:
    try:
        manifest = json.loads((path / "backup_manifest").read_text(encoding="utf-8"))
        pg_version = (path / "PG_VERSION").read_text(encoding="utf-8").strip()
    except (OSError, json.JSONDecodeError) as exc:
        raise RehearsalError("backup evidence is unreadable or invalid") from exc
    wal_ranges = manifest.get("WAL-Ranges")
    if not isinstance(wal_ranges, list) or len(wal_ranges) != 1:
        raise RehearsalError("backup manifest must contain one exact WAL range")
    wal_range = wal_ranges[0]
    try:
        timeline_id = int(wal_range["Timeline"])
        start_lsn = str(wal_range["Start-LSN"])
        end_lsn = str(wal_range["End-LSN"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RehearsalError("backup manifest WAL range is invalid") from exc
    if timeline_id <= 0 or not start_lsn or not end_lsn:
        raise RehearsalError("backup manifest WAL identity is incomplete")
    manifest_sha = sha256_file(path / "backup_manifest")
    payload = {
        "schema": "dish-section2-backup-evidence-v1",
        "system_identifier": system_identifier,
        "manifest_sha256": manifest_sha,
        "timeline_id": timeline_id,
        "start_lsn": start_lsn,
        "end_lsn": end_lsn,
        "pg_version": pg_version,
    }
    return BackupEvidence(
        system_identifier=system_identifier,
        manifest_sha256=manifest_sha,
        evidence_sha256=release_sha256_json(payload),
        timeline_id=timeline_id,
        start_lsn=start_lsn,
        end_lsn=end_lsn,
        pg_version=pg_version,
    )


def _verify_backup(
    runner: Runner,
    binaries: Mapping[str, Path],
    path: Path,
    *,
    system_identifier: str,
) -> BackupEvidence:
    required = (path / "PG_VERSION", path / "backup_manifest", path / "global", path / "base")
    missing = [str(item.name) for item in required if not item.exists()]
    if missing:
        raise RehearsalError("backup output incomplete: " + ",".join(missing))
    runner.run(
        [binaries["pg_verifybackup"], path],
        timeout_seconds=COMMAND_TIMEOUTS["pg_verifybackup"],
    )
    observed_system_identifier = _controldata_system_identifier(
        runner, binaries["pg_controldata"], path
    )
    if observed_system_identifier != system_identifier:
        raise RehearsalError(
            "backup control data belongs to a different PostgreSQL system identifier"
        )
    return _backup_evidence(path, system_identifier=system_identifier)


def _backup_reservation_path(parent: Path) -> Path:
    return parent / "backup-reservation.json"


def _write_reservation(
    parent: Path,
    *,
    candidate: Path,
    final: Path,
    system_identifier: str,
) -> None:
    atomic_json(
        _backup_reservation_path(parent),
        {
            "schema": "dish-section2-backup-reservation-v2",
            "state": "reserved",
            "candidate": str(candidate),
            "final": str(final),
            "system_identifier": system_identifier,
        },
    )


def finalize_backup(
    parent: Path,
    *,
    candidate: Path,
    final: Path,
    system_identifier: str,
    verifier: Callable[[Path], BackupEvidence],
    inject_after_rename: bool = False,
) -> BackupEvidence:
    """Durably finalize or reconcile one exact backup reservation."""
    reservation_path = _backup_reservation_path(parent)
    if not reservation_path.exists():
        _write_reservation(
            parent,
            candidate=candidate,
            final=final,
            system_identifier=system_identifier,
        )
    try:
        reservation = json.loads(reservation_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RehearsalError("backup reservation is unreadable or invalid") from exc
    if reservation.get("schema") != "dish-section2-backup-reservation-v2":
        raise RehearsalError("backup reservation schema is invalid")
    if reservation.get("state") not in {"reserved", "verified", "finalized"}:
        raise RehearsalError("backup reservation state is invalid")
    expected = {
        "candidate": str(candidate),
        "final": str(final),
        "system_identifier": system_identifier,
    }
    if any(reservation.get(key) != value for key, value in expected.items()):
        raise RehearsalError("stale backup reservation does not match the requested finalization")
    candidate_exists = candidate.exists()
    final_exists = final.exists()
    if reservation["state"] == "finalized":
        if candidate_exists or not final_exists:
            raise RehearsalError(
                "finalized backup reservation does not name one exact final output"
            )
        evidence = verifier(final)
        if reservation.get("backup_evidence") != evidence.as_json():
            raise RehearsalError(
                "finalized backup reservation evidence does not match final output"
            )
        return evidence
    if candidate_exists and final_exists:
        raise RehearsalError("ambiguous backup finalization: candidate and final both exist")
    if not candidate_exists and not final_exists:
        raise RehearsalError("partial backup reservation has neither candidate nor final output")
    reserved_evidence = reservation.get("backup_evidence")
    verified_candidate: BackupEvidence | None = None
    if reservation["state"] == "reserved":
        if final_exists:
            raise RehearsalError(
                "renamed backup lacks durable pre-rename verification evidence"
            )
        verified_candidate = verifier(candidate)
        atomic_json(
            reservation_path,
            {
                **expected,
                "schema": "dish-section2-backup-reservation-v2",
                "state": "verified",
                "backup_evidence": verified_candidate.as_json(),
            },
        )
        reserved_evidence = verified_candidate.as_json()
    elif not isinstance(reserved_evidence, dict):
        raise RehearsalError("verified backup reservation lacks exact backup evidence")
    if candidate_exists:
        evidence = verified_candidate or verifier(candidate)
        if evidence.as_json() != reserved_evidence:
            raise RehearsalError(
                "verified candidate evidence changed before backup rename"
            )
        os.replace(candidate, final)
        directory_fd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if inject_after_rename:
            raise InjectedFinalizationFault(
                "fault after backup rename before reservation finalization"
            )
    evidence = verifier(final)
    if evidence.as_json() != reserved_evidence:
        raise RehearsalError(
            "final backup evidence does not match the pre-rename verified candidate"
        )
    atomic_json(
        reservation_path,
        {
            **expected,
            "schema": "dish-section2-backup-reservation-v2",
            "state": "finalized",
            "backup_evidence": evidence.as_json(),
        },
    )
    return evidence


def _internal_reconcile_backup(
    request_path: Path, result_path: Path, *, inject_after_rename: bool
) -> int:
    result: dict[str, Any] = {
        "schema": "dish-section2-backup-reconcile-v1",
        "pid": os.getpid(),
        "status": "failed",
        "inject_after_rename": inject_after_rename,
    }
    runner: Runner | None = None
    try:
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        required = {
            "parent",
            "candidate",
            "final",
            "system_identifier",
            "pg_verifybackup",
            "pg_controldata",
            "log_dir",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise RehearsalError("backup reconciliation request fields mismatch")
        runner = Runner(Path(payload["log_dir"]) / result_path.stem)
        evidence = finalize_backup(
            Path(payload["parent"]),
            candidate=Path(payload["candidate"]),
            final=Path(payload["final"]),
            system_identifier=str(payload["system_identifier"]),
            verifier=lambda path: _verify_backup(
                runner,
                {
                    "pg_verifybackup": Path(payload["pg_verifybackup"]),
                    "pg_controldata": Path(payload["pg_controldata"]),
                },
                path,
                system_identifier=str(payload["system_identifier"]),
            ),
            inject_after_rename=inject_after_rename,
        )
        result.update(
            {
                "status": "passed",
                "backup_evidence": evidence.as_json(),
                "commands": [asdict(item) for item in runner.commands],
            }
        )
        return_code = 0
    except InjectedFinalizationFault as exc:
        result.update({"status": "interrupted_after_rename", "error": str(exc)})
        return_code = 75
    except BaseException as exc:
        result.update({"status": "failed", "error": str(exc)})
        return_code = 2
    if runner is not None:
        result["commands"] = [asdict(item) for item in runner.commands]
    atomic_json(result_path, result)
    return return_code


def _run_backup_reconcile_process(
    runner: Runner,
    *,
    request_path: Path,
    result_path: Path,
    inject_after_rename: bool,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    if result_path.exists():
        raise RehearsalError(
            "restart reconciliation result path already exists; refusing stale evidence"
        )
    argv: list[str | Path] = [
        sys.executable,
        ROOT / "scripts" / "dish-pg-recovery-rehearsal",
        "--internal-reconcile-backup",
        request_path,
        result_path,
    ]
    if inject_after_rename:
        argv.append("--inject-after-rename")
    completed = runner.run(
        argv,
        timeout_seconds=COMMAND_TIMEOUTS["restart_reconcile"],
        check=False,
    )
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RehearsalError("restart reconciliation process omitted a valid result") from exc
    nested_commands = result.get("commands", [])
    if not isinstance(nested_commands, list):
        raise RehearsalError("restart reconciliation result commands are invalid")
    for item in nested_commands:
        if not isinstance(item, dict):
            raise RehearsalError("restart reconciliation command evidence is invalid")
        try:
            runner.commands.append(CommandEvidence(**item))
        except TypeError as exc:
            raise RehearsalError(
                "restart reconciliation command evidence fields mismatch"
            ) from exc
    return completed, result


def _physical_backup(
    source: Cluster,
    backup_parent: Path,
    system_identifier: str,
    *,
    inject_rename_fault: bool,
) -> tuple[Path, float, BackupEvidence, dict[str, Any]]:
    backup_parent.mkdir(parents=True, exist_ok=True)
    candidate = backup_parent / "candidate"
    final = backup_parent / "final"
    reservation = _backup_reservation_path(backup_parent)
    if candidate.exists() or final.exists() or reservation.exists():
        raise RehearsalError(
            "backup startup found existing candidate/final/reservation state; "
            "reconcile it explicitly instead of deleting it"
        )
    _write_reservation(
        backup_parent,
        candidate=candidate,
        final=final,
        system_identifier=system_identifier,
    )
    started = time.perf_counter()
    source.runner.run(
        [
            source.binaries["pg_basebackup"],
            "-h",
            "127.0.0.1",
            "-p",
            str(source.port),
            "-U",
            "dish",
            "-D",
            candidate,
            "--format=plain",
            "--wal-method=stream",
            "--checkpoint=fast",
            "--manifest-checksums=SHA256",
        ],
        timeout_seconds=COMMAND_TIMEOUTS["pg_basebackup"],
    )
    request_path = backup_parent / "reconcile-request.json"
    first_result_path = backup_parent / "rename-process-result.json"
    second_result_path = backup_parent / "restart-process-result.json"
    atomic_json(
        request_path,
        {
            "parent": str(backup_parent),
            "candidate": str(candidate),
            "final": str(final),
            "system_identifier": system_identifier,
            "pg_verifybackup": str(source.binaries["pg_verifybackup"]),
            "pg_controldata": str(source.binaries["pg_controldata"]),
            "log_dir": str(source.runner.log_dir / "restart-reconcile-child"),
        },
    )
    first, first_result = _run_backup_reconcile_process(
        source.runner,
        request_path=request_path,
        result_path=first_result_path,
        inject_after_rename=inject_rename_fault,
    )
    if inject_rename_fault:
        if first.returncode != 75 or first_result.get("status") != "interrupted_after_rename":
            raise RehearsalError("rename process did not terminate at the required boundary")
        if candidate.exists() or not final.exists():
            raise RehearsalError("rename interruption did not leave one exact final candidate")
    elif first.returncode != 0:
        raise RehearsalError("backup finalization process failed")
    second, second_result = _run_backup_reconcile_process(
        source.runner,
        request_path=request_path,
        result_path=second_result_path,
        inject_after_rename=False,
    )
    if second.returncode != 0 or second_result.get("status") != "passed":
        raise RehearsalError("restart reconciliation failed to validate exact final backup")
    if first_result.get("pid") == second_result.get("pid"):
        raise RehearsalError("rename fault and reconciliation did not cross a process boundary")
    evidence = BackupEvidence(**second_result["backup_evidence"])
    restart_evidence = {
        "process_boundary_proven": True,
        "rename_process": first_result,
        "restart_process": second_result,
        "same_process_fault_handling": False,
    }
    return final, time.perf_counter() - started, evidence, restart_evidence


def _copy_backup(backup: Path, target: Path) -> float:
    require_empty_target(target)
    started = time.perf_counter()
    shutil.copytree(backup, target, dirs_exist_ok=True, symlinks=True)
    return time.perf_counter() - started


def _configure_restored_cluster(cluster: Cluster) -> None:
    cluster.socket_dir.mkdir(parents=True, exist_ok=True)
    cluster._append_config(primary=False)
    (cluster.data_dir / "standby.signal").unlink(missing_ok=True)
    (cluster.data_dir / "recovery.signal").unlink(missing_ok=True)


def _configure_pitr(cluster: Cluster, restore_helper: Path, target_lsn: str) -> None:
    cluster.socket_dir.mkdir(parents=True, exist_ok=True)
    with (cluster.data_dir / "postgresql.conf").open("a", encoding="utf-8") as handle:
        handle.write("\n# dish Section 2 PITR target\n")
        handle.write(f"port = {cluster.port}\n")
        handle.write("listen_addresses = '127.0.0.1'\n")
        handle.write(f"unix_socket_directories = '{cluster.socket_dir}'\n")
        handle.write("archive_mode = off\n")
        handle.write("logging_collector = on\n")
        handle.write(f"log_directory = '{cluster.data_dir / 'log'}'\n")
        handle.write("log_filename = 'postgresql.log'\n")
        handle.write(f"restore_command = '{restore_helper} %f %p'\n")
        handle.write(f"recovery_target_lsn = '{target_lsn}'\n")
        handle.write("recovery_target_inclusive = on\n")
        handle.write("recovery_target_action = 'promote'\n")
    (cluster.data_dir / "recovery.signal").touch()


def _wait_for_promotion(
    engine: Engine, target_lsn: str, *, timeout_seconds: float = 30.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last_replayed: str | None = None
    while time.monotonic() < deadline:
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT pg_is_in_recovery(), "
                    "pg_last_wal_replay_lsn()::text"
                )
            ).one()
        recovering = bool(row[0])
        last_replayed = None if row[1] is None else str(row[1])
        if not recovering:
            with engine.connect() as connection:
                delta = connection.scalar(
                    text(
                        "SELECT pg_wal_lsn_diff("
                        "pg_last_wal_replay_lsn(), CAST(:target_lsn AS pg_lsn))"
                    ),
                    {"target_lsn": target_lsn},
                )
            if delta is None or float(delta) < 0:
                raise RehearsalError(
                    "PITR promoted before replaying the selected recovery target"
                )
            return {
                "promoted": True,
                "target_lsn": target_lsn,
                "last_replayed_lsn": last_replayed,
                "replay_delta_bytes": float(delta),
            }
        time.sleep(0.05)
    raise RehearsalError(
        f"PITR target did not promote within {timeout_seconds:.1f}s; "
        f"last_replayed_lsn={last_replayed}"
    )


def _observe_recovered_target(
    engine: Engine,
    *,
    backup_evidence: BackupEvidence,
    recovery_target_type: str,
    recovery_target_lsn: str,
) -> RecoveredPhysicalState:
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT current_database(), "
                "(SELECT system_identifier::text FROM pg_control_system()), "
                "(SELECT version_num FROM alembic_version), "
                "(pg_control_checkpoint()).timeline_id, "
                "pg_current_wal_lsn()::text, "
                "current_setting('data_directory'), "
                "pg_postmaster_start_time(), "
                "inet_server_port()"
            )
        ).one()
    started_at = row[6]
    if not isinstance(started_at, datetime):
        raise RehearsalError("recovered target omitted postmaster start identity")
    target_instance = {
        "data_directory": str(Path(str(row[5])).resolve()),
        "postmaster_started_at": iso(started_at),
        "server_port": int(row[7]),
    }
    return RecoveredPhysicalState(
        database_name=str(row[0]),
        system_identifier=str(row[1]),
        schema_head=str(row[2]),
        backup_manifest_sha256=backup_evidence.manifest_sha256,
        backup_evidence_sha256=backup_evidence.evidence_sha256,
        recovery_timeline_id=int(row[3]),
        recovery_target_type=recovery_target_type,
        recovery_target_lsn=recovery_target_lsn,
        recovery_completion_lsn=str(row[4]),
        recovery_target_instance_sha256=release_sha256_json(target_instance),
    )


def _control_payload(
    context: SeedContext, recovered_state: RecoveredPhysicalState
) -> dict[str, Any]:
    digest = hashlib.sha256(b"section2-post-restore-current-actor").hexdigest()
    return {
        "external_control_id": f"section2-control-{uuid.uuid4()}",
        "predecessor_generation_id": str(context.generation_id),
        "generation_id": str(uuid.uuid4()),
        "bootstrap_id": str(uuid.uuid4()),
        "bootstrap_capability_sha256": digest,
        "expected_database_name": recovered_state.database_name,
        "expected_system_identifier": recovered_state.system_identifier,
        "schema_head": recovered_state.schema_head,
        "dish_release": context.dish_release,
        "honest_release": "honest-pantry@" + "0" * 40,
        "protocol_release": context.protocol_release,
        "openapi_release": "section2-openapi",
        "routing_release": "section2-routing",
        "backup_manifest_sha256": recovered_state.backup_manifest_sha256,
        "backup_evidence_sha256": recovered_state.backup_evidence_sha256,
        "recovery_timeline_id": recovered_state.recovery_timeline_id,
        "recovery_target_type": recovered_state.recovery_target_type,
        "recovery_target_lsn": recovered_state.recovery_target_lsn,
        "recovery_completion_lsn": recovered_state.recovery_completion_lsn,
        "recovery_target_instance_sha256": (
            recovered_state.recovery_target_instance_sha256
        ),
        "recovery_evidence_sha256": recovered_state.evidence_sha256,
        "issued_at": iso(utc_now()),
    }


def _assert_promotion_fault(
    session: Session,
    control: RestoreControl,
    recovered_state: RecoveredPhysicalState,
    *,
    name: str,
    faults: dict[str, Any],
) -> None:
    try:
        promote_restored_generation(session, control, recovered_state=recovered_state)
    except RestoreControlError as exc:
        faults[name] = {"passed": True, "error": str(exc)}
    else:
        raise RehearsalError(f"{name} did not fail closed")


def _exercise_preburn_rejection(
    engine: Engine,
    control_path: Path,
    *,
    recovered_state: RecoveredPhysicalState,
) -> dict[str, Any]:
    control = load_restore_control(control_path)
    factory = session_factory(engine)
    with session_scope(factory) as session:
        try:
            promote_restored_generation(
                session, control, recovered_state=recovered_state
            )
        except RestoreControlError as exc:
            if "not rollback-burned: approved" not in str(exc):
                raise RehearsalError(
                    "pre-burn recovery rejected for the wrong authority reason: " + str(exc)
                ) from exc
            return {"passed": True, "error": str(exc)}
    raise RehearsalError("pre-burn recovery point incorrectly authorized promotion")


def _exercise_promotion_once(
    engine: Engine,
    control_path: Path,
    *,
    recovered_state: RecoveredPhysicalState,
) -> dict[str, str]:
    control = load_restore_control(control_path)
    factory = session_factory(engine)
    with session_scope(factory) as session:
        return promote_restored_generation(
            session, control, recovered_state=recovered_state
        ).as_json()


def _exercise_promotion(
    engine: Engine,
    control_path: Path,
    *,
    recovered_state: RecoveredPhysicalState,
    context: SeedContext,
) -> dict[str, Any]:
    faults: dict[str, Any] = {}
    try:
        load_restore_control(control_path.with_name("unavailable-control.json"))
    except RestoreControlError as exc:
        faults["unavailable_external_restore_control"] = {"passed": True, "error": str(exc)}
    else:
        raise RehearsalError("unavailable external restore control did not fail closed")
    control = load_restore_control(control_path)
    factory = session_factory(engine)
    with session_scope(factory) as session:
        physical_faults = {
            "wrong_database": replace(recovered_state, database_name="dish_service_profile"),
            "wrong_source_cluster": replace(recovered_state, system_identifier="0"),
            "wrong_backup_manifest": replace(
                recovered_state, backup_manifest_sha256="0" * 64
            ),
            "wrong_backup_evidence": replace(
                recovered_state, backup_evidence_sha256="1" * 64
            ),
            "wrong_recovery_timeline": replace(
                recovered_state, recovery_timeline_id=recovered_state.recovery_timeline_id + 1
            ),
            "wrong_recovery_target_type": replace(
                recovered_state,
                recovery_target_type=(
                    "lsn"
                    if recovered_state.recovery_target_type == "backup_end"
                    else "backup_end"
                ),
            ),
            "wrong_recovery_target": replace(
                recovered_state, recovery_target_lsn="0/0"
            ),
            "wrong_recovery_completion": replace(
                recovered_state, recovery_completion_lsn="0/0"
            ),
            "wrong_recovery_target_instance": replace(
                recovered_state, recovery_target_instance_sha256="2" * 64
            ),
        }
        for name, state in physical_faults.items():
            _assert_promotion_fault(
                session, control, state, name=name, faults=faults
            )
        wrong_generation = replace(
            control, predecessor_generation_id=uuid.uuid4()
        )
        _assert_promotion_fault(
            session,
            wrong_generation,
            recovered_state,
            name="wrong_generation",
            faults=faults,
        )
        wrong_release = replace(control, protocol_release="wrong-protocol-release")
        _assert_promotion_fault(
            session,
            wrong_release,
            recovered_state,
            name="unauthorized_release_coordinates",
            faults=faults,
        )
        result = promote_restored_generation(
            session, control, recovered_state=recovered_state
        )
        workflow = WorkflowAuthorityService(session)
        try:
            workflow.register_run(
                run_id=uuid.uuid4(),
                generation_id=context.generation_id,
                owner_id="restored-stale-run",
                agent="service",
                capability_digest=hashlib.sha256(b"stale").digest(),
                registered_at=utc_now(),
            )
        except StaleAuthorityError as exc:
            faults["pre_restore_run_fenced"] = {"passed": True, "error": str(exc)}
        else:
            raise RehearsalError("pre-restore run regained authority")
        try:
            workflow.admit_request(
                RequestSpec(
                    request_id=uuid.uuid4(),
                    generation_id=context.generation_id,
                    run_id=context.run_id,
                    owner_id="section2-pre-restore-service",
                    principal_class="service",
                    command_name="section2_stale_request",
                    canonical_payload={"restored": True},
                    protocol_release=context.protocol_release,
                    dish_release=context.dish_release,
                    admitted_at=utc_now(),
                )
            )
        except StaleAuthorityError as exc:
            faults["pre_restore_request_context_fenced"] = {
                "passed": True,
                "error": str(exc),
            }
        else:
            raise RehearsalError("pre-restore request context regained authority")
        try:
            workflow.register_run(
                run_id=uuid.uuid4(),
                generation_id=control.generation_id,
                owner_id="stale-self-registration",
                agent="service",
                capability_digest=hashlib.sha256(b"stale").digest(),
                registered_at=utc_now(),
            )
        except StaleAuthorityError as exc:
            faults["stale_client_self_registration"] = {"passed": True, "error": str(exc)}
        else:
            raise RehearsalError("stale client self-registered into restored generation")
        try:
            workflow.register_run(
                run_id=uuid.uuid4(),
                generation_id=control.generation_id,
                owner_id="wrong-bootstrap-capability",
                agent="service",
                capability_digest=hashlib.sha256(b"wrong-bootstrap-capability").digest(),
                bootstrap_id=control.bootstrap_id,
                registered_at=utc_now(),
            )
        except StaleAuthorityError as exc:
            faults["wrong_bootstrap_capability"] = {"passed": True, "error": str(exc)}
        else:
            raise RehearsalError("wrong bootstrap capability was accepted")
        current_run = workflow.register_run(
            run_id=uuid.uuid4(),
            generation_id=control.generation_id,
            owner_id="section2-current-actor",
            agent="service",
            capability_digest=control.bootstrap_capability_digest,
            bootstrap_id=control.bootstrap_id,
            registered_at=utc_now(),
        )
        try:
            workflow.register_run(
                run_id=uuid.uuid4(),
                generation_id=control.generation_id,
                owner_id="replayed-bootstrap",
                agent="service",
                capability_digest=control.bootstrap_capability_digest,
                bootstrap_id=control.bootstrap_id,
                registered_at=utc_now(),
            )
        except StaleAuthorityError as exc:
            faults["bootstrap_replay_fenced"] = {"passed": True, "error": str(exc)}
        else:
            raise RehearsalError("consumed restore bootstrap was replayed")
        try:
            workflow.renew_lease(
                lease_id=context.lease_id,
                execution_id=context.execution_id,
                run_id=context.run_id,
                owner_id="section2-pre-restore-service",
                now=utc_now(),
                new_expiry=utc_now() + timedelta(days=366),
            )
        except StaleAuthorityError as exc:
            faults["pre_restore_lease_fenced"] = {"passed": True, "error": str(exc)}
        else:
            raise RehearsalError("restored pre-restore lease was renewed")
        try:
            workflow.admit_request(
                RequestSpec(
                    request_id=uuid.uuid4(),
                    generation_id=control.generation_id,
                    run_id=current_run.run_id,
                    owner_id=current_run.owner_id,
                    principal_class="service",
                    command_name="section2_post_restore_request",
                    canonical_payload={"deliberate_reissue": False},
                    protocol_release=context.protocol_release,
                    dish_release=context.dish_release,
                    admitted_at=utc_now(),
                )
            )
        except MutationAdmissionClosed as exc:
            faults["post_restore_mutation_admission_closed"] = {
                "passed": True,
                "error": str(exc),
            }
        else:
            raise RehearsalError(
                "restored generation admitted mutation before deliberate reissue control"
            )
        old_lease = session.scalar(
            select(wf.ServiceLease).where(
                wf.ServiceLease.generation_id == context.generation_id
            )
        )
        old_epoch = session.scalar(
            select(transition_models.ProjectionEpoch).where(
                transition_models.ProjectionEpoch.generation_id == context.generation_id
            )
        )
        stale_claim = ProjectionService(session).claim_next(
            worker_id="section2-restored-stale-worker",
            now=utc_now(),
            ttl=timedelta(minutes=1),
        )
        if stale_claim is not None:
            raise RehearsalError("restored projection worker regained external-effect authority")
        faults["pre_restore_projection_worker_fenced"] = {
            "passed": True,
            "old_epoch_status": None if old_epoch is None else old_epoch.status,
        }
        return {
            "promotion": result.as_json(),
            "recovered_physical_state": recovered_state.evidence_payload(),
            "recovery_evidence_sha256": recovered_state.evidence_sha256,
            "current_run_id": str(current_run.run_id),
            "bootstrap_consumed": True,
            "mutation_admission": "closed_pending_deliberate_reissue_control",
            "old_lease_restored_but_stale": old_lease is not None,
            "old_projection_epoch_status": None if old_epoch is None else old_epoch.status,
            "faults": faults,
        }


def _copy_backup_with_interruption(backup: Path, target: Path, *, stop_after_files: int = 8) -> int:
    require_empty_target(target)
    copied = 0
    for source in sorted(backup.rglob("*")):
        relative = source.relative_to(backup)
        destination = target / relative
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.is_symlink():
            destination.symlink_to(os.readlink(source))
        else:
            shutil.copy2(source, destination)
        copied += 1
        if copied >= stop_after_files:
            raise InjectedRestoreFault(
                f"fault after copying {copied} backup files before restore finalization"
            )
    raise RehearsalError("restore interruption point was not reached")


def _fault_backup_validation(
    runner: Runner,
    binaries: Mapping[str, Path],
    backup: Path,
    faults_dir: Path,
    *,
    system_identifier: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    corrupt = faults_dir / "corrupt-backup"
    shutil.copytree(backup, corrupt)
    with (corrupt / "backup_manifest").open("ab") as handle:
        handle.write(b"\ncorrupt\n")
    try:
        _verify_backup(runner, binaries, corrupt, system_identifier=system_identifier)
    except RehearsalError as exc:
        result["corrupt_backup"] = {"passed": True, "error": str(exc)}
    else:
        raise RehearsalError("corrupt backup was accepted")

    interrupted = faults_dir / "interrupted-restore"
    try:
        _copy_backup_with_interruption(backup, interrupted)
    except InjectedRestoreFault as interruption:
        try:
            _verify_backup(runner, binaries, interrupted, system_identifier=system_identifier)
        except RehearsalError as exc:
            result["interrupted_restore"] = {
                "passed": True,
                "interruption": str(interruption),
                "validation_error": str(exc),
            }
        else:
            raise RehearsalError("interrupted restore output was accepted")
    else:
        raise RehearsalError("restore interruption fault was not injected")

    unexpected = faults_dir / "unexpected-target"
    unexpected.mkdir(parents=True)
    (unexpected / "foreign-state").write_text("not owned\n", encoding="utf-8")
    try:
        require_empty_target(unexpected)
    except RehearsalError as exc:
        result["unexpected_target_state"] = {"passed": True, "error": str(exc)}
    else:
        raise RehearsalError("unexpected restore target state was accepted")

    reservation_faults = faults_dir / "reservation-faults"
    reservation_faults.mkdir(parents=True)
    stale = reservation_faults / "stale"
    stale.mkdir()
    stale_candidate, stale_final = stale / "candidate", stale / "final"
    stale_candidate.mkdir()
    _write_reservation(
        stale,
        candidate=stale_candidate,
        final=stale_final,
        system_identifier="wrong-source-system-identifier",
    )
    try:
        finalize_backup(
            stale,
            candidate=stale_candidate,
            final=stale_final,
            system_identifier=system_identifier,
            verifier=lambda _path: None,
        )
    except RehearsalError as exc:
        result["stale_backup_reservation"] = {"passed": True, "error": str(exc)}
    else:
        raise RehearsalError("stale backup reservation was accepted")

    partial = reservation_faults / "partial"
    partial.mkdir()
    partial_candidate, partial_final = partial / "candidate", partial / "final"
    _write_reservation(
        partial,
        candidate=partial_candidate,
        final=partial_final,
        system_identifier=system_identifier,
    )
    try:
        finalize_backup(
            partial,
            candidate=partial_candidate,
            final=partial_final,
            system_identifier=system_identifier,
            verifier=lambda _path: None,
        )
    except RehearsalError as exc:
        result["partial_backup_reservation"] = {"passed": True, "error": str(exc)}
    else:
        raise RehearsalError("partial backup reservation was accepted")

    ambiguous = reservation_faults / "ambiguous"
    ambiguous.mkdir()
    ambiguous_candidate, ambiguous_final = ambiguous / "candidate", ambiguous / "final"
    ambiguous_candidate.mkdir()
    ambiguous_final.mkdir()
    _write_reservation(
        ambiguous,
        candidate=ambiguous_candidate,
        final=ambiguous_final,
        system_identifier=system_identifier,
    )
    try:
        finalize_backup(
            ambiguous,
            candidate=ambiguous_candidate,
            final=ambiguous_final,
            system_identifier=system_identifier,
            verifier=lambda _path: None,
        )
    except RehearsalError as exc:
        result["ambiguous_backup_reservation"] = {"passed": True, "error": str(exc)}
    else:
        raise RehearsalError("ambiguous backup reservation was accepted")
    return result


def _interrupted_backup_fault(
    source: Cluster, target: Path, *, timeout_seconds: float = 10.0
) -> dict[str, Any]:
    shutil.rmtree(target, ignore_errors=True)
    argv = [
        str(source.binaries["pg_basebackup"]),
        "-h",
        "127.0.0.1",
        "-p",
        str(source.port),
        "-U",
        "dish",
        "-D",
        str(target),
        "--format=plain",
        "--wal-method=stream",
        "--checkpoint=fast",
        "--max-rate=32k",
    ]
    source.runner.counter += 1
    stdout_path = source.runner.log_dir / f"{source.runner.counter:03d}-stdout.log"
    stderr_path = source.runner.log_dir / f"{source.runner.counter:03d}-stderr.log"
    started_at = utc_now()
    started = time.perf_counter()
    termination = "not_started"
    cleanup_result = "not_started"
    with stdout_path.open("w", encoding="utf-8") as stdout_handle, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr_handle:
        process = subprocess.Popen(
            argv,
            stdout=stdout_handle,
            stderr=stderr_handle,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout_seconds
        reached_injection = False
        while time.monotonic() < deadline and process.poll() is None:
            if target.exists() and any(target.rglob("*")):
                reached_injection = True
                break
            time.sleep(0.05)
        timed_out = process.poll() is None and not reached_injection
        if process.poll() is None:
            termination, cleanup_result = source.runner._terminate_group(process)
        else:
            termination, cleanup_result = "exited_before_injection", "exited"
    duration = time.perf_counter() - started
    source.runner.commands.append(
        CommandEvidence(
            argv=_redact_argv(argv),
            process_group_id=process.pid,
            started_at=iso(started_at),
            duration_seconds=duration,
            timeout_seconds=timeout_seconds,
            returncode=process.returncode,
            stdout_log=str(stdout_path),
            stderr_log=str(stderr_path),
            timed_out=timed_out,
            termination=termination,
            cleanup_result=cleanup_result,
        )
    )
    if timed_out:
        raise CommandTimeout(
            f"interrupted-backup command timed out before the deterministic injection "
            f"boundary after {timeout_seconds:.1f}s; termination={termination}; "
            f"stderr={stderr_path}"
        )
    if termination == "exited_before_injection":
        raise RehearsalError(
            "interrupted-backup command exited before the deterministic injection boundary; "
            f"returncode={process.returncode} stderr={stderr_path}"
        )
    try:
        _verify_backup(
            source.runner,
            source.binaries,
            target,
            system_identifier=source.system_identifier(),
        )
    except RehearsalError as exc:
        return {
            "passed": True,
            "command": _redact_argv(argv),
            "started_at": iso(started_at),
            "duration_seconds": duration,
            "returncode": process.returncode,
            "termination": termination,
            "cleanup_result": cleanup_result,
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
            "error": str(exc),
        }
    raise RehearsalError("interrupted backup was accepted as valid")


def _source_manifest() -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    for relative in SOURCE_IDENTITY_PATHS:
        path = ROOT / relative
        if not path.is_file():
            raise RehearsalError(f"source identity path is missing: {relative}")
        entries.append(
            {
                "path": relative,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    payload = {
        "schema": "dish-section2-source-manifest-v1",
        "alembic_head": ALEMBIC_HEAD,
        "files": entries,
    }
    return {**payload, "manifest_sha256": release_sha256_json(payload)}


def _verified_source_identity(
    runner: Runner, args: argparse.Namespace
) -> dict[str, Any]:
    manifest = _source_manifest()
    repository = ROOT.parent
    identity: dict[str, Any] = {
        "source_manifest": manifest,
        "caller_identity_is_authoritative": False,
    }
    if not (repository / ".git").exists():
        if args.dish_commit or args.base_commit:
            raise RehearsalError(
                "caller commit identities cannot be verified because Git metadata is absent"
            )
        if args.source_identity_kind == "git_commit":
            raise RehearsalError(
                "git_commit source identity cannot be verified because Git metadata is absent"
            )
        identity.update(
            {
                "kind": "source_manifest",
                "git_metadata_present": False,
                "execution_identity": manifest["manifest_sha256"],
                "relevant_tree_identity": manifest["manifest_sha256"],
                "base_identity_kind": args.source_identity_kind or "source_manifest",
            }
        )
        return identity

    def git(*git_args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return runner.run(
            ["git", "-c", f"safe.directory={repository}", "-C", repository, *git_args],
            timeout_seconds=COMMAND_TIMEOUTS["git"],
            check=check,
        )

    head = git("rev-parse", "HEAD").stdout.strip()
    tree = git("rev-parse", "HEAD^{tree}").stdout.strip()
    parent_result = git("rev-parse", "HEAD^", check=False)
    parent = parent_result.stdout.strip() if parent_result.returncode == 0 else None
    relevant_paths = [f"dish/{relative}" for relative in SOURCE_IDENTITY_PATHS]
    worktree_status = git("status", "--porcelain=v1").stdout.splitlines()
    relevant_status = git(
        "status", "--porcelain=v1", "--", *relevant_paths
    ).stdout.splitlines()
    if args.dish_commit is not None and args.dish_commit != head:
        raise RehearsalError(
            f"--dish-commit does not match executed Git commit: expected {args.dish_commit}, observed {head}"
        )
    if args.base_commit is not None and args.base_commit != parent:
        raise RehearsalError(
            f"--base-commit does not match executed Git parent: expected {args.base_commit}, observed {parent}"
        )
    if args.source_identity_kind == "git_commit" and args.dish_commit is None:
        raise RehearsalError("git_commit source identity requires --dish-commit")
    if args.source_identity_kind == "synthetic_base" and args.base_commit is None:
        raise RehearsalError("synthetic_base source identity requires --base-commit")
    identity.update(
        {
            "kind": "git_worktree",
            "git_metadata_present": True,
            "current_commit": head,
            "current_tree": tree,
            "parent_commit": parent,
            "base_identity_kind": args.source_identity_kind,
            "worktree_clean": not worktree_status,
            "worktree_status": worktree_status,
            "relevant_worktree_clean": not relevant_status,
            "relevant_worktree_status": relevant_status,
            "execution_identity": manifest["manifest_sha256"],
            "relevant_tree_identity": manifest["manifest_sha256"],
            "caller_expectations": {
                "dish_commit": args.dish_commit,
                "base_commit": args.base_commit,
                "source_identity_kind": args.source_identity_kind,
                "dish_commit_matches_head": (
                    None if args.dish_commit is None else args.dish_commit == head
                ),
                "base_commit_matches_parent": (
                    None if args.base_commit is None else args.base_commit == parent
                ),
                "declared_identity_binding_verified": (
                    None if args.source_identity_kind is None else True
                ),
                "caller_values_are_execution_identity": False,
            },
        }
    )
    return identity


def _postgres_version(runner: Runner, binaries: Mapping[str, Path]) -> str:
    result = runner.run(
        [binaries["postgres"], "--version"],
        timeout_seconds=COMMAND_TIMEOUTS["version"],
        env={"LC_ALL": "C"},
    )
    version = result.stdout.strip()
    match = re.search(r"PostgreSQL\) (\d+\.\d+)(?:\s|$)", version)
    if match is None or match.group(1) != "17.10":
        raise RehearsalError(
            f"PostgreSQL 17.10 is required by the current deployment; got {version}"
        )
    return version


def _cluster_manual_cleanup(cluster: Cluster) -> list[str]:
    details = [str(cluster.data_dir)]
    pid_path = cluster.data_dir / "postmaster.pid"
    try:
        pid = int(pid_path.read_text(encoding="utf-8").splitlines()[0])
    except (OSError, ValueError, IndexError):
        pid = None
    if pid is not None:
        details.append(
            "postgresql-server:"
            f"{cluster.name}:pid={pid}:port={cluster.port}:data_dir={cluster.data_dir}"
        )
    return details


def _cleanup_rehearsal(
    *,
    clusters: Sequence[Cluster],
    evidence_dir: Path,
    work_root: Path,
    keep_resources: bool,
    report: dict[str, Any],
) -> None:
    cleanup: list[dict[str, Any]] = []
    manual_cleanup: list[str] = []
    unsafe_to_remove = False
    for cluster in reversed(clusters):
        try:
            cluster.stop()
            cleanup.append({"resource": cluster.name, "result": "stopped"})
        except BaseException as exc:
            unsafe_to_remove = True
            cleanup.append(
                {
                    "resource": cluster.name,
                    "result": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            manual_cleanup.extend(_cluster_manual_cleanup(cluster))
    try:
        report["postgresql_logs"] = _preserve_postgresql_logs(clusters, evidence_dir)
    except BaseException as exc:
        report["postgresql_logs"] = []
        unsafe_to_remove = True
        cleanup.append(
            {
                "resource": "postgresql_logs",
                "result": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        manual_cleanup.append(str(work_root))
    if keep_resources:
        manual_cleanup.append(str(work_root))
        cleanup.append({"resource": str(work_root), "result": "retained_by_request"})
    elif unsafe_to_remove:
        manual_cleanup.append(str(work_root))
        cleanup.append(
            {"resource": str(work_root), "result": "retained_due_to_cleanup_failure"}
        )
    else:
        try:
            shutil.rmtree(work_root)
            cleanup.append({"resource": str(work_root), "result": "removed"})
        except FileNotFoundError:
            cleanup.append({"resource": str(work_root), "result": "already_absent"})
        except BaseException as exc:
            unsafe_to_remove = True
            cleanup.append(
                {
                    "resource": str(work_root),
                    "result": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            manual_cleanup.append(str(work_root))
    report["cleanup"] = cleanup
    report["manual_cleanup"] = sorted(set(manual_cleanup))
    if unsafe_to_remove and report.get("status") == "passed":
        report["status"] = "failed"
        report["error"] = {
            "type": "RehearsalError",
            "message": "rehearsal cleanup incomplete; retained resources require manual cleanup",
        }


def _run(
    args: argparse.Namespace, report: dict[str, Any], runner: Runner
) -> None:
    binaries = discover_pg_bin(args.pg_bin)
    evidence_dir = args.evidence_dir.expanduser().resolve()
    version = _postgres_version(runner, binaries)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise RehearsalBlocked(
            "native PostgreSQL rehearsal must run as an unprivileged operating-system user"
        )
    work_root = ensure_safe_owned_root(args.work_root)
    report["postgresql"] = {
        "version": version,
        "configuration": {
            "wal_level": "replica",
            "archive_mode": "on",
            "archive_timeout": "5s",
            "backup": "pg_basebackup plain + streamed WAL + SHA256 manifest",
            "verification": "pg_verifybackup plus required-output checks",
            "pitr_target": "recovery_target_lsn inclusive, promote",
        },
    }
    archive_dir = work_root / "wal-archive"
    _, restore_helper = _write_archive_helpers(work_root, archive_dir)
    source = Cluster(
        name="source",
        data_dir=work_root / "source-cluster",
        socket_dir=work_root / "source-socket",
        port=args.port_base,
        database=f"{RESOURCE_PREFIX}source".replace("-", "_"),
        binaries=binaries,
        runner=runner,
        archive_dir=archive_dir,
    )
    clusters: list[Cluster] = [source]
    try:
        source.init()
        source.start()
        source.create_database()
        _alembic_upgrade(runner, source.dsn)
        source_engine = _engine(source.dsn)
        try:
            context = _seed_baseline(
                source_engine,
                evidence_dir,
                dish_commit=str(report["source_identity"].get("current_commit") or report["source_identity"]["execution_identity"]),
            )
            baseline = _record_boundary(
                source_engine,
                "baseline",
                ["baseline"],
                context.baseline_transaction_id,
            )
            _force_archive(source_engine, archive_dir)
            system_identifier = source.system_identifier()
            backup_path, backup_duration, backup_evidence, restart_recovery = _physical_backup(
                source,
                work_root / "backup",
                system_identifier,
                inject_rename_fault=True,
            )
            rollback_burn_txid = _commit_rollback_burn(source_engine, context)
            rollback_burn = _record_boundary(
                source_engine, "rollback_burn", ["baseline"], rollback_burn_txid
            )
            rollback_burn_wal = _force_archive(source_engine, archive_dir)
            boundary_a_txid = _record_bundle(source_engine, context, "boundary_a", 2)
            boundary_a = _record_boundary(
                source_engine, "boundary_a", ["baseline", "boundary_a"], boundary_a_txid
            )
            boundary_a_wal = _force_archive(source_engine, archive_dir)
            boundary_b_txid = _record_bundle(source_engine, context, "boundary_b", 3)
            boundary_b = _record_boundary(
                source_engine,
                "boundary_b",
                ["baseline", "boundary_a", "boundary_b"],
                boundary_b_txid,
            )
            boundary_b_wal = _force_archive(source_engine, archive_dir)
            latest_txid = _record_bundle(source_engine, context, "after_boundary_b", 4)
            latest = _record_boundary(
                source_engine,
                "after_boundary_b",
                ["baseline", "boundary_a", "boundary_b", "after_boundary_b"],
                latest_txid,
            )
            latest_wal = _force_archive(source_engine, archive_dir)

            faults = _fault_backup_validation(
                runner,
                binaries,
                backup_path,
                work_root / "faults",
                system_identifier=system_identifier,
            )
            faults["interrupted_backup"] = _interrupted_backup_fault(
                source, work_root / "faults" / "interrupted-backup"
            )

            restore_full = Cluster(
                name="independent-restore",
                data_dir=work_root / "restore-full",
                socket_dir=work_root / "restore-full-socket",
                port=args.port_base + 1,
                database=source.database,
                binaries=binaries,
                runner=runner,
            )
            clusters.append(restore_full)
            restore_started = time.perf_counter()
            _copy_backup(backup_path, restore_full.data_dir)
            _verify_backup(
                runner, binaries, restore_full.data_dir, system_identifier=system_identifier
            )
            _configure_restored_cluster(restore_full)
            restore_full.start()
            restored_engine = _engine(restore_full.dsn)
            control_path = evidence_dir / "restore-control.json"
            try:
                full_state = _verify_state(
                    restored_engine,
                    expected_labels=["baseline"],
                    absent_labels=["boundary_a", "boundary_b", "after_boundary_b"],
                )
                restore_duration = time.perf_counter() - restore_started
                recovered_state = _observe_recovered_target(
                    restored_engine,
                    backup_evidence=backup_evidence,
                    recovery_target_type="backup_end",
                    recovery_target_lsn=backup_evidence.end_lsn,
                )
                atomic_json(
                    control_path,
                    _control_payload(context, recovered_state),
                    mode=0o600,
                )
                control_receipt_sha256 = sha256_file(control_path)
                pre_burn_authority_rejection = _exercise_preburn_rejection(
                    restored_engine,
                    control_path,
                    recovered_state=recovered_state,
                )
                pre_burn_authority_rejection[
                    "external_control_receipt_sha256"
                ] = control_receipt_sha256
            finally:
                control_path.unlink(missing_ok=True)
                restored_engine.dispose()

            pitr_results: list[dict[str, Any]] = []
            promotion: dict[str, Any] | None = None
            all_after = ["baseline", "boundary_a", "boundary_b", "after_boundary_b"]
            for index, boundary in enumerate((rollback_burn, boundary_b), start=1):
                target = Cluster(
                    name=f"pitr-{index}",
                    data_dir=work_root / f"pitr-{index}",
                    socket_dir=work_root / f"pitr-{index}-socket",
                    port=args.port_base + 1 + index,
                    database=source.database,
                    binaries=binaries,
                    runner=runner,
                )
                clusters.append(target)
                recovery_started = time.perf_counter()
                _copy_backup(backup_path, target.data_dir)
                _verify_backup(
                    runner, binaries, target.data_dir, system_identifier=system_identifier
                )
                _configure_pitr(target, restore_helper, boundary.lsn)
                target.start(wait_seconds=30)
                target_engine = _engine(target.dsn)
                try:
                    promotion_evidence = _wait_for_promotion(
                        target_engine, boundary.lsn, timeout_seconds=30.0
                    )
                    absent = [label for label in all_after if label not in boundary.expected_labels]
                    observed = _verify_state(
                        target_engine,
                        expected_labels=boundary.expected_labels,
                        absent_labels=absent,
                    )
                    physical_state = _observe_recovered_target(
                        target_engine,
                        backup_evidence=backup_evidence,
                        recovery_target_type="lsn",
                        recovery_target_lsn=boundary.lsn,
                    )
                    pitr_control_path = evidence_dir / f"restore-control-pitr-{index}.json"
                    atomic_json(
                        pitr_control_path,
                        _control_payload(context, physical_state),
                        mode=0o600,
                    )
                    try:
                        authority_started = time.perf_counter()
                        if boundary.label == "rollback_burn":
                            authority_promotion = _exercise_promotion(
                                target_engine,
                                pitr_control_path,
                                recovered_state=physical_state,
                                context=context,
                            )
                            faults.update(authority_promotion.pop("faults"))
                            promotion = authority_promotion
                        else:
                            authority_promotion = {
                                "promotion": _exercise_promotion_once(
                                    target_engine,
                                    pitr_control_path,
                                    recovered_state=physical_state,
                                )
                            }
                        authority_promotion["duration_seconds"] = (
                            time.perf_counter() - authority_started
                        )
                        authority_promotion["external_control_receipt_sha256"] = (
                            sha256_file(pitr_control_path)
                        )
                    finally:
                        pitr_control_path.unlink(missing_ok=True)
                    recovery_duration = time.perf_counter() - recovery_started
                finally:
                    target_engine.dispose()
                pitr_results.append(
                    {
                        "target": asdict(boundary),
                        "duration_seconds": recovery_duration,
                        "promotion": promotion_evidence,
                        "authority_promotion": authority_promotion,
                        "observed": observed,
                        "recovered_physical_state": physical_state.evidence_payload(),
                        "recovery_evidence_sha256": physical_state.evidence_sha256,
                    }
                )

            missing_archive = work_root / "missing-wal-archive"
            missing_archive.mkdir()
            _, missing_restore = _write_archive_helpers(work_root / "missing-wal", missing_archive)
            missing = Cluster(
                name="missing-wal",
                data_dir=work_root / "missing-wal-target",
                socket_dir=work_root / "missing-wal-socket",
                port=args.port_base + 4,
                database=source.database,
                binaries=binaries,
                runner=runner,
            )
            clusters.append(missing)
            _copy_backup(backup_path, missing.data_dir)
            _configure_pitr(missing, missing_restore, boundary_b.lsn)
            missing_started = missing.start(wait_seconds=8, check=False)
            if not missing_started:
                raise RehearsalError(
                    "missing-WAL rehearsal did not reach a queryable recovery state; "
                    "the startup failure cannot be attributed to missing WAL; "
                    f"stderr={runner.commands[-1].stderr_log}"
                )
            missing_engine = _engine(missing.dsn)
            try:
                with missing_engine.connect() as connection:
                    row = connection.execute(
                        text(
                            "SELECT pg_is_in_recovery(), "
                            "pg_last_wal_replay_lsn()::text, "
                            "pg_wal_lsn_diff("
                            "pg_last_wal_replay_lsn(), CAST(:target_lsn AS pg_lsn))"
                        ),
                        {"target_lsn": boundary_b.lsn},
                    ).one()
                in_recovery = bool(row[0])
                replay_lsn = None if row[1] is None else str(row[1])
                replay_delta = None if row[2] is None else float(row[2])
                if not in_recovery or (replay_delta is not None and replay_delta >= 0):
                    raise RehearsalError(
                        "PITR with missing WAL escaped recovery instead of failing closed"
                    )
                faults["missing_wal"] = {
                    "passed": True,
                    "target_lsn": boundary_b.lsn,
                    "observed": (
                        "server remained below target in recovery with an empty archive"
                    ),
                    "last_replayed_lsn": replay_lsn,
                    "replay_delta_bytes": replay_delta,
                    "archive_entries": sorted(item.name for item in missing_archive.iterdir()),
                }
            finally:
                missing_engine.dispose()
                missing.stop(mode="immediate")

            latest_time = datetime.fromisoformat(
                latest.committed_at.replace("Z", "+00:00")
            )
            selected_recovery_point_age = []
            for boundary in (rollback_burn, boundary_b):
                target_time = datetime.fromisoformat(
                    boundary.committed_at.replace("Z", "+00:00")
                )
                selected_recovery_point_age.append(
                    {
                        "target": boundary.label,
                        "seconds_to_latest_committed_boundary": (
                            latest_time - target_time
                        ).total_seconds(),
                        "lost_labels": [
                            label
                            for label in latest.expected_labels
                            if label not in boundary.expected_labels
                        ],
                    }
                )
            if promotion is None:
                raise RehearsalError(
                    "immediate post-burn PITR did not exercise authority promotion"
                )
            report.update(
                {
                    "status": "passed",
                    "topology": {
                        "source": {
                            "port": source.port,
                            "database": source.database,
                            "data_dir": str(source.data_dir),
                            "system_identifier": system_identifier,
                        },
                        "backup": str(backup_path),
                        "wal_archive": str(archive_dir),
                        "independent_restore": {
                            "port": restore_full.port,
                            "database": restore_full.database,
                            "data_dir": str(restore_full.data_dir),
                        },
                        "pitr_targets": [
                            {
                                "port": args.port_base + 2,
                                "database": source.database,
                                "data_dir": str(work_root / "pitr-1"),
                            },
                            {
                                "port": args.port_base + 3,
                                "database": source.database,
                                "data_dir": str(work_root / "pitr-2"),
                            },
                        ],
                        "database_name_reused_only_inside_independent_clusters": True,
                    },
                    "backup": {
                        "duration_seconds": backup_duration,
                        "evidence": backup_evidence.as_json(),
                        "restart_finalization_recovery": restart_recovery,
                        "independent_restore_duration_seconds": restore_duration,
                        "independent_restore_state": full_state,
                    },
                    "boundaries": [
                        asdict(baseline),
                        asdict(rollback_burn),
                        asdict(boundary_a),
                        asdict(boundary_b),
                        asdict(latest),
                    ],
                    "wal_evidence": [
                        rollback_burn_wal,
                        boundary_a_wal,
                        boundary_b_wal,
                        latest_wal,
                    ],
                    "pitr": pitr_results,
                    "pre_burn_authority_rejection": pre_burn_authority_rejection,
                    "generation_and_fencing": promotion,
                    "faults": faults,
                    "measurements": {
                        "backup_duration_seconds": backup_duration,
                        "restore_duration_seconds": restore_duration,
                        "recovery_durations_seconds": [
                            item["duration_seconds"] for item in pitr_results
                        ],
                        "generation_promotion_duration_seconds": promotion["duration_seconds"],
                        "selected_recovery_point_age": selected_recovery_point_age,
                        "production_rpo_measured": False,
                        "production_rpo_reason": (
                            "selected test transaction boundaries do not define or measure a "
                            "production recovery-point objective"
                        ),
                        "approved_threshold": None,
                        "production_requirement_claimed": False,
                    },
                    "rename_reservation_disposition": {
                        "observed": "safe",
                        "recommended_ops_issue_disposition": "retain Skip",
                        "evidence": (
                            "actual native backup was renamed by one process, that process "
                            "terminated before reservation finalization, and a separate process "
                            "accepted only the independently verified exact final output"
                        ),
                    },
                }
            )
        finally:
            source_engine.dispose()
    finally:
        _cleanup_rehearsal(
            clusters=clusters,
            evidence_dir=evidence_dir,
            work_root=work_root,
            keep_resources=args.keep_resources,
            report=report,
        )
        report["commands"] = [asdict(item) for item in runner.commands]



def _preserve_postgresql_logs(clusters: Iterable[Cluster], evidence_dir: Path) -> list[str]:
    destination = evidence_dir / "postgresql-logs"
    preserved: list[str] = []
    for cluster in clusters:
        source = cluster.data_dir / "log"
        if not source.is_dir():
            continue
        target = destination / cluster.name
        target.mkdir(parents=True, exist_ok=True)
        for log in sorted(source.glob("*.log")):
            copied = target / log.name
            shutil.copy2(log, copied)
            preserved.append(str(copied))
    return preserved


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-recovery-rehearsal")
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--work-root", required=True, type=Path)
    parser.add_argument("--pg-bin", type=Path)
    parser.add_argument("--dish-commit")
    parser.add_argument("--base-commit")
    parser.add_argument(
        "--source-identity-kind", choices=("git_commit", "synthetic_base")
    )
    parser.add_argument("--port-base", type=int, default=DEFAULT_PORT_BASE)
    parser.add_argument("--keep-resources", action="store_true")
    return parser


def _internal_main(argv: list[str]) -> int | None:
    if not argv or argv[0] != "--internal-reconcile-backup":
        return None
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--internal-reconcile-backup", action="store_true")
    parser.add_argument("request_path", type=Path)
    parser.add_argument("result_path", type=Path)
    parser.add_argument("--inject-after-rename", action="store_true")
    args = parser.parse_args(argv)
    return _internal_reconcile_backup(
        args.request_path,
        args.result_path,
        inject_after_rename=args.inject_after_rename,
    )


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    internal_result = _internal_main(raw_argv)
    if internal_result is not None:
        return internal_result
    args = _parser().parse_args(raw_argv)
    started = utc_now()
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "started_at": iso(started),
        "status": "failed",
        "scope": "database-backend-postgresql-test-plan section 2",
        "manual_cleanup": [],
        "safety": {
            "arbitrary_dsn_accepted": False,
            "production_service_profile_allowed": False,
            "resource_prefix": RESOURCE_PREFIX,
            "normal_runtime_activation": False,
        },
    }
    runner: Runner | None = None
    report_path = args.report.expanduser().resolve()
    try:
        if not (1024 <= args.port_base <= 65000 - 5):
            raise RehearsalError("--port-base must leave five unprivileged dedicated ports")
        evidence_path = args.evidence_dir.expanduser().resolve()
        work_path = args.work_root.expanduser().resolve()
        for label, path in (
            ("machine report", report_path),
            ("evidence directory", evidence_path),
            ("work root", work_path),
        ):
            if path.is_relative_to(ROOT.resolve()):
                raise RehearsalError(f"{label} must be outside the repository")
        if (
            evidence_path == work_path
            or evidence_path.is_relative_to(work_path)
            or work_path.is_relative_to(evidence_path)
        ):
            raise RehearsalError("evidence directory and work root must be independent paths")
        if report_path.is_relative_to(work_path):
            raise RehearsalError("machine report must not be inside the disposable work root")
        evidence_path.mkdir(parents=True, exist_ok=True)
        runner = Runner(evidence_path / "logs")
        report["source_identity"] = _verified_source_identity(runner, args)
        _run(args, report, runner)
        return_code = 0 if report.get("status") == "passed" else 1
    except BaseException as exc:
        report["status"] = "blocked" if isinstance(exc, RehearsalBlocked) else "failed"
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, RehearsalBlocked):
            report["blocked"] = {
                "missing_commands": list(exc.missing_commands),
                "remaining_native_scenarios": [
                    "physical backup and verification",
                    "independent restore",
                    "PITR selected boundaries",
                    "corrupt backup and missing WAL",
                    "interrupted backup and restore",
                    "restart-based rename/finalization recovery",
                    "recovered-generation and stale-actor fencing",
                    "native recovery timing measurements",
                ],
            }
        return_code = 2
    finally:
        if runner is not None and "commands" not in report:
            report["commands"] = [asdict(item) for item in runner.commands]
        if runner is not None:
            completed_cleanup = {
                "exited",
                "already_exited",
                "terminated",
                "killed",
                "exited_during_escalation",
                "spawn_failed",
            }
            unresolved_groups = [
                item.process_group_id
                for item in runner.commands
                if item.cleanup_result not in completed_cleanup
                and item.process_group_id is not None
            ]
            if unresolved_groups:
                report["manual_cleanup"] = sorted(
                    set(report.get("manual_cleanup", []))
                    | {f"process-group:{group}" for group in unresolved_groups}
                )
        completed = utc_now()
        report["completed_at"] = iso(completed)
        report["duration_seconds"] = (completed - started).total_seconds()
        atomic_json(report_path, report)
        print(json.dumps(report, sort_keys=True, indent=2))
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
