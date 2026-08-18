"""Unattended PostgreSQL backup, retention, and health checks.

Scheduled backups intentionally use the same custom pg_dump archive shape as the
existing governed clean restore/fingerprint rehearsal.  This module does not
restore, migrate, or mutate PostgreSQL.  It creates and verifies one local
archive, creates and verifies one byte-identical off-device copy, then applies
retention only after both copies are durable and valid.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

FORMAT = "dish-postgresql-scheduled-backup-v1"
HEALTH_FORMAT = "dish-postgresql-backup-health-v1"
ATTEMPT_FORMAT = "dish-postgresql-backup-attempt-v1"
LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
BACKUP_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parent
DEFAULT_LOCAL_DIR = Path("/home/marco/.local/state/dish/prod/postgresql-backups")
DEFAULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
DEFAULT_MAX_AGE_SECONDS = 2 * 60 * 60


class BackupError(RuntimeError):
    """The backup operation cannot safely continue."""


@dataclass(frozen=True)
class RetentionTier:
    """One GFS retention band: keep at most one backup per bucket within it.

    ``max_age_seconds`` is the band's outer edge, measured from the pruning
    instant; bands are implicitly ordered by ascending ``max_age_seconds`` and
    each covers the age range after the previous band's edge. ``bucket_seconds
    == 0`` means "keep every backup in this band" (no thinning).
    """

    max_age_seconds: int
    bucket_seconds: int


@dataclass(frozen=True)
class BackupConfig:
    database_url: str
    expected_database_name: str
    expected_schema_head: str
    local_dir: Path
    off_device_dir: Path
    retention_seconds: int
    max_age_seconds: int
    allow_same_device: bool = False
    retention_tiers: tuple[RetentionTier, ...] = ()
    repo_root: Path = REPOSITORY_ROOT
    pg_dump: str = "pg_dump"
    pg_restore: str = "pg_restore"
    psql: str = "psql"


def _effective_retention_tiers(config: BackupConfig) -> tuple[RetentionTier, ...]:
    """Tiers to prune with: configured tiers, or a single flat-cutoff tier.

    A single ``RetentionTier(config.retention_seconds, 0)`` reproduces the
    original flat-retention behaviour exactly (keep everything younger than
    ``retention_seconds``, no thinning), so deployments that never set
    ``DISH_PG_BACKUP_RETENTION_TIERS`` are unaffected by this feature.
    """
    if config.retention_tiers:
        return config.retention_tiers
    return (RetentionTier(config.retention_seconds, 0),)


def _parse_retention_tiers(value: str) -> tuple[RetentionTier, ...]:
    tiers: list[RetentionTier] = []
    previous_age = 0
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        age_text, sep, bucket_text = chunk.partition(":")
        if not sep:
            raise BackupError(
                "DISH_PG_BACKUP_RETENTION_TIERS entries must be <age_seconds>:<bucket_seconds>"
            )
        age = _positive_int(age_text.strip(), name="DISH_PG_BACKUP_RETENTION_TIERS age")
        bucket_text = bucket_text.strip()
        try:
            bucket = int(bucket_text) if bucket_text else 0
        except ValueError as exc:
            raise BackupError(
                "DISH_PG_BACKUP_RETENTION_TIERS bucket seconds must be an integer"
            ) from exc
        if bucket < 0:
            raise BackupError(
                "DISH_PG_BACKUP_RETENTION_TIERS bucket seconds must not be negative"
            )
        if age <= previous_age:
            raise BackupError(
                "DISH_PG_BACKUP_RETENTION_TIERS ages must be strictly increasing"
            )
        tiers.append(RetentionTier(max_age_seconds=age, bucket_seconds=bucket))
        previous_age = age
    if not tiers:
        raise BackupError("DISH_PG_BACKUP_RETENTION_TIERS must define at least one tier")
    return tuple(tiers)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact(value: str) -> str:
    return re.sub(
        r"(?P<scheme>postgres(?:ql)?(?:\+psycopg)?://)[^/@\s]+@",
        r"\g<scheme><redacted>@",
        value,
    )


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object, *, label: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BackupError(f"{label} must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise BackupError(f"{label} is invalid") from exc
    return parsed.astimezone(timezone.utc)


def _positive_int(value: str, *, name: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise BackupError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise BackupError(f"{name} must be a positive integer")
    return parsed


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise BackupError(f"{name} is required")
    return value


def config_from_environ(
    environ: Mapping[str, str] | None = None,
    *,
    repo_root: Path = REPOSITORY_ROOT,
    pg_dump: str = "pg_dump",
    pg_restore: str = "pg_restore",
    psql: str = "psql",
) -> BackupConfig:
    env = os.environ if environ is None else environ
    local_dir = Path(
        env.get("DISH_PG_BACKUP_LOCAL_DIR", str(DEFAULT_LOCAL_DIR)).strip()
        or str(DEFAULT_LOCAL_DIR)
    )
    off_device_dir = Path(_required_env(env, "DISH_PG_BACKUP_OFF_DEVICE_DIR"))
    allow_same_device = env.get("DISH_PG_BACKUP_ALLOW_SAME_DEVICE", "").strip() == "1"
    tiers_text = env.get("DISH_PG_BACKUP_RETENTION_TIERS", "").strip()
    retention_tiers = _parse_retention_tiers(tiers_text) if tiers_text else ()
    if retention_tiers:
        # Tiers are authoritative once configured: their outer edge is the
        # effective retention_seconds, so a stale/mismatched flat env var
        # cannot silently disagree with the active tiering.
        retention_seconds = retention_tiers[-1].max_age_seconds
    else:
        retention_seconds = _positive_int(
            env.get("DISH_PG_BACKUP_RETENTION_SECONDS", str(DEFAULT_RETENTION_SECONDS)),
            name="DISH_PG_BACKUP_RETENTION_SECONDS",
        )
    max_age_seconds = _positive_int(
        env.get("DISH_PG_BACKUP_MAX_AGE_SECONDS", str(DEFAULT_MAX_AGE_SECONDS)),
        name="DISH_PG_BACKUP_MAX_AGE_SECONDS",
    )
    return BackupConfig(
        database_url=_required_env(env, "DISH_PG_DATABASE_URL"),
        expected_database_name=_required_env(env, "DISH_PG_EXPECTED_DATABASE_NAME"),
        expected_schema_head=_required_env(env, "DISH_PG_EXPECTED_SCHEMA_HEAD"),
        local_dir=local_dir,
        off_device_dir=off_device_dir,
        retention_seconds=retention_seconds,
        max_age_seconds=max_age_seconds,
        allow_same_device=allow_same_device,
        retention_tiers=retention_tiers,
        repo_root=repo_root,
        pg_dump=pg_dump,
        pg_restore=pg_restore,
        psql=psql,
    )


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_bytes(path, _canonical(value) + b"\n")


def _atomic_text(path: Path, value: str) -> None:
    _atomic_bytes(path, value.encode("utf-8"))


def _fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _with_report_sha256(report: Mapping[str, Any]) -> dict[str, Any]:
    finalized = dict(report)
    finalized.pop("report_sha256", None)
    finalized["report_sha256"] = hashlib.sha256(_canonical(finalized)).hexdigest()
    return finalized


def _validate_report_sha256(document: Mapping[str, Any]) -> None:
    expected = document.get("report_sha256")
    if not isinstance(expected, str) or not LOWER_SHA256.fullmatch(expected):
        raise BackupError("backup report is missing a valid report SHA-256")
    unsigned = dict(document)
    unsigned.pop("report_sha256", None)
    actual = hashlib.sha256(_canonical(unsigned)).hexdigest()
    if actual != expected:
        raise BackupError("backup report SHA-256 mismatch")


def _run(
    command: Sequence[str | Path],
    *,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    argv = [str(item) for item in command]
    completed = subprocess.run(
        argv,
        env=dict(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        rendered = " ".join(_redact(item) for item in argv)
        raise BackupError(
            f"command failed ({completed.returncode}): {rendered}\n"
            f"stdout:\n{_redact(completed.stdout)}\n"
            f"stderr:\n{_redact(completed.stderr)}"
        )
    return completed


def _canonical_database_url(value: str) -> tuple[URL, str]:
    try:
        url = make_url(value)
    except ArgumentError as exc:
        raise BackupError("DISH_PG_DATABASE_URL is invalid") from exc
    if url.drivername != "postgresql+psycopg":
        raise BackupError("DISH_PG_DATABASE_URL must use postgresql+psycopg")
    if not url.database:
        raise BackupError("DISH_PG_DATABASE_URL must name a database")
    libpq = url.set(drivername="postgresql")
    return url, libpq.render_as_string(hide_password=False)


def _tool_version(binary: str, env: Mapping[str, str]) -> str:
    version = _run([binary, "--version"], env=env).stdout.strip()
    if not version:
        raise BackupError(f"{binary} --version returned no version identity")
    return version


def _git_head(repo_root: Path, env: Mapping[str, str]) -> str:
    completed = _run(["git", "-C", repo_root, "rev-parse", "HEAD"], env=env)
    value = completed.stdout.strip()
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise BackupError("repository HEAD is not a full lowercase Git commit")
    return value


def _query_source_identity(
    psql: str,
    libpq_url: str,
    env: Mapping[str, str],
) -> tuple[str, str, int, str]:
    sql = (
        "SELECT current_database(), current_setting('server_version_num'), "
        "(SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='public'), "
        "(SELECT count(*) FROM alembic_version), "
        "COALESCE((SELECT min(version_num) FROM alembic_version), '')"
    )
    completed = _run(
        [
            psql,
            "--no-psqlrc",
            "--set=ON_ERROR_STOP=1",
            "--tuples-only",
            "--no-align",
            "--field-separator=|",
            libpq_url,
            "--command",
            sql,
        ],
        env=env,
    )
    rows = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if len(rows) != 1:
        raise BackupError(f"database identity query returned {len(rows)} rows")
    parts = rows[0].split("|")
    if len(parts) != 5:
        raise BackupError("database identity query returned malformed evidence")
    try:
        table_count = int(parts[2])
        head_count = int(parts[3])
    except ValueError as exc:
        raise BackupError("database identity query returned malformed counts") from exc
    if head_count != 1:
        raise BackupError(f"database must have exactly one Alembic head, found {head_count}")
    return parts[0], parts[1], table_count, parts[4]


def _regular_file(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BackupError(f"{label} is missing: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise BackupError(f"{label} is not a regular non-symlink file: {path}")
    return metadata


def _directory(path: Path, *, label: str) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise BackupError(f"{label} is missing: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise BackupError(f"{label} is not a regular non-symlink directory: {path}")
    return metadata


def _prepare_local_root(config: BackupConfig) -> tuple[Path, os.stat_result]:
    local_root = config.local_dir.expanduser().resolve(strict=False)
    local_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(local_root, 0o700)
    return local_root, _directory(local_root, label="local backup root")


def _prepare_off_device_root(
    config: BackupConfig,
    *,
    local_metadata: os.stat_result,
) -> Path:
    requested_off_device = config.off_device_dir.expanduser()
    if not requested_off_device.exists():
        raise BackupError(
            "off-device backup root must already exist; refusing to create a missing mount path"
        )
    off_device_root = requested_off_device.resolve(strict=True)
    off_device_metadata = _directory(off_device_root, label="off-device backup root")
    if local_metadata.st_dev == off_device_metadata.st_dev and not config.allow_same_device:
        raise BackupError(
            "off-device backup root is on the same filesystem device as the local backup root"
        )
    return off_device_root


def _prepare_roots(config: BackupConfig) -> tuple[Path, Path]:
    local_root, local_metadata = _prepare_local_root(config)
    off_device_root = _prepare_off_device_root(config, local_metadata=local_metadata)
    return local_root, off_device_root


def _backup_id(now: datetime, token: str | None = None) -> str:
    suffix = token or uuid.uuid4().hex[:8]
    if not re.fullmatch(r"[0-9a-f]{8}", suffix):
        raise BackupError("backup ID token must be eight lowercase hexadecimal characters")
    return f"{now.astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{suffix}"


def _checksum_sidecar(digest: str, filename: str) -> str:
    if not LOWER_SHA256.fullmatch(digest):
        raise BackupError("invalid SHA-256 for checksum sidecar")
    return f"{digest}  {filename}\n"


def _verify_archive(pg_restore: str, archive: Path, env: Mapping[str, str]) -> None:
    completed = _run([pg_restore, "--list", archive], env=env)
    if not completed.stdout.strip():
        raise BackupError("pg_restore --list returned no archive inventory")


def _copy_off_device(
    source: Path,
    *,
    off_device_root: Path,
    backup_id: str,
    expected_sha256: str,
    allow_same_device: bool = False,
) -> tuple[Path, Path]:
    source_metadata = _regular_file(source, label="local backup artifact")
    off_root_metadata = _directory(off_device_root, label="off-device backup root")
    if source_metadata.st_dev == off_root_metadata.st_dev and not allow_same_device:
        raise BackupError("off-device destination is on the same filesystem device as the backup")

    target = off_device_root / f"{backup_id}.dump"
    checksum = off_device_root / f"{backup_id}.dump.sha256"
    if target.exists() or checksum.exists():
        raise BackupError(f"refusing to replace existing off-device backup: {backup_id}")

    temporary = off_device_root / f".{backup_id}.dump.tmp-{uuid.uuid4().hex}"
    try:
        with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
            os.fchmod(target_handle.fileno(), 0o600)
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        copied_sha256 = _sha256(temporary)
        if copied_sha256 != expected_sha256:
            raise BackupError(
                "off-device backup checksum does not match the verified local backup"
            )
        os.replace(temporary, target)
        _fsync_directory(off_device_root)
        _atomic_text(
            checksum,
            _checksum_sidecar(copied_sha256, target.name),
        )
        if _sha256(target) != expected_sha256:
            raise BackupError("off-device backup checksum changed after finalization")
        return target, checksum
    except Exception:
        temporary.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
        checksum.unlink(missing_ok=True)
        raise


def _load_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupError(f"invalid {label}: {path}") from exc
    if not isinstance(document, dict):
        raise BackupError(f"{label} root is not an object: {path}")
    return document


def _load_backup_report(path: Path) -> dict[str, Any]:
    document = _load_json(path, label="backup report")
    if document.get("format") != FORMAT or document.get("ok") is not True:
        raise BackupError(f"backup report is not a successful {FORMAT} report: {path}")
    _validate_report_sha256(document)
    return document


def _successful_reports(local_root: Path) -> list[tuple[datetime, Path, dict[str, Any]]]:
    reports: list[tuple[datetime, Path, dict[str, Any]]] = []
    for child in local_root.iterdir():
        if not BACKUP_ID.fullmatch(child.name):
            continue
        metadata = child.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise BackupError(f"backup entry is not a regular directory: {child}")
        report_path = child / "backup-report.json"
        if not report_path.exists():
            continue
        report = _load_backup_report(report_path)
        if report.get("backup_id") != child.name:
            raise BackupError(f"backup report identity does not match directory: {child}")
        completed_at = _parse_timestamp(report.get("completed_at"), label="completed_at")
        reports.append((completed_at, child, report))
    reports.sort(key=lambda item: item[0])
    return reports


def _safe_unlink(path: Path, *, label: str) -> None:
    if not path.exists():
        return
    _regular_file(path, label=label)
    path.unlink()


def _delete_backup(
    *,
    backup_dir: Path,
    off_device_root: Path,
    report: Mapping[str, Any],
) -> None:
    backup_id = backup_dir.name
    expected_off_device = off_device_root / f"{backup_id}.dump"
    expected_checksum = off_device_root / f"{backup_id}.dump.sha256"
    off_device = report.get("off_device")
    if not isinstance(off_device, Mapping):
        raise BackupError(f"backup report has no off-device evidence: {backup_dir}")
    if off_device.get("path") != str(expected_off_device):
        raise BackupError(f"backup report off-device path is unsafe: {backup_dir}")
    if off_device.get("checksum_path") != str(expected_checksum):
        raise BackupError(f"backup report checksum path is unsafe: {backup_dir}")
    _safe_unlink(expected_checksum, label="retained checksum")
    _safe_unlink(expected_off_device, label="retained backup")
    shutil.rmtree(backup_dir)


def _prune_retention(
    *,
    local_root: Path,
    off_device_root: Path,
    retention_tiers: Sequence[RetentionTier],
    now: datetime,
    current_backup_id: str,
) -> list[str]:
    """Apply tiered (GFS-style) retention.

    ``retention_tiers`` must be ordered by ascending ``max_age_seconds``. Any
    successful backup older than the last tier's edge is always deleted. Within
    each band, a non-zero ``bucket_seconds`` keeps only the newest backup per
    absolute-time bucket, discarding the rest of that band's duplicates; a
    ``bucket_seconds`` of 0 keeps every backup in the band untouched. This
    degenerates to the original flat-cutoff behaviour for a single tier with
    ``bucket_seconds == 0``.
    """
    reports = _successful_reports(local_root)
    reports_by_id = {backup_dir.name: (backup_dir, report) for _, backup_dir, report in reports}
    outer_cutoff_seconds = retention_tiers[-1].max_age_seconds

    to_delete: set[str] = set()
    for completed_at, backup_dir, _report in reports:
        if backup_dir.name == current_backup_id:
            continue
        age_seconds = (now - completed_at).total_seconds()
        if age_seconds > outer_cutoff_seconds:
            to_delete.add(backup_dir.name)

    lower_bound = -1.0
    for tier in retention_tiers:
        band = [
            (completed_at, backup_dir)
            for completed_at, backup_dir, _report in reports
            if backup_dir.name != current_backup_id
            and backup_dir.name not in to_delete
            and lower_bound < (now - completed_at).total_seconds() <= tier.max_age_seconds
        ]
        lower_bound = tier.max_age_seconds
        if tier.bucket_seconds <= 0:
            continue
        buckets: dict[int, tuple[datetime, Path]] = {}
        for completed_at, backup_dir in band:
            bucket_index = int(completed_at.timestamp() // tier.bucket_seconds)
            existing = buckets.get(bucket_index)
            if existing is None:
                buckets[bucket_index] = (completed_at, backup_dir)
            elif completed_at > existing[0]:
                to_delete.add(existing[1].name)
                buckets[bucket_index] = (completed_at, backup_dir)
            else:
                to_delete.add(backup_dir.name)

    deleted = sorted(to_delete)
    for backup_id in deleted:
        backup_dir, report = reports_by_id[backup_id]
        _delete_backup(backup_dir=backup_dir, off_device_root=off_device_root, report=report)
    return deleted


def _attempt_path(local_root: Path) -> Path:
    return local_root / "last-attempt.json"


def _write_attempt(
    local_root: Path,
    *,
    backup_id: str | None,
    started_at: datetime,
    completed_at: datetime,
    ok: bool,
    error: str | None = None,
    retention_deleted: Sequence[str] = (),
) -> None:
    document: dict[str, Any] = {
        "format": ATTEMPT_FORMAT,
        "ok": ok,
        "backup_id": backup_id,
        "started_at": _timestamp(started_at),
        "completed_at": _timestamp(completed_at),
        "retention_deleted_backup_ids": list(retention_deleted),
    }
    if error is not None:
        document["error"] = _redact(error)
    _atomic_json(_attempt_path(local_root), _with_report_sha256(document))


def run_backup(
    config: BackupConfig,
    *,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
    token: str | None = None,
) -> dict[str, Any]:
    env = dict(os.environ if environ is None else environ)
    started_at = (now or _utc_now()).astimezone(timezone.utc)
    backup_id = _backup_id(started_at, token)
    local_root, local_metadata = _prepare_local_root(config)
    try:
        off_device_root = _prepare_off_device_root(
            config, local_metadata=local_metadata
        )
    except BackupError as exc:
        completed_at = _utc_now() if now is None else started_at
        try:
            _write_attempt(
                local_root,
                backup_id=backup_id,
                started_at=started_at,
                completed_at=completed_at,
                ok=False,
                error=str(exc),
            )
        except OSError:
            pass
        raise
    lock_path = local_root / ".backup.lock"
    candidate_dir = local_root / f".incomplete-{backup_id}"
    final_dir = local_root / backup_id
    off_device_path = off_device_root / f"{backup_id}.dump"
    off_device_checksum = off_device_root / f"{backup_id}.dump.sha256"
    finalized = False

    lock_path.touch(mode=0o600, exist_ok=True)
    os.chmod(lock_path, 0o600)
    with lock_path.open("r+") as lock_handle:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BackupError("another PostgreSQL backup is already running") from exc

        try:
            if candidate_dir.exists() or final_dir.exists():
                raise BackupError(f"backup identity already exists: {backup_id}")
            candidate_dir.mkdir(mode=0o700)

            canonical_url, libpq_url = _canonical_database_url(config.database_url)
            if canonical_url.database != config.expected_database_name:
                raise BackupError(
                    "DISH_PG_DATABASE_URL database does not match DISH_PG_EXPECTED_DATABASE_NAME"
                )

            database_name, server_version, table_count, schema_head = _query_source_identity(
                config.psql, libpq_url, env
            )
            if database_name != config.expected_database_name:
                raise BackupError(
                    f"connected database is {database_name!r}, expected {config.expected_database_name!r}"
                )
            if table_count <= 0:
                raise BackupError("source PostgreSQL public schema has no tables")
            if schema_head != config.expected_schema_head:
                raise BackupError(
                    f"database schema head is {schema_head!r}, expected {config.expected_schema_head!r}"
                )

            source_commit = _git_head(config.repo_root.expanduser().resolve(strict=True), env)
            tools = {
                "pg_dump": _tool_version(config.pg_dump, env),
                "pg_restore": _tool_version(config.pg_restore, env),
                "psql": _tool_version(config.psql, env),
            }

            backup = candidate_dir / "postgresql-authority.dump"
            _run(
                [
                    config.pg_dump,
                    "--format=custom",
                    "--no-owner",
                    "--no-privileges",
                    "--file",
                    backup,
                    libpq_url,
                ],
                env=env,
            )
            metadata = _regular_file(backup, label="pg_dump backup artifact")
            if metadata.st_size <= 0:
                raise BackupError("pg_dump produced an empty backup artifact")
            os.chmod(backup, 0o600)
            _verify_archive(config.pg_restore, backup, env)
            backup_sha256 = _sha256(backup)
            checksum_path = candidate_dir / "postgresql-authority.dump.sha256"
            _atomic_text(
                checksum_path,
                _checksum_sidecar(backup_sha256, backup.name),
            )
            if _sha256(backup) != backup_sha256:
                raise BackupError("local backup checksum changed after verification")

            retained_backup, retained_checksum = _copy_off_device(
                backup,
                off_device_root=off_device_root,
                backup_id=backup_id,
                expected_sha256=backup_sha256,
                allow_same_device=config.allow_same_device,
            )
            _verify_archive(config.pg_restore, retained_backup, env)
            retained_sha256 = _sha256(retained_backup)
            if retained_sha256 != backup_sha256:
                raise BackupError("off-device backup is not byte-identical to local backup")

            completed_at = _utc_now() if now is None else started_at
            report = _with_report_sha256(
                {
                    "format": FORMAT,
                    "status": "pass",
                    "ok": True,
                    "backup_id": backup_id,
                    "started_at": _timestamp(started_at),
                    "completed_at": _timestamp(completed_at),
                    "source_commit": source_commit,
                    "database": {
                        "name": database_name,
                        "server_version_num": server_version,
                        "schema_head": schema_head,
                        "public_table_count": table_count,
                        "database_url_env": "DISH_PG_DATABASE_URL",
                    },
                    "tools": tools,
                    "backup": {
                        "path": str(final_dir / backup.name),
                        "checksum_path": str(final_dir / checksum_path.name),
                        "sha256": backup_sha256,
                        "size_bytes": metadata.st_size,
                        "archive_format": "pg_dump-custom",
                    },
                    "off_device": {
                        "path": str(retained_backup),
                        "checksum_path": str(retained_checksum),
                        "sha256": retained_sha256,
                        "size_bytes": _regular_file(
                            retained_backup, label="off-device backup artifact"
                        ).st_size,
                        "independent_device": True,
                    },
                    "restore_compatibility": {
                        "pg_dump_flags": ["--format=custom", "--no-owner", "--no-privileges"],
                        "clean_restore_flags": [
                            "--exit-on-error",
                            "--single-transaction",
                            "--no-owner",
                            "--no-privileges",
                        ],
                        "fingerprint_procedure": "dish-pg-operations-evidence database-fingerprint + compare-database-fingerprints",
                    },
                    "retention_seconds": config.retention_seconds,
                    "health_max_age_seconds": config.max_age_seconds,
                }
            )
            _atomic_json(candidate_dir / "backup-report.json", report)
            os.replace(candidate_dir, final_dir)
            _fsync_directory(local_root)
            finalized = True

            if _sha256(final_dir / backup.name) != backup_sha256:
                raise BackupError("local backup checksum changed after finalization")
            if _sha256(retained_backup) != backup_sha256:
                raise BackupError("off-device backup checksum changed after finalization")

            retention_deleted = _prune_retention(
                local_root=local_root,
                off_device_root=off_device_root,
                retention_tiers=_effective_retention_tiers(config),
                now=completed_at,
                current_backup_id=backup_id,
            )
            _write_attempt(
                local_root,
                backup_id=backup_id,
                started_at=started_at,
                completed_at=completed_at,
                ok=True,
                retention_deleted=retention_deleted,
            )
            return report
        except Exception as exc:
            completed_at = _utc_now() if now is None else started_at
            if not finalized:
                shutil.rmtree(candidate_dir, ignore_errors=True)
                off_device_path.unlink(missing_ok=True)
                off_device_checksum.unlink(missing_ok=True)
            try:
                _write_attempt(
                    local_root,
                    backup_id=backup_id,
                    started_at=started_at,
                    completed_at=completed_at,
                    ok=False,
                    error=str(exc),
                )
            except OSError:
                pass
            if isinstance(exc, BackupError):
                raise
            if isinstance(exc, (OSError, ValueError, TypeError)):
                raise BackupError(_redact(str(exc))) from exc
            raise


def _verify_latest_report(
    *,
    local_root: Path,
    off_device_root: Path,
    report_dir: Path,
    report: Mapping[str, Any],
    allow_same_device: bool = False,
) -> dict[str, Any]:
    backup_id = report_dir.name
    expected_local = report_dir / "postgresql-authority.dump"
    expected_local_checksum = report_dir / "postgresql-authority.dump.sha256"
    expected_off = off_device_root / f"{backup_id}.dump"
    expected_off_checksum = off_device_root / f"{backup_id}.dump.sha256"
    backup = report.get("backup")
    off_device = report.get("off_device")
    if not isinstance(backup, Mapping) or not isinstance(off_device, Mapping):
        raise BackupError("latest backup report is missing artifact evidence")
    if backup.get("path") != str(expected_local):
        raise BackupError("latest backup report local path is unsafe")
    if backup.get("checksum_path") != str(expected_local_checksum):
        raise BackupError("latest backup report local checksum path is unsafe")
    if off_device.get("path") != str(expected_off):
        raise BackupError("latest backup report off-device path is unsafe")
    if off_device.get("checksum_path") != str(expected_off_checksum):
        raise BackupError("latest backup report off-device checksum path is unsafe")
    expected_sha256 = backup.get("sha256")
    if not isinstance(expected_sha256, str) or not LOWER_SHA256.fullmatch(expected_sha256):
        raise BackupError("latest backup report has no valid backup SHA-256")
    if off_device.get("sha256") != expected_sha256:
        raise BackupError("latest backup report local/off-device SHA-256 values differ")

    local_metadata = _regular_file(expected_local, label="latest local backup")
    off_metadata = _regular_file(expected_off, label="latest off-device backup")
    _regular_file(expected_local_checksum, label="latest local checksum")
    _regular_file(expected_off_checksum, label="latest off-device checksum")
    same_device = local_metadata.st_dev == off_metadata.st_dev
    if same_device and not allow_same_device:
        raise BackupError("latest off-device backup is no longer on an independent device")
    local_sha256 = _sha256(expected_local)
    off_sha256 = _sha256(expected_off)
    if local_sha256 != expected_sha256:
        raise BackupError("latest local backup SHA-256 mismatch")
    if off_sha256 != expected_sha256:
        raise BackupError("latest off-device backup SHA-256 mismatch")
    expected_sidecar = _checksum_sidecar(expected_sha256, expected_local.name)
    if expected_local_checksum.read_text(encoding="utf-8") != expected_sidecar:
        raise BackupError("latest local checksum sidecar mismatch")
    expected_off_sidecar = _checksum_sidecar(expected_sha256, expected_off.name)
    if expected_off_checksum.read_text(encoding="utf-8") != expected_off_sidecar:
        raise BackupError("latest off-device checksum sidecar mismatch")
    return {
        "backup_id": backup_id,
        "completed_at": report.get("completed_at"),
        "database_name": report.get("database", {}).get("name")
        if isinstance(report.get("database"), Mapping)
        else None,
        "schema_head": report.get("database", {}).get("schema_head")
        if isinstance(report.get("database"), Mapping)
        else None,
        "sha256": expected_sha256,
        "local_path": str(expected_local),
        "off_device_path": str(expected_off),
        "off_device_independent": not same_device,
    }


def health(
    config: BackupConfig,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    checked_at = (now or _utc_now()).astimezone(timezone.utc)
    result: dict[str, Any] = {
        "format": HEALTH_FORMAT,
        "ok": False,
        "checked_at": _timestamp(checked_at),
        "retention_seconds": config.retention_seconds,
        "max_age_seconds": config.max_age_seconds,
        "off_device_destination": str(config.off_device_dir.expanduser()),
    }
    try:
        local_root, off_device_root = _prepare_roots(config)
        result["off_device_destination"] = str(off_device_root)
        reports = _successful_reports(local_root)
        if not reports:
            raise BackupError("no successful PostgreSQL backup is available")
        completed_at, report_dir, report = reports[-1]
        latest = _verify_latest_report(
            local_root=local_root,
            off_device_root=off_device_root,
            report_dir=report_dir,
            report=report,
            allow_same_device=config.allow_same_device,
        )
        age_seconds = max(0.0, (checked_at - completed_at).total_seconds())
        fresh = age_seconds <= config.max_age_seconds
        latest["age_seconds"] = age_seconds
        latest["fresh"] = fresh
        result["latest_success"] = latest

        attempt_path = _attempt_path(local_root)
        latest_attempt_ok = True
        if attempt_path.exists():
            attempt = _load_json(attempt_path, label="last backup attempt")
            if attempt.get("format") != ATTEMPT_FORMAT:
                raise BackupError("last backup attempt has the wrong format")
            _validate_report_sha256(attempt)
            latest_attempt_ok = attempt.get("ok") is True
            result["latest_attempt"] = attempt
        result["fresh"] = fresh
        result["latest_attempt_ok"] = latest_attempt_ok
        result["ok"] = fresh and latest_attempt_ok
        if not fresh:
            result["error"] = (
                f"latest successful backup is stale: {age_seconds:.0f}s > "
                f"{config.max_age_seconds}s"
            )
        elif not latest_attempt_ok:
            result["error"] = "latest backup attempt failed after the last successful artifact"
        return result
    except BackupError as exc:
        result["error"] = _redact(str(exc))
        return result


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="dish-pg-scheduled-backup")
    command.add_argument(
        "--repo-root",
        type=Path,
        default=REPOSITORY_ROOT,
        help="repository root used only for source commit provenance",
    )
    command.add_argument("--pg-dump", default="pg_dump")
    command.add_argument("--pg-restore", default="pg_restore")
    command.add_argument("--psql", default="psql")
    subcommands = command.add_subparsers(dest="command", required=True)
    subcommands.add_parser("run", help="create, verify, copy, and retain one backup")
    subcommands.add_parser("health", aliases=["status"], help="verify latest backup health")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = config_from_environ(
            repo_root=args.repo_root,
            pg_dump=args.pg_dump,
            pg_restore=args.pg_restore,
            psql=args.psql,
        )
        if args.command == "run":
            report = run_backup(config)
            output = {
                "ok": True,
                "backup_id": report["backup_id"],
                "completed_at": report["completed_at"],
                "sha256": report["backup"]["sha256"],
                "off_device_path": report["off_device"]["path"],
            }
            print(json.dumps(output, sort_keys=True, separators=(",", ":")))
            return 0
        result = health(config)
        print(json.dumps(result, sort_keys=True, separators=(",", ":")))
        return 0 if result.get("ok") is True else 1
    except (BackupError, OSError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": _redact(str(exc))},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
