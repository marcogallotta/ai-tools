"""Governed pre-cutover PostgreSQL backup/restore rehearsal.

The source and restore databases are operator-provisioned, isolated PostgreSQL
instances. This command never creates, migrates, drops, or resets either one. It
creates one logical backup, verifies an independent retention copy, restores into
an already-empty target, and binds the existing material-authority fingerprints
into one durable report.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError

FORMAT = "dish-postgresql-backup-restore-rehearsal-v1"
FINGERPRINT_FORMAT = "dish-postgresql-database-fingerprint-v1"
COMPARISON_FORMAT = "dish-postgresql-database-fingerprint-comparison-v1"
LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
PROTECTED_ENV_NAMES = frozenset(
    {"DISH_PG_URL", "DISH_PG_TEST_URL", "DISH_TEST_POSTGRESQL_DSN"}
)
ROOT = Path(__file__).resolve().parents[1]
REPOSITORY_ROOT = ROOT.parent


class RehearsalError(RuntimeError):
    """The rehearsal cannot safely continue."""


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _prepare_output_dir(path: Path) -> Path:
    destination = path.expanduser().resolve(strict=False)
    if destination.exists():
        if not destination.is_dir():
            raise RehearsalError(f"output path is not a directory: {destination}")
        if any(destination.iterdir()):
            raise RehearsalError(f"output directory must be empty: {destination}")
    else:
        destination.mkdir(parents=True)
    return destination


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    destination = path.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temporary = tempfile.mkstemp(
        prefix=f".{destination.name}.", dir=destination.parent
    )
    temporary = Path(raw_temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(_canonical(value))
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


def _with_report_sha256(report: Mapping[str, Any]) -> dict[str, Any]:
    finalized = dict(report)
    finalized.pop("report_sha256", None)
    finalized["report_sha256"] = hashlib.sha256(_canonical(finalized)).hexdigest()
    return finalized


def _redact(value: str) -> str:
    return re.sub(
        r"(?P<scheme>postgres(?:ql)?(?:\+psycopg)?://)[^/@\s]+@",
        r"\g<scheme><redacted>@",
        value,
    )


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
        raise RehearsalError(
            f"command failed ({completed.returncode}): {rendered}\n"
            f"stdout:\n{_redact(completed.stdout)}\n"
            f"stderr:\n{_redact(completed.stderr)}"
        )
    return completed


def _required_env(env: Mapping[str, str], name: str) -> str:
    if name in PROTECTED_ENV_NAMES:
        raise RehearsalError(
            f"protected authority/test environment is forbidden for rehearsal: {name}"
        )
    value = env.get(name, "").strip()
    if not value:
        raise RehearsalError(f"{name} is required")
    for protected in PROTECTED_ENV_NAMES:
        protected_value = env.get(protected, "").strip()
        if protected_value and protected_value == value:
            raise RehearsalError(
                f"{name} aliases protected authority/test environment {protected}"
            )
    return value


def _optional_env(env: Mapping[str, str], name: str | None) -> str | None:
    if name is None:
        return None
    return _required_env(env, name)


def _normalized_query(url: URL) -> tuple[tuple[str, tuple[str, ...]], ...]:
    return tuple(
        sorted(
            (key, tuple(values))
            for key, values in url.normalized_query.items()
        )
    )


def _target_signature(url: URL) -> tuple[object, ...]:
    return (
        url.username,
        url.password,
        url.host,
        url.port,
        url.database,
        _normalized_query(url),
    )


def _canonical_target(value: str, *, label: str) -> tuple[URL, str]:
    try:
        url = make_url(value)
    except ArgumentError as exc:
        raise RehearsalError(f"{label} database URL is invalid") from exc
    if url.drivername != "postgresql+psycopg":
        raise RehearsalError(
            f"{label} database URL must use the postgresql+psycopg driver"
        )
    if not url.database:
        raise RehearsalError(f"{label} database URL must name a database")
    libpq = url.set(drivername="postgresql")
    return url, libpq.render_as_string(hide_password=False)


def _assert_libpq_binding(
    *,
    canonical: URL,
    asserted_value: str | None,
    asserted_env: str | None,
    label: str,
) -> bool:
    if asserted_value is None:
        return False
    try:
        asserted = make_url(asserted_value)
    except ArgumentError as exc:
        raise RehearsalError(
            f"{label} libpq assertion in {asserted_env} is invalid"
        ) from exc
    if asserted.drivername not in {"postgresql", "postgres"}:
        raise RehearsalError(
            f"{label} libpq assertion in {asserted_env} must use postgresql:// or postgres://"
        )
    asserted = asserted.set(drivername="postgresql")
    expected = canonical.set(drivername="postgresql")
    if _target_signature(asserted) != _target_signature(expected):
        raise RehearsalError(
            f"{label} libpq assertion in {asserted_env} does not match the canonical database target"
        )
    return True


def _full_commit(value: str) -> str:
    candidate = value.strip()
    if len(candidate) != 40 or any(
        character not in "0123456789abcdef" for character in candidate
    ):
        raise RehearsalError("source_commit must be a full lowercase 40-hex Git commit")
    return candidate


def _git_head(repo_root: Path, env: Mapping[str, str]) -> str:
    completed = _run(
        ["git", "-C", repo_root, "rev-parse", "HEAD"],
        env=env,
    )
    return _full_commit(completed.stdout.strip())


def _tool_version(binary: str, env: Mapping[str, str]) -> str:
    version = _run([binary, "--version"], env=env).stdout.strip()
    if not version:
        raise RehearsalError(f"{binary} --version returned no version identity")
    return version


def _query_identity(
    psql: str,
    libpq_url: str,
    env: Mapping[str, str],
) -> tuple[str, str, int]:
    sql = (
        "SELECT current_database(), current_setting('server_version_num'), "
        "(SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='public')"
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
        raise RehearsalError(f"database identity query returned {len(rows)} rows")
    parts = rows[0].split("|")
    if len(parts) != 3:
        raise RehearsalError("database identity query returned malformed evidence")
    try:
        table_count = int(parts[2])
    except ValueError as exc:
        raise RehearsalError(
            "database identity table count is not an integer"
        ) from exc
    return parts[0], parts[1], table_count


def _fingerprint(
    *,
    python: str,
    operations_tool: Path,
    env_name: str,
    expected_name: str,
    expected_schema_head: str,
    output: Path,
    env: Mapping[str, str],
) -> None:
    _run(
        [
            python,
            operations_tool,
            "database-fingerprint",
            "--database-url-env",
            env_name,
            "--expected-database-name",
            expected_name,
            "--expected-schema-head",
            expected_schema_head,
            "--output",
            output,
        ],
        env=env,
    )


def _compare(
    *,
    python: str,
    operations_tool: Path,
    source: Path,
    restored: Path,
    output: Path,
    env: Mapping[str, str],
) -> None:
    _run(
        [
            python,
            operations_tool,
            "compare-database-fingerprints",
            "--source",
            source,
            "--restored",
            restored,
            "--output",
            output,
        ],
        env=env,
    )


def _json_evidence(path: Path, *, require_ok: bool = True) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RehearsalError(f"invalid JSON evidence: {path}") from exc
    if not isinstance(value, dict):
        raise RehearsalError(f"JSON evidence root is not an object: {path}")
    if require_ok and value.get("ok") is not True:
        raise RehearsalError(f"evidence is not passing: {path}")
    return value




def _fingerprint_evidence(
    document: Mapping[str, Any],
    *,
    expected_database_name: str,
    expected_schema_head: str,
    require_material_rows: bool,
) -> int:
    if document.get("format") != FINGERPRINT_FORMAT:
        raise RehearsalError("database fingerprint evidence has the wrong format")
    if document.get("database_name") != expected_database_name:
        raise RehearsalError("database fingerprint evidence names the wrong database")
    if document.get("actual_schema_head") != expected_schema_head:
        raise RehearsalError("database fingerprint evidence has the wrong schema head")
    fingerprint_sha256 = document.get("database_fingerprint_sha256")
    if not isinstance(fingerprint_sha256, str) or not LOWER_SHA256.fullmatch(
        fingerprint_sha256
    ):
        raise RehearsalError("database fingerprint evidence is missing a valid SHA-256")
    tables = document.get("tables")
    if not isinstance(tables, list) or not tables:
        raise RehearsalError("database fingerprint evidence has no authority table inventory")
    total_rows = 0
    for item in tables:
        if not isinstance(item, Mapping):
            raise RehearsalError("database fingerprint table evidence is malformed")
        row_count = item.get("row_count")
        if not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0:
            raise RehearsalError("database fingerprint row count is malformed")
        total_rows += row_count
    if require_material_rows and total_rows <= 0:
        raise RehearsalError(
            "production-shaped source fingerprint contains no material authority rows"
        )
    return total_rows


def _comparison_evidence(document: Mapping[str, Any]) -> None:
    if document.get("format") != COMPARISON_FORMAT:
        raise RehearsalError("database fingerprint comparison has the wrong format")
    report_sha256 = document.get("report_sha256")
    if not isinstance(report_sha256, str) or not LOWER_SHA256.fullmatch(report_sha256):
        raise RehearsalError("database fingerprint comparison is missing a valid report SHA-256")


def _evidence_ref(path: Path) -> dict[str, Any]:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RehearsalError(f"required evidence file is missing: {path}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RehearsalError(f"required evidence is not a regular file: {path}")
    return {
        "path": str(path.resolve(strict=True)),
        "sha256": _sha256(path),
        "size_bytes": metadata.st_size,
    }


def _require_sha256(path: Path, expected: str, *, label: str) -> None:
    actual = _sha256(path)
    if actual != expected:
        raise RehearsalError(
            f"{label} SHA-256 mismatch: expected {expected}, got {actual}"
        )


def _copy_off_device(backup: Path, destination: Path) -> dict[str, Any]:
    target = destination.expanduser().resolve(strict=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    source_device = backup.stat().st_dev
    retention_device = target.parent.stat().st_dev
    if source_device == retention_device:
        raise RehearsalError(
            "retention destination is on the same filesystem device as the rehearsal backup"
        )
    if target.exists():
        raise RehearsalError(f"refusing to replace existing retention artifact: {target}")
    shutil.copy2(backup, target)
    metadata = target.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RehearsalError("retention copy is not a regular non-symlink file")
    os.chmod(target, 0o600)
    source_sha = _sha256(backup)
    retained_sha = _sha256(target)
    if source_sha != retained_sha:
        raise RehearsalError("off-device retention copy checksum does not match backup")
    return {
        "path": str(target),
        "sha256": retained_sha,
        "size_bytes": target.stat().st_size,
        "source_device": source_device,
        "retention_device": retention_device,
        "independent_device": True,
    }


def run(
    args: argparse.Namespace,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    env = dict(os.environ if environ is None else environ)
    output_dir = _prepare_output_dir(args.output_dir)
    report_path = output_dir / "rehearsal-report.json"
    report: dict[str, Any] = {
        "format": FORMAT,
        "status": "fail",
        "ok": False,
        "started_at": _utc_now(),
        "source_commit": args.source_commit,
        "expected_schema_head": args.expected_schema_head,
    }
    try:
        source_commit = _full_commit(args.source_commit)
        expected_schema_head = args.expected_schema_head.strip()
        if not expected_schema_head:
            raise RehearsalError("expected_schema_head must be nonblank")
        if args.expected_source_database == args.expected_restore_database:
            raise RehearsalError("source and restore database names must be distinct")

        source_fingerprint_url = _required_env(env, args.source_url_env)
        restore_fingerprint_url = _required_env(env, args.restore_url_env)
        source_target, source_libpq_url = _canonical_target(
            source_fingerprint_url, label="source"
        )
        restore_target, restore_libpq_url = _canonical_target(
            restore_fingerprint_url, label="restore"
        )
        source_asserted_libpq = _optional_env(env, args.source_libpq_url_env)
        restore_asserted_libpq = _optional_env(env, args.restore_libpq_url_env)
        source_assertion_verified = _assert_libpq_binding(
            canonical=source_target,
            asserted_value=source_asserted_libpq,
            asserted_env=args.source_libpq_url_env,
            label="source",
        )
        restore_assertion_verified = _assert_libpq_binding(
            canonical=restore_target,
            asserted_value=restore_asserted_libpq,
            asserted_env=args.restore_libpq_url_env,
            label="restore",
        )
        if _target_signature(source_target) == _target_signature(restore_target):
            raise RehearsalError("source and restore database targets must be independent")

        actual_commit = _git_head(args.repo_root.expanduser().resolve(strict=True), env)
        if actual_commit != source_commit:
            raise RehearsalError(
                f"source commit mismatch: checkout is {actual_commit}, expected {source_commit}"
            )

        operations_tool = args.operations_tool.expanduser().resolve(strict=True)
        tools = {
            "pg_dump": _tool_version(args.pg_dump, env),
            "pg_restore": _tool_version(args.pg_restore, env),
            "psql": _tool_version(args.psql, env),
        }
        source_name, source_server, source_tables = _query_identity(
            args.psql, source_libpq_url, env
        )
        restore_name, restore_server, restore_tables = _query_identity(
            args.psql, restore_libpq_url, env
        )
        if source_name != args.expected_source_database:
            raise RehearsalError(
                f"source database is {source_name!r}, expected {args.expected_source_database!r}"
            )
        if restore_name != args.expected_restore_database:
            raise RehearsalError(
                f"restore database is {restore_name!r}, expected {args.expected_restore_database!r}"
            )
        if restore_tables != 0:
            raise RehearsalError(
                f"restore target is not clean: public schema has {restore_tables} tables"
            )

        report["source_commit"] = source_commit
        report["checkout_commit"] = actual_commit
        report["tools"] = tools
        report["source"] = {
            "database_name": source_name,
            "server_version_num": source_server,
            "initial_public_table_count": source_tables,
            "database_url_env": args.source_url_env,
            "libpq_target": "derived_from_canonical_database_url",
            "libpq_assertion_env": args.source_libpq_url_env,
            "libpq_assertion_verified": source_assertion_verified,
        }
        report["restore_target"] = {
            "database_name": restore_name,
            "server_version_num": restore_server,
            "database_url_env": args.restore_url_env,
            "libpq_target": "derived_from_canonical_database_url",
            "libpq_assertion_env": args.restore_libpq_url_env,
            "libpq_assertion_verified": restore_assertion_verified,
            "initial_public_table_count": restore_tables,
        }

        source_before = output_dir / "source-before.json"
        source_after = output_dir / "source-after.json"
        source_stability = output_dir / "source-stability.json"
        restored = output_dir / "restored.json"
        restore_comparison = output_dir / "restore-comparison.json"

        _fingerprint(
            python=args.python,
            operations_tool=operations_tool,
            env_name=args.source_url_env,
            expected_name=source_name,
            expected_schema_head=expected_schema_head,
            output=source_before,
            env=env,
        )
        source_before_document = _json_evidence(source_before)
        source_material_rows = _fingerprint_evidence(
            source_before_document,
            expected_database_name=source_name,
            expected_schema_head=expected_schema_head,
            require_material_rows=True,
        )
        report["verification"] = {
            "source_before": _evidence_ref(source_before),
            "source_material_row_count": source_material_rows,
        }

        backup = output_dir / "postgresql-authority.dump"
        if backup.exists():
            raise RehearsalError(
                f"refusing to replace existing backup artifact: {backup}"
            )
        _run(
            [
                args.pg_dump,
                "--format=custom",
                "--no-owner",
                "--no-privileges",
                "--file",
                backup,
                source_libpq_url,
            ],
            env=env,
        )
        if not backup.is_file() or backup.stat().st_size == 0:
            raise RehearsalError(
                "pg_dump did not produce a non-empty backup artifact"
            )
        backup.chmod(0o600)
        report["backup"] = _evidence_ref(backup)

        _fingerprint(
            python=args.python,
            operations_tool=operations_tool,
            env_name=args.source_url_env,
            expected_name=source_name,
            expected_schema_head=expected_schema_head,
            output=source_after,
            env=env,
        )
        source_after_document = _json_evidence(source_after)
        _fingerprint_evidence(
            source_after_document,
            expected_database_name=source_name,
            expected_schema_head=expected_schema_head,
            require_material_rows=True,
        )
        _compare(
            python=args.python,
            operations_tool=operations_tool,
            source=source_before,
            restored=source_after,
            output=source_stability,
            env=env,
        )
        source_stability_document = _json_evidence(source_stability)
        _comparison_evidence(source_stability_document)
        report["verification"].update(
            {
                "source_after": _evidence_ref(source_after),
                "source_stability": _evidence_ref(source_stability),
            }
        )
        if (
            source_before_document.get("database_fingerprint_sha256")
            != source_after_document.get("database_fingerprint_sha256")
        ):
            raise RehearsalError(
                "source authority changed while backup evidence was being captured"
            )

        retention = _copy_off_device(backup, args.retention_destination)
        report["off_device_retention"] = retention
        _require_sha256(backup, report["backup"]["sha256"], label="backup artifact")

        _run(
            [
                args.pg_restore,
                "--exit-on-error",
                "--single-transaction",
                "--no-owner",
                "--no-privileges",
                "--dbname",
                restore_libpq_url,
                backup,
            ],
            env=env,
        )
        _fingerprint(
            python=args.python,
            operations_tool=operations_tool,
            env_name=args.restore_url_env,
            expected_name=restore_name,
            expected_schema_head=expected_schema_head,
            output=restored,
            env=env,
        )
        restored_document = _json_evidence(restored)
        _fingerprint_evidence(
            restored_document,
            expected_database_name=restore_name,
            expected_schema_head=expected_schema_head,
            require_material_rows=True,
        )
        _compare(
            python=args.python,
            operations_tool=operations_tool,
            source=source_after,
            restored=restored,
            output=restore_comparison,
            env=env,
        )
        comparison_document = _json_evidence(restore_comparison)
        _comparison_evidence(comparison_document)

        if (
            source_after_document.get("database_fingerprint_sha256")
            != restored_document.get("database_fingerprint_sha256")
        ):
            raise RehearsalError(
                "restored authority fingerprint does not match backup source"
            )

        _require_sha256(backup, report["backup"]["sha256"], label="backup artifact")
        _require_sha256(
            Path(retention["path"]),
            retention["sha256"],
            label="off-device retention artifact",
        )

        report["verification"].update(
            {
                "restored": _evidence_ref(restored),
                "restore_comparison": _evidence_ref(restore_comparison),
                "database_fingerprint_sha256": source_after_document.get(
                    "database_fingerprint_sha256"
                ),
                "restored_database_fingerprint_sha256": restored_document.get(
                    "database_fingerprint_sha256"
                ),
                "comparison_report_sha256": comparison_document.get("report_sha256"),
            }
        )
        report["status"] = "pass"
        report["ok"] = True
        report["completed_at"] = _utc_now()
    except (OSError, RehearsalError, TypeError, ValueError) as exc:
        report["completed_at"] = _utc_now()
        report["error_type"] = type(exc).__name__
        report["error"] = _redact(str(exc))
        finalized = _with_report_sha256(report)
        _atomic_json(report_path, finalized)
        raise

    finalized = _with_report_sha256(report)
    _atomic_json(report_path, finalized)
    return finalized


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(prog="dish-pg-backup-restore-rehearsal")
    command.add_argument("--source-commit", required=True)
    command.add_argument("--expected-schema-head", required=True)
    command.add_argument("--expected-source-database", required=True)
    command.add_argument("--expected-restore-database", required=True)
    command.add_argument("--output-dir", type=Path, required=True)
    command.add_argument("--retention-destination", type=Path, required=True)
    command.add_argument(
        "--source-url-env",
        default="DISH_PG_REHEARSAL_URL",
        help="canonical SQLAlchemy postgresql+psycopg source DSN environment",
    )
    command.add_argument(
        "--source-libpq-url-env",
        help=(
            "optional legacy/libpq DSN assertion environment; when supplied it must "
            "normalize to the canonical source target and is never used for pg_dump"
        ),
    )
    command.add_argument(
        "--restore-url-env",
        default="DISH_PG_RESTORE_URL",
        help="canonical SQLAlchemy postgresql+psycopg restore DSN environment",
    )
    command.add_argument(
        "--restore-libpq-url-env",
        help=(
            "optional legacy/libpq DSN assertion environment; when supplied it must "
            "normalize to the canonical restore target and is never used for pg_restore"
        ),
    )
    command.add_argument(
        "--operations-tool",
        type=Path,
        default=ROOT / "scripts" / "dish-pg-operations-evidence",
    )
    command.add_argument("--repo-root", type=Path, default=REPOSITORY_ROOT)
    command.add_argument("--python", default=sys.executable)
    command.add_argument("--pg-dump", default="pg_dump")
    command.add_argument("--pg-restore", default="pg_restore")
    command.add_argument("--psql", default="psql")
    return command


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        report = run(args)
    except (OSError, RehearsalError, TypeError, ValueError) as exc:
        print(
            json.dumps(
                {"ok": False, "error": _redact(str(exc))},
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "report": str((args.output_dir / "rehearsal-report.json").resolve()),
                "report_sha256": report["report_sha256"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0
