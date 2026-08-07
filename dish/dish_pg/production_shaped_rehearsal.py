"""Section 4 production-shaped PostgreSQL rehearsal on disposable local resources.

This is a thin orchestration layer. Bootstrap, import, reconciliation, projection
work, physical backup, independent restore, and PITR use existing production
entry points and Section 2 recovery helpers. Service phases reuse the maintained ``dish-service --postgresql-test-runtime``
HTTP path introduced by the Section 3 rehearsal. The command never accepts an
arbitrary service DSN and every child receives an explicit credential-free
environment.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import create_engine, func, select, text, update

from .bootstrap import (
    DEFAULT_PROJECT_GID,
    DEFAULT_PROJECT_ID,
    DEFAULT_SECTION_GID,
    DEFAULT_SECTION_ID,
)
from .database import session_factory, session_scope
from . import models as core_models
from . import stage3_models as workflow_models
from . import stage5_models as projection_models
from .production_shaped_support import corpus_identity, sha256_file
from .production_shaped_runtime import (
    BarrierServer,
    ManagedChild,
    RuntimeControlError,
    ServiceRuntimeClient,
)
from .release import ALEMBIC_HEAD
from .recovery_rehearsal import (
    COMMAND_TIMEOUTS,
    Cluster,
    RehearsalBlocked,
    RehearsalError,
    Runner,
    _alembic_upgrade,
    _configure_pitr,
    _configure_restored_cluster,
    _copy_backup,
    _force_archive,
    _physical_backup,
    _postgres_version,
    _wait_for_promotion,
    _write_archive_helpers,
    atomic_json,
    discover_pg_bin,
    iso,
    utc_now,
)
from .services import CoreAuthorityService, ImportedTaskSpec
from .transition import ProjectionService
from .workflow import WorkflowAuthorityService, sha256_json

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT.parent
REPORT_SCHEMA = "dish-postgresql-production-shaped-rehearsal-v1"
MANIFEST_SCHEMA = "dish-production-shaped-corpus-manifest-v1"
RESOURCE_MARKER = ".dish-section4-owned"
EVIDENCE_MARKER = ".dish-section4-evidence"
DATABASE_PREFIX = "dish_section4_"
DEFAULT_PORT_BASE = 56640
PHASES = (
    "postgresql_migration",
    "corpus_import",
    "reconciliation",
    "service_and_worker_startup",
    "representative_commands",
    "process_and_database_fault_injection",
    "physical_backup",
    "independent_restore",
    "point_in_time_recovery",
    "final_reconciliation_and_evidence",
)
SOURCE_IDENTITY_PATHS = (
    "dish_pg/production_shaped_rehearsal.py",
    "dish_pg/production_shaped_support.py",
    "dish_pg/production_shaped_runtime.py",
    "dish_pg/recovery_rehearsal.py",
    "dish_pg/process_failure_rehearsal.py",
    "dish_pg/bootstrap.py",
    "dish_pg/import_runtime.py",
    "dish_pg/reconciliation_worker.py",
    "dish_pg/projection_worker.py",
    "dish_pg/protocol.py",
    "dish_pg/command_port.py",
    "dish_pg/postgres_service.py",
    "dish_service/__main__.py",
    "dish_service/http.py",
    "dish_pg/transition.py",
    "dish_pg/database.py",
    "dish_pg/models.py",
    "dish_pg/stage5_models.py",
    f"dish_pg/migrations/versions/{ALEMBIC_HEAD}.py",
    "scripts/dish-pg-production-shaped-rehearsal",
    "scripts/dish-pg-recovery-rehearsal",
    "deploy/postgresql/compose.yaml",
    "alembic.ini",
    "requirements.txt",
)
FORBIDDEN_ENV_FRAGMENTS = (
    "ASANA",
    "PRODUCTION",
    "PROD_",
    "DATABASE_URL",
    "PGPASSWORD",
    "DISH_ACTION",
    "DISH_PRIVATE",
    "TOKEN",
    "SECRET",
    "CREDENTIAL",
)
SAFE_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")
REPOSITORY_INPUT_IDENTITY = re.compile(
    r"^(?:archive-sha256:[0-9a-f]{64}|git-commit:[0-9a-f]{40})$"
)
GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
FORBIDDEN_PRODUCTION_ROOTS = (
    Path("/home/marco/honest-pantry"),
    Path("/home/marco/.config"),
    Path("/home/marco/.local/state/dish"),
    Path("/etc/dish"),
    Path("/var/lib/dish"),
)

RUNTIME_UNAVAILABLE = (
    "PostgreSQL TEST service runtime is unavailable; the maintained "
    "dish-service --postgresql-test-runtime path could not be started"
)
PHASE_STATUSES = frozenset({"passed", "blocked", "failed"})
IMPLEMENTATION_STATUSES = frozenset({"implemented", "not_implemented"})


class ProductionShapedError(RehearsalError):
    """The Section 4 rehearsal cannot safely or completely proceed."""


@dataclass
class RecoveryBoundary:
    label: str
    lsn: str
    committed_at: str
    transaction_id: int
    expected_reconciliation_runs: int


@dataclass
class PhaseEvidence:
    name: str
    status: str
    implementation_status: str
    availability_status: str
    started_at: str
    duration_seconds: float
    first_attempt_status: str
    details: Mapping[str, Any]


class PhaseRecorder:
    def __init__(self) -> None:
        self.items: list[PhaseEvidence] = []

    def _require_next(self, name: str) -> None:
        if len(self.items) >= len(PHASES) or name != PHASES[len(self.items)]:
            expected = PHASES[len(self.items)] if len(self.items) < len(PHASES) else "<complete>"
            raise ProductionShapedError(f"phase order violation: expected {expected}")

    def record(
        self,
        name: str,
        *,
        status: str,
        details: Mapping[str, Any],
        started_at: datetime | None = None,
        duration_seconds: float = 0.0,
        first_attempt_status: str | None = None,
        implementation_status: str = "implemented",
        availability_status: str = "available",
    ) -> Mapping[str, Any]:
        self._require_next(name)
        if status not in PHASE_STATUSES:
            raise ProductionShapedError(f"invalid phase status: {status}")
        if implementation_status not in IMPLEMENTATION_STATUSES:
            raise ProductionShapedError(
                f"invalid phase implementation status: {implementation_status}"
            )
        payload = {"implementation_status": implementation_status, **dict(details)}
        self.items.append(
            PhaseEvidence(
                name=name,
                status=status,
                implementation_status=implementation_status,
                availability_status=availability_status,
                started_at=iso(started_at or utc_now()),
                duration_seconds=duration_seconds,
                first_attempt_status=first_attempt_status or status,
                details=payload,
            )
        )
        return payload

    def run(self, name: str, operation: Callable[[], Mapping[str, Any]]) -> Mapping[str, Any]:
        self._require_next(name)
        started_at = utc_now()
        started = time.perf_counter()
        try:
            details = dict(operation())
        except Exception as exc:
            self.record(
                name,
                status="failed",
                started_at=started_at,
                duration_seconds=time.perf_counter() - started,
                details={"error": str(exc), "type": type(exc).__name__},
                availability_status="available",
            )
            raise
        return self.record(
            name,
            status="passed",
            started_at=started_at,
            duration_seconds=time.perf_counter() - started,
            details=details,
        )

    def classified(
        self,
        name: str,
        operation: Callable[[], tuple[str, str, Mapping[str, Any]]],
    ) -> Mapping[str, Any]:
        self._require_next(name)
        started_at = utc_now()
        started = time.perf_counter()
        try:
            status, availability_status, details = operation()
        except Exception as exc:
            self.record(
                name,
                status="failed",
                started_at=started_at,
                duration_seconds=time.perf_counter() - started,
                details={"error": str(exc), "type": type(exc).__name__},
                availability_status="available",
            )
            raise
        return self.record(
            name,
            status=status,
            availability_status=availability_status,
            started_at=started_at,
            duration_seconds=time.perf_counter() - started,
            details=details,
        )

    def blocked(
        self,
        name: str,
        *,
        reason: str,
        details: Mapping[str, Any],
        availability_status: str = "blocked_runtime_infrastructure",
    ) -> Mapping[str, Any]:
        return self.record(
            name,
            status="blocked",
            first_attempt_status="blocked",
            availability_status=availability_status,
            details={"reason": reason, **dict(details)},
        )

    def fill_remaining_blocked(
        self,
        *,
        reason: str,
        availability_status: str,
        first_phase_status: str = "blocked",
    ) -> None:
        while len(self.items) < len(PHASES):
            name = PHASES[len(self.items)]
            self.record(
                name,
                status=first_phase_status,
                first_attempt_status=first_phase_status,
                availability_status=availability_status,
                details={
                    "reason": reason,
                    "execution_status": "implemented_but_blocked",
                },
            )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _hash_value(value: object) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _safe_child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": "/tmp",
        "TMPDIR": "/tmp",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONPATH": str(ROOT),
        "PYTHONNOUSERSITE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "false",
    }
    env.update(extra or {})
    forbidden = [key for key in env if any(part in key.upper() for part in FORBIDDEN_ENV_FRAGMENTS)]
    if forbidden:
        raise ProductionShapedError("unsafe child environment keys: " + ", ".join(sorted(forbidden)))
    return env


class SafeRunner(Runner):
    """Runner variant that never inherits the parent process environment."""

    def run(
        self,
        argv: Sequence[str | Path],
        *,
        timeout_seconds: float,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        inherit_env: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        if inherit_env:
            raise ProductionShapedError("Section 4 commands may not inherit the ambient environment")
        return super().run(
            argv,
            timeout_seconds=timeout_seconds,
            env=_safe_child_env(env),
            check=check,
            inherit_env=False,
        )


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _reject_production_path(path: Path, *, label: str) -> None:
    resolved = path.expanduser().resolve()
    for forbidden in FORBIDDEN_PRODUCTION_ROOTS:
        if resolved == forbidden or _is_within(resolved, forbidden):
            raise ProductionShapedError(f"{label} points at a forbidden production path: {resolved}")


def _validate_isolation_inputs(args: argparse.Namespace) -> None:
    assert args.report and args.evidence_dir and args.work_root
    assert args.corpus and args.corpus_manifest and args.honest_repo
    resolved = {
        "report": args.report.expanduser().resolve(),
        "evidence": args.evidence_dir.expanduser().resolve(),
        "work": args.work_root.expanduser().resolve(),
        "corpus": args.corpus.expanduser().resolve(),
        "corpus_manifest": args.corpus_manifest.expanduser().resolve(),
        "honest_repo": args.honest_repo.expanduser().resolve(),
    }
    for label, path in resolved.items():
        _reject_production_path(path, label=label)
    for label in ("report", "evidence", "work"):
        if resolved[label] == REPOSITORY or _is_within(resolved[label], REPOSITORY):
            raise ProductionShapedError(f"{label} must be outside the repository worktree")
    if _is_within(resolved["evidence"], resolved["work"]) or _is_within(
        resolved["report"], resolved["work"]
    ):
        raise ProductionShapedError("evidence and report paths must be outside the disposable work root")
    if _is_within(resolved["corpus"], resolved["work"]) or _is_within(
        resolved["corpus_manifest"], resolved["work"]
    ):
        raise ProductionShapedError("corpus inputs must be outside the disposable work root")
    if resolved["report"].exists():
        raise ProductionShapedError("report path already exists; refusing stale evidence overwrite")
    if not resolved["corpus"].is_file() or not resolved["corpus_manifest"].is_file():
        raise ProductionShapedError("corpus and corpus manifest must be readable regular files")
    if not resolved["honest_repo"].is_dir():
        raise ProductionShapedError("Honest checkout must be a local directory")


def _write_private_marker(path: Path, value: Mapping[str, Any]) -> None:
    atomic_json(path, value)
    path.chmod(0o600)


def _ensure_evidence_dir(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    marker = resolved / EVIDENCE_MARKER
    expected = {"schema": REPORT_SCHEMA}
    if marker.exists():
        try:
            observed = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProductionShapedError("Section 4 evidence ownership marker is invalid") from exc
        if observed != expected:
            raise ProductionShapedError("Section 4 evidence ownership marker is invalid")
        stale = [item for item in resolved.iterdir() if item.name != EVIDENCE_MARKER]
        if stale:
            raise ProductionShapedError("Section 4 evidence directory contains stale artifacts")
    else:
        if any(resolved.iterdir()):
            raise ProductionShapedError("Section 4 evidence directory is nonempty and unowned")
        _write_private_marker(marker, expected)
    return resolved


def _owned_root_identity(port_base: int) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "database_prefix": DATABASE_PREFIX,
        "allowed_ports": list(range(port_base, port_base + 5)),
    }


def _ensure_owned_root(path: Path, *, port_base: int) -> Path:
    resolved = path.expanduser().resolve()
    if resolved in {Path("/"), Path.home(), ROOT.resolve(), REPOSITORY.resolve()}:
        raise ProductionShapedError(f"unsafe Section 4 work root: {resolved}")
    if not SAFE_PATH.fullmatch(str(resolved)):
        raise ProductionShapedError("Section 4 paths may contain only letters, numbers, _, -, ., and /")
    resolved.mkdir(parents=True, exist_ok=True)
    marker = resolved / RESOURCE_MARKER
    expected = _owned_root_identity(port_base)
    if marker.exists():
        try:
            observed = json.loads(marker.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ProductionShapedError("Section 4 ownership marker is invalid") from exc
        if observed != expected:
            raise ProductionShapedError("Section 4 ownership marker is invalid")
        stale = [item for item in resolved.iterdir() if item.name != RESOURCE_MARKER]
        if stale:
            raise ProductionShapedError(
                "Section 4 work root contains retained resources; clean them before rerunning"
            )
    else:
        if any(resolved.iterdir()):
            raise ProductionShapedError("Section 4 work root is nonempty and unowned")
        _write_private_marker(marker, expected)
    return resolved


def _git(runner: Runner, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return runner.run(
        ["git", "-c", f"safe.directory={REPOSITORY}", "-C", REPOSITORY, *args],
        timeout_seconds=COMMAND_TIMEOUTS["git"],
        check=check,
        env=_safe_child_env(),
        inherit_env=False,
    )


def _git_at(
    runner: Runner,
    repository: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    resolved = repository.expanduser().resolve()
    return runner.run(
        ["git", "-c", f"safe.directory={resolved}", "-C", resolved, *args],
        timeout_seconds=COMMAND_TIMEOUTS["git"],
        check=check,
        env=_safe_child_env(),
        inherit_env=False,
    )


def _checkout_identity(
    runner: Runner,
    repository: Path,
    *,
    expected_commit: str,
    label: str,
) -> dict[str, Any]:
    resolved = repository.expanduser().resolve()
    if not (resolved / ".git").exists():
        raise ProductionShapedError(f"{label} Git metadata is required")
    head = _git_at(runner, resolved, "rev-parse", "HEAD").stdout.strip()
    tree = _git_at(runner, resolved, "rev-parse", "HEAD^{tree}").stdout.strip()
    status = _git_at(runner, resolved, "status", "--porcelain=v1").stdout.splitlines()
    if head != expected_commit:
        raise ProductionShapedError(
            f"{label} commit mismatch: expected {expected_commit}, observed {head}"
        )
    if status:
        raise ProductionShapedError(f"{label} worktree must be clean")
    return {
        "path": str(resolved),
        "commit": head,
        "tree": tree,
        "worktree_clean": True,
    }


def _deployment_configuration_identity() -> dict[str, Any]:
    paths = (
        ROOT / "deploy/postgresql/compose.yaml",
        ROOT / "alembic.ini",
        ROOT / "requirements.txt",
    )
    files = [
        {"path": str(path.relative_to(ROOT)), "sha256": sha256_file(path)}
        for path in paths
    ]
    payload = {"schema": "dish-section4-deployment-config-v1", "files": files}
    return {**payload, "identity": _hash_value(payload)}


def _validate_manifest_bindings(
    manifest: Mapping[str, Any],
    *,
    source_identity: Mapping[str, Any],
    deployment_identity: Mapping[str, Any],
) -> None:
    declared_source = manifest["source_manifest"]["identity"]
    observed_source = source_identity["source_manifest"]["manifest_sha256"]
    if declared_source != observed_source:
        raise ProductionShapedError(
            "corpus manifest source_manifest identity does not match the current source manifest"
        )
    declared_deployment = manifest["deployment_identity"]["identity"]
    observed_deployment = deployment_identity["identity"]
    if declared_deployment != observed_deployment:
        raise ProductionShapedError(
            "corpus manifest deployment_identity does not match current deployment configuration"
        )


def _source_manifest() -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for relative in SOURCE_IDENTITY_PATHS:
        path = ROOT / relative
        if not path.is_file():
            raise ProductionShapedError(f"source identity path missing: {relative}")
        files.append({"path": relative, "size_bytes": path.stat().st_size, "sha256": sha256_file(path)})
    payload = {"schema": "dish-section4-source-manifest-v1", "alembic_head": ALEMBIC_HEAD, "files": files}
    return {**payload, "manifest_sha256": _hash_value(payload)}


def _source_identity(
    runner: Runner,
    *,
    expected_commit: str | None,
    expected_base: str | None,
    base_kind: str,
) -> dict[str, Any]:
    for label, value in (("dish commit", expected_commit), ("base commit", expected_base)):
        if value is not None and not GIT_COMMIT.fullmatch(value):
            raise ProductionShapedError(f"{label} must be 40 lowercase hexadecimal characters")
    if not (REPOSITORY / ".git").is_dir():
        raise ProductionShapedError("Git metadata is required for Section 4 evidence")
    head = _git(runner, "rev-parse", "HEAD").stdout.strip()
    tree = _git(runner, "rev-parse", "HEAD^{tree}").stdout.strip()
    parent_result = _git(runner, "rev-parse", "HEAD^", check=False)
    parent = parent_result.stdout.strip() if parent_result.returncode == 0 else None
    status = _git(runner, "status", "--porcelain=v1").stdout.splitlines()
    relevant = _git(
        runner,
        "status",
        "--porcelain=v1",
        "--",
        *[f"dish/{path}" for path in SOURCE_IDENTITY_PATHS],
    ).stdout.splitlines()
    if status or relevant:
        raise ProductionShapedError("Section 4 requires a clean Git worktree")
    if expected_commit and expected_commit != head:
        raise ProductionShapedError(f"--dish-commit mismatch: expected {expected_commit}, observed {head}")
    if expected_base and expected_base != parent:
        raise ProductionShapedError(f"--base-commit mismatch: expected {expected_base}, observed {parent}")
    if base_kind == "synthetic_base" and not expected_base:
        raise ProductionShapedError("synthetic_base identity requires --base-commit")
    return {
        "kind": "git_commit",
        "current_commit": head,
        "current_tree": tree,
        "parent_commit": parent,
        "base_identity_kind": base_kind,
        "worktree_clean": True,
        "relevant_worktree_clean": True,
        "source_manifest": _source_manifest(),
    }


def _reject_sensitive_identity(value: object, *, label: str) -> None:
    forbidden_values = (
        "asana.com/",
        "api.asana.com",
        "postgresql://",
        "postgresql+psycopg://",
        "/production/",
        "/prod/",
        "/home/marco/",
        "/etc/dish",
        "/var/lib/dish",
    )

    def visit(item: object, path: str) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                lowered = str(key).lower()
                if any(part in lowered for part in ("token", "secret", "password", "credential")):
                    raise ProductionShapedError(
                        f"{label} contains credential-shaped key: {path}.{key}"
                    )
                visit(nested, f"{path}.{key}")
        elif isinstance(item, list):
            for index, nested in enumerate(item):
                visit(nested, f"{path}[{index}]")
        elif isinstance(item, str):
            lowered = item.lower()
            if any(token in lowered for token in forbidden_values):
                raise ProductionShapedError(
                    f"{label} contains a forbidden production locator: {path}"
                )

    visit(value, label)


def _load_corpus_manifest(path: Path, corpus: Path) -> dict[str, Any]:
    manifest_path = path.expanduser().resolve()
    corpus_path = corpus.expanduser().resolve()
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProductionShapedError("sanitized corpus manifest is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema") != MANIFEST_SCHEMA:
        raise ProductionShapedError(f"corpus manifest schema must be {MANIFEST_SCHEMA}")
    allowed_keys = {
        "schema",
        "sanitized",
        "resource_scope",
        "production_contact_prohibited",
        "contains_production_credentials",
        "corpus_sha256",
        "record_count",
        "deployment_identity",
        "source_manifest",
    }
    if set(value) != allowed_keys:
        extras = sorted(set(value) - allowed_keys)
        missing = sorted(allowed_keys - set(value))
        raise ProductionShapedError(
            f"corpus manifest fields mismatch; missing={missing}, unexpected={extras}"
        )
    required = {
        "sanitized": True,
        "resource_scope": "local_or_test_only",
        "production_contact_prohibited": True,
        "contains_production_credentials": False,
    }
    for key, expected in required.items():
        if value.get(key) != expected:
            raise ProductionShapedError(f"corpus manifest must declare {key}={expected!r}")
    digest = sha256_file(corpus_path)
    if value.get("corpus_sha256") != digest:
        raise ProductionShapedError("sanitized corpus SHA-256 does not match its manifest")
    records = [line for line in corpus_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if value.get("record_count") != len(records) or not records:
        raise ProductionShapedError("sanitized corpus record count does not match its manifest")
    for line_number, raw in enumerate(records, start=1):
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProductionShapedError(f"corpus line {line_number} is invalid JSON") from exc
        _reject_sensitive_identity(item, label=f"corpus line {line_number}")
    deployment = value.get("deployment_identity")
    source_manifest = value.get("source_manifest")
    if not isinstance(deployment, dict) or not deployment.get("identity"):
        raise ProductionShapedError("corpus manifest omits deployment identity")
    if not isinstance(source_manifest, dict) or not source_manifest.get("identity"):
        raise ProductionShapedError("corpus manifest omits source manifest identity")
    _reject_sensitive_identity(deployment, label="deployment_identity")
    _reject_sensitive_identity(source_manifest, label="source_manifest")
    return {
        **value,
        "path": str(manifest_path),
        "manifest_sha256": sha256_file(manifest_path),
        "corpus_path": str(corpus_path),
    }


def _run_module(
    runner: Runner,
    module: str,
    arguments: Sequence[str | Path],
    *,
    timeout: float = 180.0,
    check: bool = True,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return runner.run(
        [sys.executable, "-m", module, *arguments],
        timeout_seconds=timeout,
        env=env,
        inherit_env=False,
        check=check,
    )


def _authoritative_projection_snapshot(
    session,
    *,
    generation_id: uuid.UUID,
    task_id: uuid.UUID,
) -> dict[str, Any]:
    head = session.get(core_models.TaskAuthorityHead, (generation_id, task_id))
    if head is None:
        raise ProductionShapedError("projection fixture task authority is missing")
    activation = session.get(
        core_models.ContentActivation, head.current_content_activation_id
    )
    if activation is None:
        raise ProductionShapedError("projection fixture content activation is missing")
    content = session.get(core_models.ContentVersion, activation.content_version_id)
    placement = session.get(core_models.CurrentTaskSectionPlacement, (generation_id, task_id))
    completion = session.get(core_models.CurrentTaskCompletion, (generation_id, task_id))
    if content is None or placement is None or completion is None:
        raise ProductionShapedError("projection fixture current state is incomplete")
    project_ids = sorted(
        str(value)
        for value in session.scalars(
            select(core_models.CurrentTaskProjectMembership.project_id).where(
                core_models.CurrentTaskProjectMembership.generation_id == generation_id,
                core_models.CurrentTaskProjectMembership.task_id == task_id,
                core_models.CurrentTaskProjectMembership.is_member.is_(True),
            )
        )
    )
    return {
        "task_id": str(task_id),
        "title": content.title,
        "body": content.body,
        "identity_scheme": content.identity_scheme,
        "content_identity": content.content_identity,
        "project_ids": project_ids,
        "section_id": None if placement.section_id is None else str(placement.section_id),
        "completed": bool(completion.completed),
    }


def _prepare_projection(engine, generation_id: uuid.UUID, corpus: Path, local_store: Path) -> dict[str, Any]:
    with session_scope(session_factory(engine)) as session:
        service = ProjectionService(session)
        epoch = service.activate_epoch(
            generation_id=generation_id,
            activation_reason="Section 4 local production-shaped rehearsal",
            created_at=utc_now(),
            external_effects_enabled=True,
        )
        project_count, section_count, task_count = service.bind_imported_mappings(
            generation_id=generation_id, bound_at=utc_now()
        )
        first = json.loads(next(line for line in corpus.read_text(encoding="utf-8").splitlines() if line.strip()))
        task_id = uuid.UUID(first["task_id"])
        authoritative_snapshot = _authoritative_projection_snapshot(
            session,
            generation_id=generation_id,
            task_id=task_id,
        )
        event = service._record_event(
            generation_id=generation_id,
            execution_id=None,
            task_id=task_id,
            event_type="reproject",
            payload={
                "reason": "section4_local_projection",
                "local_store_path": str(local_store),
                "authoritative_snapshot": authoritative_snapshot,
            },
            source_route="service",
            origin="live",
            created_at=utc_now(),
        )
    return {
        "projection_epoch_id": str(epoch.projection_epoch_id),
        "project_mappings": project_count,
        "section_mappings": section_count,
        "task_mappings": task_count,
        "projection_event_id": str(event.projection_event_id),
        "task_id": str(task_id),
    }


def _reconcile(
    runner: Runner,
    *,
    dsn: str,
    generation_id: str,
    identity: str,
    output: Path,
) -> dict[str, Any]:
    completed = _run_module(
        runner,
        "dish_pg.reconciliation_worker",
        [
            "--database-url",
            dsn,
            "--generation-id",
            generation_id,
            "--corpus-identity",
            identity,
            "--fetcher",
            "dish_pg.production_shaped_support:fetch_sanitized_corpus",
            "--comparator",
            "dish_pg.production_shaped_support:compare_sanitized_item",
            "--output",
            output,
        ],
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    if completed.returncode != 0 or not report.get("ok"):
        raise ProductionShapedError("sanitized corpus reconciliation failed")
    return report


def _reconciliation_count(engine) -> int:
    with engine.connect() as connection:
        return int(connection.scalar(text("SELECT count(*) FROM projection_reconciliation_runs")) or 0)


def _boundary(engine, label: str, expected_count: int) -> RecoveryBoundary:
    with engine.connect() as connection:
        row = connection.execute(text("SELECT pg_current_wal_flush_lsn()::text, clock_timestamp(), txid_current()" )).one()
    return RecoveryBoundary(
        label=label,
        lsn=str(row[0]),
        committed_at=iso(row[1]),
        transaction_id=int(row[2]),
        expected_reconciliation_runs=expected_count,
    )


def _verify_database(engine, *, expected_min_reconciliations: int, task_id: str) -> dict[str, Any]:
    with engine.connect() as connection:
        head = connection.scalar(text("SELECT version_num FROM alembic_version"))
        task_count = int(connection.scalar(text("SELECT count(*) FROM dish_tasks")) or 0)
        reconciliations = int(connection.scalar(text("SELECT count(*) FROM projection_reconciliation_runs")) or 0)
        task_present = bool(
            connection.scalar(text("SELECT EXISTS(SELECT 1 FROM dish_tasks WHERE task_id=CAST(:task AS uuid))"), {"task": task_id})
        )
    if head != ALEMBIC_HEAD or not task_present or reconciliations < expected_min_reconciliations:
        raise ProductionShapedError("restored database verification failed")
    return {
        "alembic_head": head,
        "task_count": task_count,
        "reconciliation_runs": reconciliations,
        "representative_task_present": task_present,
    }




def _runtime_entrypoint_identity(runner: Runner, path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve()
    if not _is_within(resolved, REPOSITORY):
        raise ProductionShapedError("service entry point must be inside the repository")
    if not resolved.is_file():
        raise ProductionShapedError("service entry point is not a file")
    relative = resolved.relative_to(REPOSITORY).as_posix()
    tracked = _git_at(runner, REPOSITORY, "ls-files", "--error-unmatch", relative, check=False)
    if tracked.returncode != 0:
        raise ProductionShapedError("service entry point must be Git-tracked")
    status = _git_at(runner, REPOSITORY, "status", "--porcelain=v1", "--", relative)
    if status.stdout.strip():
        raise ProductionShapedError("service entry point must be clean at the bound commit")
    if resolved.suffix != ".py" and not os.access(resolved, os.X_OK):
        raise ProductionShapedError("non-Python service entry point must be executable")
    return {
        "path": relative,
        "sha256": sha256_file(resolved),
        "size_bytes": resolved.stat().st_size,
        "runtime": "dish-service --postgresql-test-runtime",
        "transport": "loopback-http",
    }


def _spawn_module_child(
    *,
    label: str,
    module: str,
    arguments: Sequence[str | Path],
    evidence_dir: Path,
    env: Mapping[str, str] | None = None,
) -> ManagedChild:
    return ManagedChild.spawn(
        label=label,
        argv=[sys.executable, "-m", module, *arguments],
        cwd=ROOT,
        env=_safe_child_env(env),
        log_path=evidence_dir / "process-logs" / f"{label}.log",
    )


def _seed_projection_event(
    engine,
    *,
    generation_id: uuid.UUID,
    task_id: uuid.UUID,
    local_store: Path,
    reason: str,
) -> uuid.UUID:
    with session_scope(session_factory(engine)) as session:
        authoritative_snapshot = _authoritative_projection_snapshot(
            session,
            generation_id=generation_id,
            task_id=task_id,
        )
        event = ProjectionService(session)._record_event(
            generation_id=generation_id,
            execution_id=None,
            task_id=task_id,
            event_type="reproject",
            payload={
                "reason": reason,
                "local_store_path": str(local_store),
                "authoritative_snapshot": authoritative_snapshot,
            },
            source_route="service",
            origin="live",
            created_at=utc_now(),
        )
        return event.projection_event_id


def _import_projection_fault_task(
    engine,
    *,
    generation_id: uuid.UUID,
    import_run_id: uuid.UUID,
    contract_binding_id: uuid.UUID,
    project_id: uuid.UUID,
    section_id: uuid.UUID,
    scenario: str,
) -> uuid.UUID:
    task_id = uuid.uuid5(uuid.NAMESPACE_URL, f"dish-section4-projection-fault:{scenario}")
    alias_suffix = int.from_bytes(hashlib.sha256(scenario.encode("utf-8")).digest()[:6], "big")
    asana_gid = str(8_000_000_000_000_000 + alias_suffix)
    title = f"Sanitized Section 4 projection fault {scenario}"
    body = f"Isolated local-only projection fault fixture for {scenario}."
    content_identity = sha256_json(
        {
            "scenario": scenario,
            "title": title,
            "body": body,
            "section_id": str(section_id),
        }
    )
    with session_scope(session_factory(engine)) as session:
        CoreAuthorityService(session).import_task_document(
            generation_id=generation_id,
            import_run_id=import_run_id,
            contract_binding_id=contract_binding_id,
            spec=ImportedTaskSpec(
                task_id=task_id,
                asana_task_gid=asana_gid,
                title=title,
                body=body,
                identity_scheme="section4-fault-v1",
                content_identity=content_identity,
                project_ids=(project_id,),
                section_id=section_id,
                completed=False,
                observed_at=utc_now(),
            ),
        )
        ProjectionService(session).bind_imported_mappings(
            generation_id=generation_id,
            bound_at=utc_now(),
        )
        task = session.get(core_models.DishTask, task_id)
        mapping = session.scalar(
            select(projection_models.TaskProjectionMapping).where(
                projection_models.TaskProjectionMapping.generation_id == generation_id,
                projection_models.TaskProjectionMapping.task_id == task_id,
                projection_models.TaskProjectionMapping.state == "active",
            )
        )
        if task is None or task.creation_route != "import" or mapping is None:
            raise ProductionShapedError("projection fault task import provenance is incomplete")
    return task_id


def _projection_snapshot(engine, event_id: uuid.UUID) -> dict[str, Any]:
    with session_scope(session_factory(engine)) as session:
        event = session.get(projection_models.ProjectionOutboxEvent, event_id)
        if event is None:
            raise ProductionShapedError("projection event disappeared")
        attempts = list(
            session.scalars(
                select(projection_models.ProjectionAttempt)
                .where(projection_models.ProjectionAttempt.projection_event_id == event_id)
                .order_by(projection_models.ProjectionAttempt.attempt_number)
            )
        )
        observations = int(
            session.scalar(
                select(func.count())
                .select_from(projection_models.ProjectionObservation)
                .join(
                    projection_models.ProjectionAttempt,
                    projection_models.ProjectionAttempt.attempt_id
                    == projection_models.ProjectionObservation.attempt_id,
                )
                .where(projection_models.ProjectionAttempt.projection_event_id == event_id)
            )
            or 0
        )
    return {
        "event_id": str(event_id),
        "state": event.state,
        "claim_owner": event.claim_owner,
        "attempts": [
            {
                "attempt_id": str(item.attempt_id),
                "number": item.attempt_number,
                "kind": item.attempt_kind,
                "state": item.state,
                "dispatch_identity": item.dispatch_identity,
            }
            for item in attempts
        ],
        "observation_count": observations,
    }


def _expire_projection_claim(engine, event_id: uuid.UUID) -> None:
    with session_scope(session_factory(engine)) as session:
        result = session.execute(
            update(projection_models.ProjectionOutboxEvent)
            .where(
                projection_models.ProjectionOutboxEvent.projection_event_id == event_id,
                projection_models.ProjectionOutboxEvent.state == "claimed",
            )
            .values(claim_expires_at=text("clock_timestamp() - interval '1 second'"))
        )
        if result.rowcount != 1:
            raise ProductionShapedError("projection event was not in the expected claimed state")


def _read_effect_ledger(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"dispatch_calls": 0, "recovery_observations": 0, "effects": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ProductionShapedError("projection effect ledger is invalid")
    return value


def _reconciliation_snapshot(engine) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            "runs": int(
                connection.scalar(text("SELECT count(*) FROM projection_reconciliation_runs"))
                or 0
            ),
            "items": int(
                connection.scalar(text("SELECT count(*) FROM projection_reconciliation_items"))
                or 0
            ),
        }


def _register_runtime_run(
    engine, *, generation_id: uuid.UUID, corpus_sha256: str
) -> tuple[str, str]:
    owner_id = "cli"
    run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"dish-section4-runtime:{corpus_sha256}")
    capability = hashlib.sha256(f"section4:{run_id}".encode("utf-8")).digest()
    with session_scope(session_factory(engine)) as session:
        existing = session.get(workflow_models.ServiceRun, run_id)
        if existing is None:
            WorkflowAuthorityService(session).register_run(
                run_id=run_id,
                generation_id=generation_id,
                owner_id=owner_id,
                agent="service",
                capability_digest=capability,
                registered_at=utc_now(),
            )
        elif (
            existing.generation_id != generation_id
            or existing.owner_id != owner_id
            or existing.status != "active"
        ):
            raise ProductionShapedError("existing Section 4 runtime run identity conflicts")
    return owner_id, str(run_id)


def _service_command_snapshot(engine, request_id: uuid.UUID) -> dict[str, Any]:
    with session_scope(session_factory(engine)) as session:
        request_count = int(
            session.scalar(
                select(func.count())
                .select_from(workflow_models.ServiceRequest)
                .where(workflow_models.ServiceRequest.request_id == request_id)
            )
            or 0
        )
        outcome = session.scalar(
            select(workflow_models.ServiceRequestOutcome).where(
                workflow_models.ServiceRequestOutcome.request_id == request_id
            )
        )
        execution = session.scalar(
            select(workflow_models.CommandExecution).where(
                workflow_models.CommandExecution.request_id == request_id
            )
        )
        task_count = 0
        projection_count = 0
        task_id = None
        execution_id = None
        if execution is not None:
            execution_id = str(execution.execution_id)
            task_id = None if execution.task_id is None else str(execution.task_id)
            task_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(core_models.DishTask)
                    .where(core_models.DishTask.command_execution_id == execution.execution_id)
                )
                or 0
            )
            projection_count = int(
                session.scalar(
                    select(func.count())
                    .select_from(projection_models.ProjectionOutboxEvent)
                    .where(
                        projection_models.ProjectionOutboxEvent.command_execution_id
                        == execution.execution_id
                    )
                )
                or 0
            )
    return {
        "request_id": str(request_id),
        "request_count": request_count,
        "outcome_count": 0 if outcome is None else 1,
        "outcome_code": None if outcome is None else outcome.result_code,
        "outcome_sha256": None if outcome is None else outcome.result_sha256,
        "execution_count": 0 if execution is None else 1,
        "execution_id": execution_id,
        "task_id": task_id,
        "task_effect_count": task_count,
        "projection_effect_count": projection_count,
    }


def _assert_single_command_effect(snapshot: Mapping[str, Any]) -> None:
    expected = {
        "request_count": 1,
        "outcome_count": 1,
        "execution_count": 1,
        "task_effect_count": 1,
        "projection_effect_count": 1,
    }
    failures = {
        key: {"expected": value, "actual": snapshot.get(key)}
        for key, value in expected.items()
        if snapshot.get(key) != value
    }
    if failures:
        raise ProductionShapedError(f"duplicate or missing command effects: {failures}")


def _cluster_pid(cluster: Cluster) -> int | None:
    path = cluster.data_dir / "postmaster.pid"
    if not path.is_file():
        return None
    try:
        return int(path.read_text(encoding="utf-8").splitlines()[0])
    except (OSError, ValueError, IndexError):
        return None


def _cluster_cleanup_evidence(
    cluster: Cluster,
    *,
    stopped: bool,
    error: str | None,
    pid_before_stop: int | None = None,
    status_returncode: int | None = None,
    status_error: str | None = None,
) -> dict[str, Any]:
    log_dir = cluster.data_dir / "log"
    logs = [str(path) for path in sorted(log_dir.glob("*"))] if log_dir.is_dir() else []
    return {
        "kind": "postgresql_cluster",
        "name": cluster.name,
        "stopped": stopped,
        "pid": pid_before_stop if pid_before_stop is not None else _cluster_pid(cluster),
        "pid_file_after_stop": _cluster_pid(cluster),
        "pg_ctl_status_returncode": status_returncode,
        "pg_ctl_status_error": status_error,
        "port": cluster.port,
        "data_path": str(cluster.data_dir),
        "socket_path": str(cluster.socket_dir),
        "logs": logs,
        "error": error,
        "cleanup_commands": [
            f"{cluster.binaries['pg_ctl']} -D {cluster.data_dir} -m fast stop",
            f"{cluster.binaries['pg_ctl']} -D {cluster.data_dir} -m immediate stop",
            f"{cluster.binaries['pg_ctl']} -D {cluster.data_dir} status",
        ],
    }


def _confirm_cluster_stopped(cluster: Cluster) -> tuple[bool, int | None, str | None]:
    try:
        result = cluster.runner.run(
            [cluster.binaries["pg_ctl"], "-D", cluster.data_dir, "status"],
            timeout_seconds=10.0,
            check=False,
        )
    except Exception as exc:
        return False, None, f"{type(exc).__name__}: {exc}"
    stopped = result.returncode != 0 and not (cluster.data_dir / "postmaster.pid").exists()
    return stopped, result.returncode, None



@dataclass(frozen=True)
class CleanupOutcome:
    failed: bool
    work_root_removed: bool
    evidence: tuple[Mapping[str, Any], ...]
    requirements: tuple[str, ...]


def _cleanup_owned_resources(
    *,
    children: Sequence[ManagedChild],
    clusters: Sequence[Cluster],
    engine: Any,
    work_root: Path,
    keep_resources: bool,
) -> CleanupOutcome:
    evidence: list[dict[str, Any]] = []
    requirements: list[str] = []
    failed = False
    removed = False
    seen_pids: set[int] = set()

    for child in reversed(children):
        if child.pid in seen_pids:
            continue
        seen_pids.add(child.pid)
        try:
            stop = child.terminate(grace_seconds=5.0)
        except Exception as exc:
            stop = {
                "label": child.label,
                "pid": child.pid,
                "process_group_id": child.process_group_id,
                "stopped": False,
                "error": f"{type(exc).__name__}: {exc}",
                "log_path": str(child.log_path),
                "cleanup_commands": [
                    f"kill -TERM -- -{child.process_group_id}",
                    f"kill -KILL -- -{child.process_group_id}",
                ],
            }
        try:
            child_evidence = child.evidence()
        except Exception as exc:
            child_evidence = {
                "label": child.label,
                "pid": child.pid,
                "process_group_id": child.process_group_id,
                "log_path": str(child.log_path),
                "evidence_error": f"{type(exc).__name__}: {exc}",
            }
            failed = True
            requirements.append(
                f"child evidence collection failed for PID {child.pid}: "
                f"{type(exc).__name__}: {exc}"
            )
        record = {"kind": "child_process", **child_evidence, "cleanup": stop}
        evidence.append(record)
        if not stop.get("stopped"):
            failed = True
            requirements.append(
                f"manual child cleanup required for PID {child.pid}: "
                + "; ".join(stop.get("cleanup_commands", []))
            )

    if engine is not None:
        try:
            engine.dispose()
        except Exception as exc:
            failed = True
            requirements.append(
                f"parent engine disposal failed: {type(exc).__name__}: {exc}"
            )

    for cluster in reversed(clusters):
        pid_before_stop = _cluster_pid(cluster)
        error = None
        try:
            cluster.stop()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
        confirmed_stopped, status_returncode, status_error = _confirm_cluster_stopped(
            cluster
        )
        stopped = error is None and confirmed_stopped
        record = _cluster_cleanup_evidence(
            cluster,
            stopped=stopped,
            error=error,
            pid_before_stop=pid_before_stop,
            status_returncode=status_returncode,
            status_error=status_error,
        )
        evidence.append(record)
        if not stopped:
            failed = True
            requirements.append(
                f"manual PostgreSQL cleanup required for {cluster.name}: "
                + "; ".join(record["cleanup_commands"])
            )

    if failed:
        requirements.append(
            f"work root retained because owned resources were not confirmed stopped: {work_root}"
        )
    elif keep_resources:
        requirements.append(f"resources intentionally retained beneath {work_root}")
    else:
        try:
            shutil.rmtree(work_root)
            removed = True
        except Exception as exc:
            failed = True
            requirements.append(
                f"work root removal failed and remaining path must be inspected: {work_root}; "
                f"{type(exc).__name__}: {exc}"
            )

    return CleanupOutcome(
        failed=failed,
        work_root_removed=removed,
        evidence=tuple(evidence),
        requirements=tuple(requirements),
    )


def _projection_worker_arguments(*, dsn: str, worker_id: str) -> list[str]:
    return [
        "--database-url",
        dsn,
        "--worker-id",
        worker_id,
        "--adapter",
        "dish_pg.production_shaped_support:LocalProjectionAdapter",
        "--claim-ttl-seconds",
        "3600",
        "--once",
    ]


def _reconciliation_worker_arguments(
    *, dsn: str, generation_id: str, identity: str, output: Path
) -> list[str | Path]:
    return [
        "--database-url",
        dsn,
        "--generation-id",
        generation_id,
        "--corpus-identity",
        identity,
        "--fetcher",
        "dish_pg.production_shaped_support:fetch_sanitized_corpus",
        "--comparator",
        "dish_pg.production_shaped_support:compare_sanitized_item",
        "--output",
        output,
    ]


def _projection_process_loss_scenario(
    *,
    engine,
    primary: Cluster,
    generation_id: uuid.UUID,
    task_id: uuid.UUID,
    evidence_dir: Path,
    runner: Runner,
    children: list[ManagedChild],
) -> dict[str, Any]:
    store = evidence_dir / "fault-projection-process-loss.json"
    ledger = evidence_dir / "fault-projection-process-loss-ledger.json"
    event_id = _seed_projection_event(
        engine,
        generation_id=generation_id,
        task_id=task_id,
        local_store=store,
        reason="section4_projection_process_loss",
    )
    barrier_path = evidence_dir / "barriers" / "projection-process-loss.sock"
    with BarrierServer(barrier_path) as barrier:
        child = _spawn_module_child(
            label="projection-process-loss-old",
            module="dish_pg.projection_worker",
            arguments=_projection_worker_arguments(
                dsn=primary.dsn, worker_id="section4-projection-process-loss-old"
            ),
            evidence_dir=evidence_dir,
            env={
                "DISH_SECTION4_BARRIER_SOCKET": str(barrier_path),
                "DISH_SECTION4_PROJECTION_SCENARIO": "after_effect",
                "DISH_SECTION4_EFFECT_LEDGER": str(ledger),
            },
        )
        children.append(child)
        reached = barrier.wait(
            "projection_after_effect_before_observation", timeout_seconds=60.0
        )
        before = _projection_snapshot(engine, event_id)
        kill = child.kill_for_fault()
        reached.close_without_release()
    _expire_projection_claim(engine, event_id)
    replacement = _run_module(
        runner,
        "dish_pg.projection_worker",
        _projection_worker_arguments(
            dsn=primary.dsn, worker_id="section4-projection-process-loss-restart"
        ),
        env={"DISH_SECTION4_EFFECT_LEDGER": str(ledger)},
    )
    after = _projection_snapshot(engine, event_id)
    effects = _read_effect_ledger(ledger)
    if replacement.returncode != 0 or after["state"] != "applied":
        raise ProductionShapedError("projection worker did not recover after process loss")
    if effects.get("dispatch_calls") != 1 or effects.get("recovery_observations") != 1:
        raise ProductionShapedError("projection process-loss recovery duplicated or omitted effects")
    return {
        "status": "passed",
        "control_point": "projection_after_effect_before_observation",
        "event_id": str(event_id),
        "before_loss": before,
        "kill": kill,
        "after_restart": after,
        "effect_ledger": effects,
        "store_sha256": sha256_file(store),
    }


def _projection_database_disconnect_scenario(
    *,
    engine,
    primary: Cluster,
    generation_id: uuid.UUID,
    task_id: uuid.UUID,
    evidence_dir: Path,
    runner: Runner,
    children: list[ManagedChild],
) -> dict[str, Any]:
    store = evidence_dir / "fault-projection-database-disconnect.json"
    ledger = evidence_dir / "fault-projection-database-disconnect-ledger.json"
    event_id = _seed_projection_event(
        engine,
        generation_id=generation_id,
        task_id=task_id,
        local_store=store,
        reason="section4_projection_database_disconnect",
    )
    barrier_path = evidence_dir / "barriers" / "projection-db-disconnect.sock"
    with BarrierServer(barrier_path) as barrier:
        child = _spawn_module_child(
            label="projection-database-disconnect",
            module="dish_pg.projection_worker",
            arguments=_projection_worker_arguments(
                dsn=primary.dsn, worker_id="section4-projection-db-disconnect"
            ),
            evidence_dir=evidence_dir,
            env={
                "DISH_SECTION4_BARRIER_SOCKET": str(barrier_path),
                "DISH_SECTION4_PROJECTION_SCENARIO": "before_effect",
                "DISH_SECTION4_EFFECT_LEDGER": str(ledger),
            },
        )
        children.append(child)
        reached = barrier.wait("projection_after_intent_before_effect", timeout_seconds=60.0)
        before_disconnect = _projection_snapshot(engine, event_id)
        primary.stop(mode="fast")
        reached.release()
        failed_exit = child.wait(timeout_seconds=60.0, check=False)
        if failed_exit == 0:
            raise ProductionShapedError(
                "projection worker unexpectedly succeeded while PostgreSQL was disconnected"
            )
    primary.start()
    _expire_projection_claim(engine, event_id)
    replacement = _run_module(
        runner,
        "dish_pg.projection_worker",
        _projection_worker_arguments(
            dsn=primary.dsn, worker_id="section4-projection-db-restart"
        ),
        env={"DISH_SECTION4_EFFECT_LEDGER": str(ledger)},
    )
    after = _projection_snapshot(engine, event_id)
    effects = _read_effect_ledger(ledger)
    if replacement.returncode != 0 or after["state"] != "applied":
        raise ProductionShapedError("projection worker did not recover after PostgreSQL restart")
    if effects.get("dispatch_calls") != 1 or effects.get("recovery_observations") != 1:
        raise ProductionShapedError(
            "projection database-disconnect recovery duplicated or omitted effects"
        )
    return {
        "status": "passed",
        "control_point": "projection_after_intent_before_effect",
        "event_id": str(event_id),
        "before_disconnect": before_disconnect,
        "failed_exit_status": failed_exit,
        "after_restart": after,
        "effect_ledger": effects,
        "store_sha256": sha256_file(store),
    }


def _reconciliation_process_loss_scenario(
    *,
    engine,
    primary: Cluster,
    generation_id: str,
    identity: str,
    evidence_dir: Path,
    runner: Runner,
    children: list[ManagedChild],
    expected_items: int,
) -> dict[str, Any]:
    before = _reconciliation_snapshot(engine)
    output = evidence_dir / "fault-reconciliation-process-loss-first.json"
    barrier_path = evidence_dir / "barriers" / "reconciliation-process-loss.sock"
    with BarrierServer(barrier_path) as barrier:
        child = _spawn_module_child(
            label="reconciliation-process-loss-old",
            module="dish_pg.reconciliation_worker",
            arguments=_reconciliation_worker_arguments(
                dsn=primary.dsn,
                generation_id=generation_id,
                identity=identity,
                output=output,
            ),
            evidence_dir=evidence_dir,
            env={
                "DISH_SECTION4_BARRIER_SOCKET": str(barrier_path),
                "DISH_SECTION4_RECONCILIATION_SCENARIO": "after_fetch_before_transaction",
            },
        )
        children.append(child)
        reached = barrier.wait(
            "reconciliation_after_fetch_before_transaction", timeout_seconds=60.0
        )
        kill = child.kill_for_fault()
        reached.close_without_release()
    after_loss = _reconciliation_snapshot(engine)
    if after_loss != before:
        raise ProductionShapedError("reconciliation process loss wrote partial durable state")
    replacement_output = evidence_dir / "fault-reconciliation-process-loss-restart.json"
    report = _reconcile(
        runner,
        dsn=primary.dsn,
        generation_id=generation_id,
        identity=identity,
        output=replacement_output,
    )
    after = _reconciliation_snapshot(engine)
    if after != before:
        raise ProductionShapedError(
            "reconciliation restart duplicated or omitted durable effects"
        )
    if report["processed_items"] != expected_items or report["outcome_counts"].get(
        "matched"
    ) != expected_items:
        raise ProductionShapedError(
            "reconciliation restart did not confirm the corpus as already reconciled"
        )
    return {
        "status": "passed",
        "control_point": "reconciliation_after_fetch_before_transaction",
        "before": before,
        "kill": kill,
        "after_loss": after_loss,
        "after_restart": after,
        "restart_report": report,
    }


def _reconciliation_database_disconnect_scenario(
    *,
    engine,
    primary: Cluster,
    generation_id: str,
    identity: str,
    evidence_dir: Path,
    runner: Runner,
    children: list[ManagedChild],
    expected_items: int,
) -> dict[str, Any]:
    before = _reconciliation_snapshot(engine)
    output = evidence_dir / "fault-reconciliation-db-disconnect-first.json"
    barrier_path = evidence_dir / "barriers" / "reconciliation-db-disconnect.sock"
    with BarrierServer(barrier_path) as barrier:
        child = _spawn_module_child(
            label="reconciliation-database-disconnect",
            module="dish_pg.reconciliation_worker",
            arguments=_reconciliation_worker_arguments(
                dsn=primary.dsn,
                generation_id=generation_id,
                identity=identity,
                output=output,
            ),
            evidence_dir=evidence_dir,
            env={
                "DISH_SECTION4_BARRIER_SOCKET": str(barrier_path),
                "DISH_SECTION4_RECONCILIATION_SCENARIO": "after_fetch_before_transaction",
            },
        )
        children.append(child)
        reached = barrier.wait(
            "reconciliation_after_fetch_before_transaction", timeout_seconds=60.0
        )
        primary.stop(mode="fast")
        reached.release()
        failed_exit = child.wait(timeout_seconds=60.0, check=False)
        if failed_exit == 0:
            raise ProductionShapedError(
                "reconciliation worker unexpectedly succeeded while PostgreSQL was disconnected"
            )
    primary.start()
    after_failure = _reconciliation_snapshot(engine)
    if after_failure != before:
        raise ProductionShapedError("database-disconnected reconciliation wrote partial state")
    replacement_output = evidence_dir / "fault-reconciliation-db-disconnect-restart.json"
    report = _reconcile(
        runner,
        dsn=primary.dsn,
        generation_id=generation_id,
        identity=identity,
        output=replacement_output,
    )
    after = _reconciliation_snapshot(engine)
    if after != before:
        raise ProductionShapedError(
            "reconciliation database recovery duplicated or omitted durable effects"
        )
    if report["processed_items"] != expected_items or report["outcome_counts"].get(
        "matched"
    ) != expected_items:
        raise ProductionShapedError(
            "reconciliation database recovery did not confirm the corpus as already reconciled"
        )
    return {
        "status": "passed",
        "control_point": "reconciliation_after_fetch_before_transaction",
        "before": before,
        "failed_exit_status": failed_exit,
        "after_failure": after_failure,
        "after_restart": after,
        "restart_report": report,
    }


def _service_process_loss_scenario(
    *,
    runtime: ServiceRuntimeClient,
    engine,
    evidence_dir: Path,
) -> dict[str, Any]:
    request_id = uuid.uuid5(uuid.NAMESPACE_URL, "dish-section4-service-process-loss")
    barrier_path = evidence_dir / "barriers" / "service-process-loss.sock"
    command = {
        "action": "command",
        "command": "create",
        "arguments": {
            "title": "Sanitized Section 4 service-loss command",
            "body": "Local-only deterministic replay evidence.",
        },
        "owner_id": runtime.owner_id,
        "run_id": runtime.run_id,
        "command_request_id": str(request_id),
        "control_point": "after_commit_before_response",
        "barrier_socket": str(barrier_path),
    }
    with BarrierServer(barrier_path) as barrier:
        pending = runtime.pending_request(command)
        reached = barrier.wait("service_after_commit_before_response", timeout_seconds=60.0)
        before_loss = _service_command_snapshot(engine, request_id)
        _assert_single_command_effect(before_loss)
        kill = runtime.kill_for_fault()
        reached.close_without_release()
        pending.finish(timeout_seconds=30.0, allow_error=True)
    restart = runtime.start()
    replay = runtime.command(
        command="create",
        arguments=dict(command["arguments"]),
        request_id=str(request_id),
    )
    if not replay.get("request_replayed"):
        raise ProductionShapedError("service restart did not replay the committed request outcome")
    after = _service_command_snapshot(engine, request_id)
    _assert_single_command_effect(after)
    if after != before_loss:
        raise ProductionShapedError("service replay changed committed command effects")
    return {
        "status": "passed",
        "control_point": "service_after_commit_before_response",
        "request_id": str(request_id),
        "before_loss": before_loss,
        "kill": kill,
        "restart": restart,
        "replay_result": replay,
        "after_replay": after,
    }


def _service_database_disconnect_scenario(
    *,
    runtime: ServiceRuntimeClient,
    engine,
    primary: Cluster,
    evidence_dir: Path,
) -> dict[str, Any]:
    request_id = uuid.uuid5(uuid.NAMESPACE_URL, "dish-section4-service-db-disconnect")
    barrier_path = evidence_dir / "barriers" / "service-db-disconnect.sock"
    command = {
        "action": "command",
        "command": "create",
        "arguments": {
            "title": "Sanitized Section 4 database-loss command",
            "body": "Local-only deterministic rollback and retry evidence.",
        },
        "owner_id": runtime.owner_id,
        "run_id": runtime.run_id,
        "command_request_id": str(request_id),
        "control_point": "after_execute_before_commit",
        "barrier_socket": str(barrier_path),
    }
    with BarrierServer(barrier_path) as barrier:
        pending = runtime.pending_request(command)
        reached = barrier.wait("service_after_execute_before_commit", timeout_seconds=60.0)
        primary.stop(mode="fast")
        reached.release()
        first_response = pending.finish(timeout_seconds=60.0, allow_error=True)
    primary.start()
    health = runtime.request({"action": "health"})
    if not health.get("ok"):
        raise ProductionShapedError("service runtime did not recover database connectivity")
    before_replay = _service_command_snapshot(engine, request_id)
    if any(before_replay[key] for key in ("request_count", "outcome_count", "execution_count")):
        raise ProductionShapedError("disconnected command transaction was partially committed")
    replay = runtime.command(
        command="create",
        arguments=dict(command["arguments"]),
        request_id=str(request_id),
    )
    if replay.get("request_replayed"):
        raise ProductionShapedError("aborted command was incorrectly classified as a replay")
    after = _service_command_snapshot(engine, request_id)
    _assert_single_command_effect(after)
    return {
        "status": "passed",
        "control_point": "service_after_execute_before_commit",
        "request_id": str(request_id),
        "first_response": first_response,
        "before_replay": before_replay,
        "health_after_restart": health,
        "replay_result": replay,
        "after_replay": after,
    }

def _artifact_hashes(root: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in {RESOURCE_MARKER, EVIDENCE_MARKER}:
            items.append({"path": str(path), "size_bytes": path.stat().st_size, "sha256": sha256_file(path)})
    return items


def _report_hash(report: dict[str, Any]) -> dict[str, Any]:
    payload = dict(report)
    payload.pop("report_sha256", None)
    report["report_sha256"] = _hash_value(payload)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-production-shaped-rehearsal")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--work-root", type=Path)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--corpus-manifest", type=Path)
    parser.add_argument("--honest-repo", type=Path)
    parser.add_argument("--honest-commit")
    parser.add_argument("--source-generation", default="section4-sanitized-production-shaped")
    parser.add_argument(
        "--repository-input-identity",
        help="identity of the received repository archive or upstream commit",
    )
    parser.add_argument("--project-id", type=uuid.UUID, default=DEFAULT_PROJECT_ID)
    parser.add_argument("--project-gid", default=DEFAULT_PROJECT_GID)
    parser.add_argument("--project-name", default="Sanitized production-shaped project")
    parser.add_argument("--section-id", type=uuid.UUID, default=DEFAULT_SECTION_ID)
    parser.add_argument("--section-gid", default=DEFAULT_SECTION_GID)
    parser.add_argument("--section-name", default="Sanitized production-shaped section")
    parser.add_argument("--pg-bin", type=Path)
    parser.add_argument(
        "--service-entry-point",
        type=Path,
        default=ROOT / "dish-service",
        help=(
            "clean Git-tracked dish-service entry point supporting "
            "--postgresql-test-runtime"
        ),
    )
    parser.add_argument("--port-base", type=int, default=DEFAULT_PORT_BASE)
    parser.add_argument("--dish-commit")
    parser.add_argument("--base-commit")
    parser.add_argument("--source-identity-kind", choices=("git_commit", "synthetic_base"), default="git_commit")
    parser.add_argument("--keep-resources", action="store_true")
    parser.add_argument(
        "--describe-input-identities",
        action="store_true",
        help="print current source/deployment identities for corpus-manifest authoring",
    )
    return parser


def _describe_input_identities(args: argparse.Namespace) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="dish-section4-identities-") as temporary:
        runner = SafeRunner(Path(temporary) / "command-logs")
        source = _source_identity(
            runner,
            expected_commit=args.dish_commit,
            expected_base=args.base_commit,
            base_kind=args.source_identity_kind,
        )
    return {
        "schema": "dish-section4-input-identities-v1",
        "source_manifest_identity": source["source_manifest"]["manifest_sha256"],
        "deployment_identity": _deployment_configuration_identity()["identity"],
        "alembic_head": ALEMBIC_HEAD,
        "source_identity": source,
    }


def _required_args(args: argparse.Namespace) -> None:
    required = (
        "report",
        "evidence_dir",
        "work_root",
        "corpus",
        "corpus_manifest",
        "honest_repo",
        "honest_commit",
        "repository_input_identity",
    )
    missing = [name for name in required if getattr(args, name, None) is None]
    if missing:
        options = ", ".join("--" + name.replace("_", "-") for name in missing)
        raise ProductionShapedError("missing required arguments: " + options)
    if not REPOSITORY_INPUT_IDENTITY.fullmatch(str(args.repository_input_identity)):
        raise ProductionShapedError(
            "--repository-input-identity must be archive-sha256:<64 lowercase hex> "
            "or git-commit:<40 lowercase hex>"
        )
    if not GIT_COMMIT.fullmatch(str(args.honest_commit)):
        raise ProductionShapedError(
            "--honest-commit must be 40 lowercase hexadecimal characters"
        )
    if not 1024 <= args.port_base <= 65000 - 4:
        raise ProductionShapedError("--port-base must reserve five unprivileged local ports")
    if args.pg_bin is not None:
        _reject_production_path(args.pg_bin, label="pg_bin")
    if getattr(args, "service_entry_point", None) is not None:
        _reject_production_path(args.service_entry_point, label="service_entry_point")


def run(args: argparse.Namespace) -> dict[str, Any]:
    _required_args(args)
    assert args.report and args.evidence_dir and args.work_root and args.corpus and args.corpus_manifest
    assert args.honest_repo and args.honest_commit
    _validate_isolation_inputs(args)
    started_at = utc_now()
    evidence_dir = _ensure_evidence_dir(args.evidence_dir)
    work_root = _ensure_owned_root(args.work_root, port_base=args.port_base)
    runner = SafeRunner(evidence_dir / "command-logs")
    phases = PhaseRecorder()
    clusters: list[Cluster] = []
    worker_children: list[ManagedChild] = []
    runtime: ServiceRuntimeClient | None = None
    source_identity: dict[str, Any] | None = None
    corpus_manifest: dict[str, Any] | None = None
    honest_identity: dict[str, Any] | None = None
    service_entry_identity: dict[str, Any] | None = None
    deployment_identity = _deployment_configuration_identity()
    blocked: list[str] = []
    cleanup_requirements: list[str] = []
    cleanup_evidence: list[dict[str, Any]] = []
    pg_version: str | None = None
    system_identifier: str | None = None
    command_inventory: list[dict[str, Any]] = [
        {
            "command": command,
            "first_attempt_status": "not_attempted",
            "code": None,
            "http_status": None,
        }
        for command in ("sections", "section-tasks", "read")
    ]
    selected_recovery_age: float | None = None
    result_status = "failed"
    phase_context: dict[str, Any] = {}
    engine = None
    work_root_removed = False
    cleanup_failed = False

    try:
        source_identity = _source_identity(
            runner,
            expected_commit=args.dish_commit,
            expected_base=args.base_commit,
            base_kind=args.source_identity_kind,
        )
        corpus_manifest = _load_corpus_manifest(args.corpus_manifest, args.corpus)
        honest_identity = _checkout_identity(
            runner,
            args.honest_repo,
            expected_commit=args.honest_commit,
            label="Honest checkout",
        )
        _validate_manifest_bindings(
            corpus_manifest,
            source_identity=source_identity,
            deployment_identity=deployment_identity,
        )
        if getattr(args, "service_entry_point", None) is not None:
            service_entry_identity = _runtime_entrypoint_identity(
                runner, args.service_entry_point
            )

        binaries = discover_pg_bin(args.pg_bin)
        pg_version = _postgres_version(runner, binaries)
        archive_dir = work_root / "wal-archive"
        _, restore_helper = _write_archive_helpers(work_root, archive_dir)
        primary = Cluster(
            "section4-primary",
            work_root / "primary-data",
            work_root / "primary-socket",
            args.port_base,
            DATABASE_PREFIX + "primary_test",
            binaries,
            runner,
            archive_dir,
        )
        clusters.append(primary)
        primary.init()
        primary.start()
        primary.create_database()
        system_identifier = primary.system_identifier()
        engine = create_engine(
            primary.dsn,
            future=True,
            pool_pre_ping=True,
            connect_args={"connect_timeout": 5},
        )
        identity = corpus_identity(args.corpus, corpus_manifest["corpus_sha256"])

        phases.run(
            "postgresql_migration",
            lambda: (
                _alembic_upgrade(runner, primary.dsn)
                or {"alembic_head": ALEMBIC_HEAD, "execution_status": "implemented_and_passed"}
            ),
        )

        def import_phase() -> Mapping[str, Any]:
            receipt = evidence_dir / "bootstrap-receipt.json"
            _run_module(
                runner,
                "dish_pg.bootstrap",
                [
                    "--database-url",
                    primary.dsn,
                    "--expected-database-name",
                    primary.database,
                    "--source",
                    args.corpus,
                    "--source-generation",
                    args.source_generation,
                    "--dish-repo",
                    REPOSITORY,
                    "--dish-commit",
                    source_identity["current_commit"],
                    "--honest-repo",
                    args.honest_repo,
                    "--honest-commit",
                    args.honest_commit,
                    "--schema-head",
                    ALEMBIC_HEAD,
                    "--project-id",
                    str(args.project_id),
                    "--project-gid",
                    args.project_gid,
                    "--project-name",
                    args.project_name,
                    "--section-id",
                    str(args.section_id),
                    "--section-gid",
                    args.section_gid,
                    "--section-name",
                    args.section_name,
                    "--receipt",
                    receipt,
                ],
            )
            boot = json.loads(receipt.read_text(encoding="utf-8"))
            imported = _run_module(
                runner,
                "dish_pg.import_runtime",
                [
                    "--database-url",
                    primary.dsn,
                    "--source",
                    args.corpus,
                    "--expected-source-sha256",
                    corpus_manifest["corpus_sha256"],
                    "--expected-record-count",
                    str(corpus_manifest["record_count"]),
                    "--generation-id",
                    boot["generation_id"],
                    "--import-run-id",
                    boot["import_run_id"],
                    "--contract-binding-id",
                    boot["binding_id"],
                ],
            )
            import_result = json.loads(imported.stdout)
            first_record = json.loads(
                next(
                    line
                    for line in args.corpus.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                )
            )
            phase_context.update(
                {
                    "bootstrap": boot,
                    "task_id": first_record["task_id"],
                    "task_uuid": uuid.UUID(first_record["task_id"]),
                }
            )
            return {
                "execution_status": "implemented_and_passed",
                "bootstrap_receipt": str(receipt),
                "bootstrap_receipt_sha256": sha256_file(receipt),
                "generation_id": boot["generation_id"],
                "import_result": import_result,
            }

        phases.run("corpus_import", import_phase)

        def reconciliation_phase() -> Mapping[str, Any]:
            setup = _prepare_projection(
                engine,
                uuid.UUID(phase_context["bootstrap"]["generation_id"]),
                args.corpus,
                evidence_dir / "local-projection.json",
            )
            phase_context["projection"] = setup
            report = _reconcile(
                runner,
                dsn=primary.dsn,
                generation_id=phase_context["bootstrap"]["generation_id"],
                identity=identity,
                output=evidence_dir / "reconciliation-initial.json",
            )
            return {
                "execution_status": "implemented_and_passed",
                "projection_setup": setup,
                "reconciliation": report,
            }

        phases.run("reconciliation", reconciliation_phase)

        def startup_phase() -> tuple[str, str, Mapping[str, Any]]:
            projection_worker = _run_module(
                runner,
                "dish_pg.projection_worker",
                _projection_worker_arguments(
                    dsn=primary.dsn, worker_id="section4-startup-projection-worker"
                ),
            )
            local_projection = evidence_dir / "local-projection.json"
            if projection_worker.returncode != 0 or not local_projection.is_file():
                raise ProductionShapedError(
                    "local projection worker did not materialize its observation"
                )
            startup_reconciliation = _reconcile(
                runner,
                dsn=primary.dsn,
                generation_id=phase_context["bootstrap"]["generation_id"],
                identity=identity,
                output=evidence_dir / "reconciliation-startup-worker.json",
            )
            worker_details = {
                "projection_worker": {
                    "first_attempt_status": "passed",
                    "local_projection_sha256": sha256_file(local_projection),
                },
                "reconciliation_worker": {
                    "first_attempt_status": "passed",
                    "report": startup_reconciliation,
                },
            }
            nonlocal runtime
            if service_entry_identity is None:
                blocked.append(RUNTIME_UNAVAILABLE)
                return (
                    "blocked",
                    "blocked_runtime_infrastructure",
                    {
                        "execution_status": "implemented_but_blocked",
                        "reason": RUNTIME_UNAVAILABLE,
                        "service": {
                            "implementation_status": "implemented",
                            "first_attempt_status": "blocked_runtime_unavailable",
                        },
                        "workers": worker_details,
                    },
                )
            owner_id, run_id = _register_runtime_run(
                engine,
                generation_id=uuid.UUID(phase_context["bootstrap"]["generation_id"]),
                corpus_sha256=corpus_manifest["corpus_sha256"],
            )
            runtime = ServiceRuntimeClient(
                entry_point=args.service_entry_point.expanduser().resolve(),
                database_url=primary.dsn,
                expected_database=primary.database,
                expected_schema_head=ALEMBIC_HEAD,
                expected_release=phase_context["bootstrap"]["dish_release"],
                generation_id=phase_context["bootstrap"]["generation_id"],
                owner_id=owner_id,
                run_id=run_id,
                evidence_dir=evidence_dir,
                cwd=ROOT,
                env=_safe_child_env(),
                log_path=evidence_dir / "process-logs" / "section4-test-service.log",
                python_executable=sys.executable,
            )
            service = runtime.start()
            phase_context["runtime_owner_id"] = owner_id
            phase_context["runtime_run_id"] = run_id
            return (
                "passed",
                "available",
                {
                    "execution_status": "implemented_and_passed",
                    "service": {
                        "first_attempt_status": "passed",
                        "entry_point_identity": service_entry_identity,
                        **service,
                    },
                    "workers": worker_details,
                },
            )

        phases.classified("service_and_worker_startup", startup_phase)

        planned_commands = [
            {"command": "sections", "arguments": {}},
            {
                "command": "section-tasks",
                "arguments": {"section_id": str(args.section_id), "page_size": 10},
            },
            {"command": "read", "arguments": {"task_id": phase_context["task_id"]}},
        ]

        def representative_phase() -> tuple[str, str, Mapping[str, Any]]:
            nonlocal command_inventory
            if runtime is None:
                command_inventory = [
                    {
                        "command": item["command"],
                        "first_attempt_status": "blocked_runtime_unavailable",
                        "code": None,
                        "http_status": None,
                    }
                    for item in planned_commands
                ]
                return (
                    "blocked",
                    "blocked_runtime_infrastructure",
                    {
                        "execution_status": "implemented_but_blocked",
                        "reason": RUNTIME_UNAVAILABLE,
                        "planned_commands": planned_commands,
                        "command_inventory": command_inventory,
                    },
                )
            results: list[dict[str, Any]] = []
            inventory: list[dict[str, Any]] = []
            for item in planned_commands:
                result = runtime.command(
                    command=item["command"],
                    arguments=item["arguments"],
                    request_id=None,
                )
                serialized = _canonical_json(result)
                results.append(
                    {
                        "command": item["command"],
                        "arguments": item["arguments"],
                        "result": result,
                        "result_sha256": hashlib.sha256(serialized).hexdigest(),
                    }
                )
                inventory.append(
                    {
                        "command": item["command"],
                        "first_attempt_status": "passed",
                        "code": None,
                        "http_status": None,
                    }
                )
            command_inventory = inventory
            return (
                "passed",
                "available",
                {
                    "execution_status": "implemented_and_passed",
                    "commands": results,
                    "command_inventory": command_inventory,
                    "public_action_route_contacted": False,
                },
            )

        phases.classified("representative_commands", representative_phase)

        def fault_phase() -> tuple[str, str, Mapping[str, Any]]:
            generation_id = uuid.UUID(phase_context["bootstrap"]["generation_id"])
            import_run_id = uuid.UUID(phase_context["bootstrap"]["import_run_id"])
            binding_id = uuid.UUID(phase_context["bootstrap"]["binding_id"])
            process_loss_task_id = _import_projection_fault_task(
                engine,
                generation_id=generation_id,
                import_run_id=import_run_id,
                contract_binding_id=binding_id,
                project_id=args.project_id,
                section_id=args.section_id,
                scenario="process-loss",
            )
            database_disconnect_task_id = _import_projection_fault_task(
                engine,
                generation_id=generation_id,
                import_run_id=import_run_id,
                contract_binding_id=binding_id,
                project_id=args.project_id,
                section_id=args.section_id,
                scenario="database-disconnect",
            )
            worker_scenarios = {
                "projection_worker_loss_and_restart": _projection_process_loss_scenario(
                    engine=engine,
                    primary=primary,
                    generation_id=generation_id,
                    task_id=process_loss_task_id,
                    evidence_dir=evidence_dir,
                    runner=runner,
                    children=worker_children,
                ),
                "projection_worker_postgresql_disconnect": _projection_database_disconnect_scenario(
                    engine=engine,
                    primary=primary,
                    generation_id=generation_id,
                    task_id=database_disconnect_task_id,
                    evidence_dir=evidence_dir,
                    runner=runner,
                    children=worker_children,
                ),
                "reconciliation_worker_loss_and_restart": _reconciliation_process_loss_scenario(
                    engine=engine,
                    primary=primary,
                    generation_id=phase_context["bootstrap"]["generation_id"],
                    identity=identity,
                    evidence_dir=evidence_dir,
                    runner=runner,
                    children=worker_children,
                    expected_items=int(corpus_manifest["record_count"]),
                ),
                "reconciliation_worker_postgresql_disconnect": _reconciliation_database_disconnect_scenario(
                    engine=engine,
                    primary=primary,
                    generation_id=phase_context["bootstrap"]["generation_id"],
                    identity=identity,
                    evidence_dir=evidence_dir,
                    runner=runner,
                    children=worker_children,
                    expected_items=int(corpus_manifest["record_count"]),
                ),
            }
            if (
                process_loss_task_id == database_disconnect_task_id
                or process_loss_task_id == phase_context["task_uuid"]
                or database_disconnect_task_id == phase_context["task_uuid"]
            ):
                raise ProductionShapedError(
                    "projection fault scenarios did not receive isolated tasks"
                )
            if runtime is None:
                return (
                    "blocked",
                    "blocked_runtime_infrastructure",
                    {
                        "execution_status": "implemented_but_blocked",
                        "worker_scenarios": worker_scenarios,
                        "service_process_loss": {
                            "implementation_status": "implemented",
                            "status": "blocked",
                            "reason": RUNTIME_UNAVAILABLE,
                        },
                        "postgresql_disconnect_during_governed_command": {
                            "implementation_status": "implemented",
                            "status": "blocked",
                            "reason": RUNTIME_UNAVAILABLE,
                        },
                        "duplicate_effect_verification": {
                            "command": "blocked_runtime_unavailable",
                            "projection": "passed",
                            "reconciliation": "passed",
                        },
                        "projection_fault_task_ids": {
                            "process_loss": str(process_loss_task_id),
                            "database_disconnect": str(database_disconnect_task_id),
                        },
                    },
                )
            service_loss = _service_process_loss_scenario(
                runtime=runtime, engine=engine, evidence_dir=evidence_dir
            )
            command_disconnect = _service_database_disconnect_scenario(
                runtime=runtime,
                engine=engine,
                primary=primary,
                evidence_dir=evidence_dir,
            )
            return (
                "passed",
                "available",
                {
                    "execution_status": "implemented_and_passed",
                    "worker_scenarios": worker_scenarios,
                    "service_process_loss": service_loss,
                    "postgresql_disconnect_during_governed_command": command_disconnect,
                    "duplicate_effect_verification": {
                        "command": "passed",
                        "projection": "passed",
                        "reconciliation": "passed",
                    },
                    "projection_fault_task_ids": {
                        "process_loss": str(process_loss_task_id),
                        "database_disconnect": str(database_disconnect_task_id),
                    },
                    "synchronization": "explicit_unix_socket_barriers_no_fault_sleeps",
                },
            )

        phases.classified("process_and_database_fault_injection", fault_phase)

        def backup_phase() -> Mapping[str, Any]:
            backup, duration, evidence, restart = _physical_backup(
                primary,
                work_root / "physical-backup",
                system_identifier,
                inject_rename_fault=False,
            )
            phase_context["backup"] = backup
            return {
                "execution_status": "implemented_and_passed",
                "backup_path": str(backup),
                "duration_seconds": duration,
                "backup_evidence": asdict(evidence),
                "restart_reconciliation": restart,
                "reused_section2_function": "dish_pg.recovery_rehearsal._physical_backup",
            }

        phases.run("physical_backup", backup_phase)

        def restore_phase() -> Mapping[str, Any]:
            restored = Cluster(
                "section4-independent-restore",
                work_root / "restore-data",
                work_root / "restore-socket",
                args.port_base + 1,
                primary.database,
                binaries,
                runner,
            )
            clusters.append(restored)
            copy_duration = _copy_backup(phase_context["backup"], restored.data_dir)
            _configure_restored_cluster(restored)
            restored.start()
            restored_engine = create_engine(
                restored.dsn,
                future=True,
                pool_pre_ping=True,
                connect_args={"connect_timeout": 5},
            )
            try:
                verification = _verify_database(
                    restored_engine,
                    expected_min_reconciliations=1,
                    task_id=phase_context["task_id"],
                )
                recon = _reconcile(
                    runner,
                    dsn=restored.dsn,
                    generation_id=phase_context["bootstrap"]["generation_id"],
                    identity=identity,
                    output=evidence_dir / "reconciliation-independent-restore.json",
                )
            finally:
                restored_engine.dispose()
                restored.stop()
            return {
                "execution_status": "implemented_and_passed",
                "copy_duration_seconds": copy_duration,
                "verification": verification,
                "reconciliation": recon,
                "command_service_verification": (
                    "runtime_entry_point_bound"
                    if service_entry_identity is not None
                    else "implemented_but_runtime_unavailable"
                ),
                "reused_section2_function": "dish_pg.recovery_rehearsal._copy_backup",
            }

        phases.run("independent_restore", restore_phase)

        def pitr_phase() -> Mapping[str, Any]:
            boundaries: list[RecoveryBoundary] = []
            for index in (1, 2):
                _reconcile(
                    runner,
                    dsn=primary.dsn,
                    generation_id=phase_context["bootstrap"]["generation_id"],
                    identity=identity,
                    output=evidence_dir / f"reconciliation-boundary-{index}.json",
                )
                count = _reconciliation_count(engine)
                boundary = _boundary(engine, f"section4-boundary-{index}", count)
                boundaries.append(boundary)
                _force_archive(engine, archive_dir)
            results: list[dict[str, Any]] = []
            for index, boundary in enumerate(boundaries, start=1):
                pitr = Cluster(
                    f"section4-pitr-{index}",
                    work_root / f"pitr-{index}-data",
                    work_root / f"pitr-{index}-socket",
                    args.port_base + 1 + index,
                    primary.database,
                    binaries,
                    runner,
                )
                clusters.append(pitr)
                copy_duration = _copy_backup(phase_context["backup"], pitr.data_dir)
                _configure_pitr(pitr, restore_helper, boundary.lsn)
                pitr.start()
                pitr_engine = create_engine(
                    pitr.dsn,
                    future=True,
                    pool_pre_ping=True,
                    connect_args={"connect_timeout": 5},
                )
                try:
                    promotion = _wait_for_promotion(pitr_engine, boundary.lsn)
                    verification = _verify_database(
                        pitr_engine,
                        expected_min_reconciliations=boundary.expected_reconciliation_runs,
                        task_id=phase_context["task_id"],
                    )
                finally:
                    pitr_engine.dispose()
                    pitr.stop()
                results.append(
                    {
                        "boundary": asdict(boundary),
                        "copy_duration_seconds": copy_duration,
                        "promotion": promotion,
                        "verification": verification,
                    }
                )
            chosen = boundaries[-1]
            chosen_time = datetime.fromisoformat(chosen.committed_at.replace("Z", "+00:00"))
            nonlocal selected_recovery_age
            selected_recovery_age = max(0.0, (utc_now() - chosen_time).total_seconds())
            return {
                "execution_status": "implemented_and_passed",
                "boundaries": results,
                "selected_boundary": chosen.label,
                "selected_recovery_point_age_seconds": selected_recovery_age,
                "reused_section2_functions": [
                    "_copy_backup",
                    "_configure_pitr",
                    "_wait_for_promotion",
                    "_force_archive",
                ],
            }

        phases.run("point_in_time_recovery", pitr_phase)

        def final_phase() -> Mapping[str, Any]:
            report = _reconcile(
                runner,
                dsn=primary.dsn,
                generation_id=phase_context["bootstrap"]["generation_id"],
                identity=identity,
                output=evidence_dir / "reconciliation-final.json",
            )
            verification = _verify_database(
                engine,
                expected_min_reconciliations=1,
                task_id=phase_context["task_id"],
            )
            return {
                "execution_status": "implemented_and_passed",
                "reconciliation": report,
                "verification": verification,
            }

        phases.run("final_reconciliation_and_evidence", final_phase)
        if all(item.status == "passed" for item in phases.items):
            result_status = "passed"
        elif any(item.status == "failed" for item in phases.items):
            result_status = "failed"
        else:
            result_status = "blocked"
    except RehearsalBlocked as exc:
        result_status = "blocked"
        reason = str(exc)
        blocked.append(reason)
        if exc.missing_commands:
            blocked.append("missing_native_commands:" + ",".join(exc.missing_commands))
        phases.fill_remaining_blocked(
            reason=reason,
            availability_status="blocked_native_infrastructure",
        )
    except Exception as exc:
        result_status = "failed"
        reason = f"{type(exc).__name__}: {exc}"
        blocked.append(reason)
        phases.fill_remaining_blocked(
            reason="upstream phase failed: " + reason,
            availability_status="blocked_upstream_failure",
        )
    finally:
        all_children: list[ManagedChild] = list(worker_children)
        if runtime is not None:
            all_children.extend(runtime.children)
        cleanup = _cleanup_owned_resources(
            children=all_children,
            clusters=clusters,
            engine=engine,
            work_root=work_root,
            keep_resources=args.keep_resources,
        )
        cleanup_evidence.extend(cleanup.evidence)
        cleanup_requirements.extend(cleanup.requirements)
        cleanup_failed = cleanup.failed
        work_root_removed = cleanup.work_root_removed
        if cleanup_failed:
            result_status = "failed"

    ended_at = utc_now()
    cleanup_requirements.append(
        "choose a new empty evidence directory for any rerun: " + str(evidence_dir)
    )
    cleanup_requirements.append(
        "preserve or relocate the immutable report before any rerun: "
        + str(args.report.expanduser().resolve())
    )
    phase_rows = [asdict(item) for item in phases.items]
    not_implemented = [
        item["name"]
        for item in phase_rows
        if item.get("implementation_status") == "not_implemented"
    ]
    if not_implemented:
        result_status = "failed"
        blocked.append("not_implemented_phases:" + ",".join(not_implemented))
    if result_status == "passed" and (
        len(phase_rows) != len(PHASES)
        or any(item["status"] != "passed" for item in phase_rows)
    ):
        result_status = "failed"
        blocked.append("completion rule rejected passed status because not all ten phases passed")

    report = {
        "schema": REPORT_SCHEMA,
        "section": "4-production-shaped-local-postgresql-rehearsal",
        "status": result_status,
        "ok": result_status == "passed",
        "started_at": iso(started_at),
        "ended_at": iso(ended_at),
        "duration_seconds": (ended_at - started_at).total_seconds(),
        "source_identity": source_identity,
        "alembic_head": ALEMBIC_HEAD,
        "postgresql": {"version": pg_version, "system_identifier": system_identifier},
        "deployment_configuration_identity": deployment_identity,
        "service_runtime_entry_point_identity": service_entry_identity,
        "sanitized_corpus": corpus_manifest,
        "input_identity": {
            "honest_checkout": honest_identity,
            "source_generation": args.source_generation,
            "repository_input_identity": args.repository_input_identity,
        },
        "safety": {
            "resource_scope": "isolated local or TEST only",
            "arbitrary_database_url_accepted": False,
            "production_services_contacted": False,
            "production_postgresql_contacted": False,
            "production_asana_contacted": False,
            "public_action_route_contacted": False,
            "production_credentials_inherited": False,
            "fail_closed_triggered": result_status in {"blocked", "failed"},
            "source_and_corpus_verified_before_native_execution": (
                source_identity is not None
                and corpus_manifest is not None
                and honest_identity is not None
            ),
        },
        "phases": phase_rows,
        "required_phase_order": list(PHASES),
        "phase_completion_rule": "passed only when all ten implemented phases ran and passed",
        "implemented_and_passed_phases": [
            item["name"] for item in phase_rows if item["status"] == "passed"
        ],
        "implemented_but_blocked_phases": [
            item["name"]
            for item in phase_rows
            if item["implementation_status"] == "implemented"
            and item["status"] == "blocked"
        ],
        "failed_phases": [
            item["name"] for item in phase_rows if item["status"] == "failed"
        ],
        "not_implemented_phases": not_implemented,
        "command_inventory": command_inventory,
        "external_commands": [asdict(item) for item in runner.commands],
        "managed_processes": [
            child.evidence()
            for child in (
                list(worker_children) + ([] if runtime is None else list(runtime.children))
            )
        ],
        "evidence_artifacts": _artifact_hashes(evidence_dir),
        "selected_recovery_point_age_seconds": selected_recovery_age,
        "local_measurement_limitations": {
            "production_rpo_claimed": False,
            "production_rto_claimed": False,
            "local_durations_are_production_estimates": False,
        },
        "blocked_scenarios": blocked,
        "cleanup": {
            "status": "failed" if cleanup_failed else "passed",
            "work_root": str(work_root),
            "work_root_removed": work_root_removed,
            "resources_confirmed_stopped_before_removal": not cleanup_failed,
            "evidence": cleanup_evidence,
        },
        "cleanup_requirements": cleanup_requirements,
        "section2_reuse": [
            "Cluster",
            "discover_pg_bin",
            "_alembic_upgrade",
            "_physical_backup",
            "_copy_backup",
            "_configure_restored_cluster",
            "_configure_pitr",
            "_wait_for_promotion",
            "_force_archive",
        ],
    }
    return _report_hash(report)


def _safe_report_target(path: Path | None) -> Path | None:
    if path is None:
        return None
    resolved = path.expanduser().resolve()
    _reject_production_path(resolved, label="report")
    if resolved == REPOSITORY or _is_within(resolved, REPOSITORY):
        raise ProductionShapedError("report must be outside the repository worktree")
    if resolved.exists():
        raise ProductionShapedError("report path already exists; refusing overwrite")
    return resolved


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.describe_input_identities:
        try:
            identities = _describe_input_identities(args)
        except Exception as exc:
            print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(identities, sort_keys=True, indent=2))
        return 0
    try:
        report = run(args)
    except Exception as exc:
        report = _report_hash(
            {
                "schema": REPORT_SCHEMA,
                "section": "4-production-shaped-local-postgresql-rehearsal",
                "status": "failed",
                "ok": False,
                "blocked_scenarios": [f"{type(exc).__name__}: {exc}"],
                "cleanup_requirements": [],
                "local_measurement_limitations": {
                    "production_rpo_claimed": False,
                    "production_rto_claimed": False,
                    "local_durations_are_production_estimates": False,
                },
            }
        )
    report_target: Path | None = None
    try:
        report_target = _safe_report_target(args.report)
    except ProductionShapedError as exc:
        if not any(str(exc) in item for item in report.get("blocked_scenarios", [])):
            report.setdefault("blocked_scenarios", []).append(
                f"ProductionShapedError: {exc}"
            )
        report["ok"] = False
        report["status"] = "failed"
        report = _report_hash(report)
    if report_target is not None:
        atomic_json(report_target, report)
    print(json.dumps(report, sort_keys=True, indent=2))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
