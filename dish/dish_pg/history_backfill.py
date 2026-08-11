"""One-task terminal legacy-history backfill with immutable supplemental provenance."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import tempfile
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from sqlalchemy import Engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from . import models
from . import stage6_models as release_models
from .database import DatabaseSettings, create_database_engine, session_factory, session_scope
from .importer import operation_history_from_mapping
from .legacy_source import (
    LegacySourceError,
    _require_operation_history_tables,
    _terminal_operation_history,
)
from .release import ALEMBIC_HEAD
from .release_history import (
    EXACT_REVOCATION_HISTORY_PROVENANCE_KEY,
    EXACT_REVOCATION_RECONCILIATION_CONTRACT,
    EXACT_REVOCATION_RECONCILED_OPERATIONS_KEY,
    EXACT_REVOCATION_SNAPSHOT_FORMAT,
    SUPPLEMENTAL_HISTORY_ATTESTATION_CONTRACT,
    TERMINAL_HISTORY_IMPORT_KIND,
    acquire_generation_release_gate,
    legacy_imported_operation_ids,
    task_revocation_history_reconciled,
)
from .repositories import AuthorityRepository, CoreAuthorityError
from .services import CoreAuthorityService, ImportedOperationHistorySpec

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_FORMAT = EXACT_REVOCATION_SNAPSHOT_FORMAT
IMPORT_KIND = TERMINAL_HISTORY_IMPORT_KIND
_COMMIT_RE = re.compile(r"[0-9a-f]{40,64}\Z")


class TerminalHistoryBackfillError(ValueError):
    """The requested one-task history backfill is unsafe or inconsistent."""


@dataclass(frozen=True)
class BackfillTarget:
    generation_id: uuid.UUID
    task_id: uuid.UUID
    primary_import_run_id: uuid.UUID
    contract_binding_id: uuid.UUID
    legacy_generation_id: str


@dataclass(frozen=True)
class TerminalHistorySnapshot:
    path: Path
    task_gid: str
    task_id: uuid.UUID
    sha256: str
    record_count: int
    high_water_mark: str
    history: ImportedOperationHistorySpec


@dataclass(frozen=True)
class TerminalHistoryBackfillResult:
    task_gid: str
    task_id: uuid.UUID
    generation_id: uuid.UUID
    primary_import_run_id: uuid.UUID
    supplemental_import_run_id: uuid.UUID | None
    source_sha256: str
    source_record_count: int
    baseline_high_water_mark: str
    matched_operations: int
    inserted_operations: int
    matched_verification_cycles: int
    inserted_verification_cycles: int
    matched_leases: int
    inserted_leases: int
    matched_revocations: int
    inserted_revocations: int
    candidate_attestation: str = SUPPLEMENTAL_HISTORY_ATTESTATION_CONTRACT

    def as_json(self) -> dict[str, object]:
        value = asdict(self)
        for key in (
            "task_id",
            "generation_id",
            "primary_import_run_id",
            "supplemental_import_run_id",
        ):
            if value[key] is not None:
                value[key] = str(value[key])
        return value


def _canonical_task_gid(value: str) -> str:
    task_gid = value.strip()
    if not task_gid.isdigit() or task_gid.startswith("0"):
        raise TerminalHistoryBackfillError(
            "task GID must be a canonical positive decimal Asana GID"
        )
    return task_gid


def _atomic_bytes(path: Path, payload: bytes) -> Path:
    destination = path.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        try:
            existing = destination.read_bytes()
        except OSError as exc:
            raise TerminalHistoryBackfillError(
                f"cannot verify existing terminal-history snapshot: {destination}"
            ) from exc
        if existing != payload:
            raise TerminalHistoryBackfillError(
                "terminal-history snapshot path already contains different evidence; "
                "choose a new --snapshot-output path"
            )
        return destination
    fd, raw_temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(raw_temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
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
    return destination


def _explicit_terminal_gate(conn: sqlite3.Connection, *, task_gid: str) -> None:
    operation = conn.execute(
        """SELECT operation_id,status FROM operations
             WHERE task_gid=? AND status NOT IN ('completed','cancelled')
             ORDER BY created_at,operation_id LIMIT 1""",
        (task_gid,),
    ).fetchone()
    if operation is not None:
        raise TerminalHistoryBackfillError(
            "terminal-history backfill is not eligible: "
            f"task_gid={task_gid} operation_id={operation['operation_id']} "
            f"status={operation['status']} is non-terminal"
        )

    cycle = conn.execute(
        """SELECT cycle_id,operation_id FROM verification_cycles
             WHERE task_gid=? AND (completed_at IS NULL OR outcome IS NULL)
             ORDER BY created_at,cycle_id LIMIT 1""",
        (task_gid,),
    ).fetchone()
    if cycle is not None:
        raise TerminalHistoryBackfillError(
            "terminal-history backfill is not eligible: "
            f"task_gid={task_gid} cycle_id={cycle['cycle_id']} "
            f"operation_id={cycle['operation_id']} verification cycle is open"
        )

    lease = conn.execute(
        """SELECT lease_id,operation_id FROM service_leases
             WHERE task_gid=? AND released_at IS NULL
             ORDER BY acquired_at,lease_id LIMIT 1""",
        (task_gid,),
    ).fetchone()
    if lease is not None:
        raise TerminalHistoryBackfillError(
            "terminal-history backfill is not eligible: "
            f"task_gid={task_gid} lease_id={lease['lease_id']} "
            f"operation_id={lease['operation_id']} service lease is active"
        )


def _history_high_water_mark(
    *, task_gid: str, history: ImportedOperationHistorySpec, source_sha256: str
) -> str:
    terminal_times = [
        item.completed_at for item in history.operations if item.completed_at is not None
    ]
    terminal_times.extend(
        item.completed_at
        for item in history.verification_cycles
        if item.completed_at is not None
    )
    terminal_times.extend(item.released_at for item in history.leases if item.released_at is not None)
    terminal_times.extend(item.revoked_at for item in history.revocations)
    if terminal_times:
        latest = max(
            value.astimezone(timezone.utc)
            if value.tzinfo is not None
            else value.replace(tzinfo=timezone.utc)
            for value in terminal_times
        )
        marker = latest.isoformat(timespec="microseconds").replace("+00:00", "Z")
    else:
        marker = "none"
    high_water = (
        f"terminal-history:{task_gid};terminal-at:{marker};bundle-sha256:{source_sha256}"
    )
    if len(high_water) > 256:
        raise TerminalHistoryBackfillError("supplemental import high-water mark exceeds schema limit")
    return high_water


def capture_terminal_history_snapshot(
    *,
    legacy_database: Path,
    task_gid: str,
    task_id: uuid.UUID,
    output: Path,
) -> TerminalHistorySnapshot:
    """Fail closed on any open legacy history, then write one exact immutable source record."""
    canonical_gid = _canonical_task_gid(task_gid)
    try:
        database = legacy_database.expanduser().resolve(strict=True)
    except OSError as exc:
        raise TerminalHistoryBackfillError(
            f"legacy SQLite database is unavailable: {legacy_database}"
        ) from exc
    uri = f"file:{database}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN")
        _require_operation_history_tables(conn)
        exists = conn.execute(
            "SELECT 1 FROM task_content_state WHERE task_gid=?", (canonical_gid,)
        ).fetchone()
        if exists is None:
            raise TerminalHistoryBackfillError(
                f"legacy task is absent from task_content_state: {canonical_gid}"
            )
        _explicit_terminal_gate(conn, task_gid=canonical_gid)
        history_mapping = _terminal_operation_history(
            conn, task_gid=canonical_gid, allow_open_operations=False
        )
        history = operation_history_from_mapping({"operation_history": history_mapping})
    except (sqlite3.Error, LegacySourceError, CoreAuthorityError, ValueError) as exc:
        if isinstance(exc, TerminalHistoryBackfillError):
            raise
        raise TerminalHistoryBackfillError(str(exc)) from exc
    finally:
        conn.close()

    record = {
        "format": SNAPSHOT_FORMAT,
        "task_id": str(task_id),
        "legacy_task_gid": canonical_gid,
        "operation_history": history_mapping,
    }
    payload = (
        json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        + b"\n"
    )
    digest = hashlib.sha256(payload).hexdigest()
    destination = _atomic_bytes(output, payload)
    return TerminalHistorySnapshot(
        path=destination,
        task_gid=canonical_gid,
        task_id=task_id,
        sha256=digest,
        record_count=1,
        high_water_mark=_history_high_water_mark(
            task_gid=canonical_gid, history=history, source_sha256=digest
        ),
        history=history,
    )


def resolve_backfill_target(session: Session, *, task_gid: str) -> BackfillTarget:
    canonical_gid = _canonical_task_gid(task_gid)
    alias = session.scalar(
        select(models.TaskExternalAlias).where(
            models.TaskExternalAlias.external_system == "asana",
            models.TaskExternalAlias.external_id == canonical_gid,
            models.TaskExternalAlias.state == "active",
        )
    )
    if alias is None:
        raise TerminalHistoryBackfillError(
            f"PostgreSQL DishTask is absent for legacy task GID {canonical_gid}"
        )
    task = session.get(models.DishTask, alias.task_id)
    if task is None:
        raise TerminalHistoryBackfillError(
            f"active task alias points to a missing DishTask: {canonical_gid}"
        )
    if task.creation_route != "import" or task.import_run_id is None:
        raise TerminalHistoryBackfillError(
            "terminal-history backfill applies only to a task created by the legacy bootstrap import"
        )
    primary_run = session.get(models.ImportRun, task.import_run_id)
    if primary_run is None or primary_run.status != "complete":
        raise TerminalHistoryBackfillError(
            "task bootstrap provenance does not reference a complete ImportRun"
        )
    generation = session.scalar(
        select(models.AuthorityGeneration)
        .where(models.AuthorityGeneration.status == "active")
        .execution_options(populate_existing=True)
    )
    if generation is None:
        raise TerminalHistoryBackfillError("no active PostgreSQL authority generation exists")
    if session.get(models.TaskAuthorityHead, (generation.generation_id, task.task_id)) is None:
        raise TerminalHistoryBackfillError(
            "existing DishTask is not present in the active PostgreSQL generation"
        )
    imported_versions = tuple(
        session.scalars(
            select(models.ContentVersion).where(
                models.ContentVersion.generation_id == generation.generation_id,
                models.ContentVersion.task_id == task.task_id,
                models.ContentVersion.creator_route == "import",
                models.ContentVersion.import_run_id == task.import_run_id,
            )
        )
    )
    if len(imported_versions) != 1:
        raise TerminalHistoryBackfillError(
            "task bootstrap contract provenance is ambiguous or missing"
        )
    binding = session.get(
        models.HonestContractBinding, imported_versions[0].contract_binding_id
    )
    if binding is None or binding.dish_release != generation.dish_release:
        raise TerminalHistoryBackfillError(
            "task bootstrap contract binding does not match the active generation"
        )
    blocked_candidates = tuple(
        session.scalars(
            select(release_models.ReleaseCandidate)
            .where(
                release_models.ReleaseCandidate.generation_id == generation.generation_id,
                release_models.ReleaseCandidate.status.in_(("validated", "approved", "activated")),
            )
            .execution_options(populate_existing=True)
        )
    )
    if blocked_candidates:
        statuses = sorted({candidate.status for candidate in blocked_candidates})
        raise TerminalHistoryBackfillError(
            "terminal-history backfill is blocked after release-candidate validation so the "
            "validated corpus cannot change; "
            f"candidate_statuses={statuses}"
        )
    return BackfillTarget(
        generation_id=generation.generation_id,
        task_id=task.task_id,
        primary_import_run_id=task.import_run_id,
        contract_binding_id=binding.binding_id,
        legacy_generation_id=primary_run.legacy_generation_id,
    )


def _same_target(left: BackfillTarget, right: BackfillTarget) -> bool:
    return left == right


def _find_supplemental_run(
    session: Session,
    *,
    target: BackfillTarget,
    snapshot: TerminalHistorySnapshot,
) -> models.ImportRun | None:
    run = session.scalar(
        select(models.ImportRun).where(
            models.ImportRun.legacy_generation_id == target.legacy_generation_id,
            models.ImportRun.baseline_high_water_mark == snapshot.high_water_mark,
        )
    )
    if run is None:
        return None
    provenance: Mapping[str, object] = run.provenance or {}
    if (
        run.status != "complete"
        or run.source_bundle_sha256 != snapshot.sha256
        or provenance.get("import_kind") != IMPORT_KIND
        or provenance.get("task_id") != str(target.task_id)
        or provenance.get("legacy_task_gid") != snapshot.task_gid
        or provenance.get("primary_import_run_id") != str(target.primary_import_run_id)
        or provenance.get("source_format") != SNAPSHOT_FORMAT
        or provenance.get("source_record_count") != snapshot.record_count
        or provenance.get(EXACT_REVOCATION_HISTORY_PROVENANCE_KEY)
        != EXACT_REVOCATION_RECONCILIATION_CONTRACT
        or provenance.get(EXACT_REVOCATION_RECONCILED_OPERATIONS_KEY)
        != sorted(str(item.operation_id) for item in snapshot.history.operations)
        or provenance.get("candidate_attestation")
        != SUPPLEMENTAL_HISTORY_ATTESTATION_CONTRACT
    ):
        raise TerminalHistoryBackfillError(
            "existing ImportRun collides with this supplemental high-water mark but has different provenance"
        )
    return run


def _new_supplemental_run(
    session: Session,
    *,
    target: BackfillTarget,
    snapshot: TerminalHistorySnapshot,
    source_commit: str,
    uuid_factory: Callable[[], uuid.UUID],
    clock: Callable[[], datetime],
) -> models.ImportRun:
    commit = source_commit.strip().lower()
    if not _COMMIT_RE.fullmatch(commit):
        raise TerminalHistoryBackfillError(
            "source commit must be a 40-64 character lowercase hexadecimal Git object ID"
        )
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise TerminalHistoryBackfillError("backfill clock must return a timezone-aware datetime")
    row = models.ImportRun(
        import_run_id=uuid_factory(),
        source_commit=commit,
        source_release=f"dish@{commit}",
        legacy_generation_id=target.legacy_generation_id,
        baseline_high_water_mark=snapshot.high_water_mark,
        source_bundle_sha256=snapshot.sha256,
        status="complete",
        started_at=now,
        completed_at=now,
        provenance={
            "resolved_by": "dish-pg-backfill-task",
            "import_kind": IMPORT_KIND,
            "task_id": str(target.task_id),
            "legacy_task_gid": snapshot.task_gid,
            "generation_id": str(target.generation_id),
            "primary_import_run_id": str(target.primary_import_run_id),
            "source_path": str(snapshot.path),
            "source_format": SNAPSHOT_FORMAT,
            "source_record_count": snapshot.record_count,
            "source_bundle_hash_method": "sha256-file-bytes",
            EXACT_REVOCATION_HISTORY_PROVENANCE_KEY: (
                EXACT_REVOCATION_RECONCILIATION_CONTRACT
            ),
            EXACT_REVOCATION_RECONCILED_OPERATIONS_KEY: sorted(
                str(item.operation_id) for item in snapshot.history.operations
            ),
            "candidate_attestation": SUPPLEMENTAL_HISTORY_ATTESTATION_CONTRACT,
        },
    )
    AuthorityRepository(session).add_import_run(row)
    return row


def apply_terminal_history_snapshot(
    session: Session,
    *,
    target: BackfillTarget,
    snapshot: TerminalHistorySnapshot,
    source_commit: str,
    uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> TerminalHistoryBackfillResult:
    """Atomically verify existing history, create/reuse provenance, and insert only missing rows."""
    generation = acquire_generation_release_gate(
        session, generation_id=target.generation_id
    )
    if generation is None or generation.status != "active":
        raise TerminalHistoryBackfillError(
            "terminal-history backfill target generation is no longer active"
        )
    current = resolve_backfill_target(session, task_gid=snapshot.task_gid)
    if not _same_target(target, current) or snapshot.task_id != current.task_id:
        raise TerminalHistoryBackfillError(
            "PostgreSQL task/import provenance changed after the legacy snapshot was captured"
        )
    service = CoreAuthorityService(session)
    try:
        plan = service.plan_operation_history_backfill(
            generation_id=target.generation_id,
            task_id=target.task_id,
            contract_binding_id=target.contract_binding_id,
            history=snapshot.history,
        )
    except CoreAuthorityError as exc:
        raise TerminalHistoryBackfillError(str(exc)) from exc

    supplemental = _find_supplemental_run(session, target=target, snapshot=snapshot)
    needs_revocation_reconciliation = not task_revocation_history_reconciled(
        session,
        generation_id=target.generation_id,
        task_id=target.task_id,
        primary_import_run_id=target.primary_import_run_id,
    )
    if needs_revocation_reconciliation:
        imported_operation_ids = legacy_imported_operation_ids(
            session,
            generation_id=target.generation_id,
            task_id=target.task_id,
            primary_import_run_id=target.primary_import_run_id,
        )
        snapshot_operation_ids = frozenset(
            item.operation_id for item in snapshot.history.operations
        )
        missing_from_snapshot = sorted(imported_operation_ids - snapshot_operation_ids, key=str)
        if missing_from_snapshot:
            raise TerminalHistoryBackfillError(
                "exact-revocation reconciliation snapshot does not cover existing imported "
                "operations: " + ",".join(str(value) for value in missing_from_snapshot)
            )
    if plan.inserted_total or needs_revocation_reconciliation:
        if supplemental is None:
            supplemental = _new_supplemental_run(
                session,
                target=target,
                snapshot=snapshot,
                source_commit=source_commit,
                uuid_factory=uuid_factory,
                clock=clock,
            )
        try:
            plan = service.backfill_imported_operation_history(
                generation_id=target.generation_id,
                task_id=target.task_id,
                import_run_id=supplemental.import_run_id,
                contract_binding_id=target.contract_binding_id,
                history=snapshot.history,
            )
        except CoreAuthorityError as exc:
            raise TerminalHistoryBackfillError(str(exc)) from exc

    return TerminalHistoryBackfillResult(
        task_gid=snapshot.task_gid,
        task_id=target.task_id,
        generation_id=target.generation_id,
        primary_import_run_id=target.primary_import_run_id,
        supplemental_import_run_id=(
            None if supplemental is None else supplemental.import_run_id
        ),
        source_sha256=snapshot.sha256,
        source_record_count=snapshot.record_count,
        baseline_high_water_mark=snapshot.high_water_mark,
        matched_operations=plan.matched_operations,
        inserted_operations=plan.inserted_operations,
        matched_verification_cycles=plan.matched_verification_cycles,
        inserted_verification_cycles=plan.inserted_verification_cycles,
        matched_leases=plan.matched_leases,
        inserted_leases=plan.inserted_leases,
        matched_revocations=plan.matched_revocations,
        inserted_revocations=plan.inserted_revocations,
    )


def require_postgresql_target(engine: Engine, *, expected_database_name: str) -> None:
    url = make_url(str(engine.url))
    if url.get_backend_name() != "postgresql":
        raise TerminalHistoryBackfillError("terminal-history backfill requires PostgreSQL")
    if url.database != expected_database_name:
        raise TerminalHistoryBackfillError(
            f"database target mismatch: expected {expected_database_name!r}, got {url.database!r}"
        )
    with engine.connect() as connection:
        versions = list(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    if versions != [ALEMBIC_HEAD]:
        raise TerminalHistoryBackfillError(
            f"Alembic head mismatch: expected [{ALEMBIC_HEAD!r}], got {versions!r}"
        )


def resolve_source_commit(explicit: str | None = None) -> str:
    supplied = (explicit or "").strip().lower()
    if supplied:
        if not _COMMIT_RE.fullmatch(supplied):
            raise TerminalHistoryBackfillError(
                "--source-commit must be a 40-64 character lowercase hexadecimal Git object ID"
            )
        return supplied
    completed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise TerminalHistoryBackfillError(
            "cannot resolve source commit from checkout; pass --source-commit explicitly"
        )
    value = completed.stdout.strip().lower()
    if not _COMMIT_RE.fullmatch(value):
        raise TerminalHistoryBackfillError("resolved checkout HEAD is not a supported Git object ID")
    return value


def run_backfill(
    *,
    database_url: str,
    expected_database_name: str,
    legacy_database: Path,
    task_gid: str,
    snapshot_output: Path,
    source_commit: str,
) -> TerminalHistoryBackfillResult:
    engine = create_database_engine(DatabaseSettings(url=database_url))
    try:
        require_postgresql_target(engine, expected_database_name=expected_database_name)
        factory = session_factory(engine)
        # Required ordering: establish that the target DishTask already exists before
        # consulting legacy terminal eligibility. This transaction is read-only by behavior.
        with session_scope(factory) as session:
            target = resolve_backfill_target(session, task_gid=task_gid)
        snapshot = capture_terminal_history_snapshot(
            legacy_database=legacy_database,
            task_gid=task_gid,
            task_id=target.task_id,
            output=snapshot_output,
        )
        with session_scope(factory) as session:
            return apply_terminal_history_snapshot(
                session,
                target=target,
                snapshot=snapshot,
                source_commit=source_commit,
            )
    finally:
        engine.dispose()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-backfill-task")
    parser.add_argument("task_gid", help="one explicit legacy/Asana task GID")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--expected-database-name", required=True)
    parser.add_argument("--legacy-database", required=True, type=Path)
    parser.add_argument("--snapshot-output", required=True, type=Path)
    parser.add_argument("--source-commit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_backfill(
            database_url=args.database_url,
            expected_database_name=args.expected_database_name,
            legacy_database=args.legacy_database,
            task_gid=args.task_gid,
            snapshot_output=args.snapshot_output,
            source_commit=resolve_source_commit(args.source_commit),
        )
        print(json.dumps(result.as_json(), sort_keys=True, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"error": str(exc), "type": type(exc).__name__}, sort_keys=True))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
