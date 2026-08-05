"""Deterministic, read-only evidence helpers for PostgreSQL operations.

The helpers in this module never create, migrate, restore, or cut over a database.
They fingerprint an already selected PostgreSQL snapshot, compare two fingerprints,
and validate a locally captured legacy-writer inventory against its evidence files.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import stat
import tempfile
import uuid
from collections.abc import Mapping
from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, Table, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError

from .database import DatabaseSettings, create_database_engine
from .release import ALEMBIC_HEAD

FINGERPRINT_FORMAT = "dish-postgresql-database-fingerprint-v1"
COMPARISON_FORMAT = "dish-postgresql-database-fingerprint-comparison-v1"
WRITER_INVENTORY_FORMAT = "dish-legacy-writer-inventory-v1"
WRITER_INVENTORY_REPORT_FORMAT = "dish-legacy-writer-inventory-report-v1"
REQUIRED_WRITER_KINDS = ("process", "endpoint", "credential", "scheduler")
ALLOWED_WRITER_STATES: Mapping[str, frozenset[str]] = {
    "process": frozenset({"fenced", "stopped"}),
    "endpoint": frozenset({"blocked", "read_only", "removed"}),
    "credential": frozenset({"disabled", "read_only", "revoked"}),
    "scheduler": frozenset({"disabled", "removed"}),
}
_NON_AUTHORITY_TABLES = frozenset({"alembic_version"})


class OperationsEvidenceError(ValueError):
    """Evidence input or selected environment is unsafe or inconsistent."""


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_atomic(path: Path, value: object) -> None:
    destination = path.expanduser().resolve(strict=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temporary = tempfile.mkstemp(prefix=f".{destination.name}.", dir=destination.parent)
    temporary = Path(raw_temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(_canonical_json_bytes(value))
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


def _reject_duplicate_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise OperationsEvidenceError(f"duplicate JSON object key: {key}")
        value[key] = item
    return value


def _load_json_object(path: Path) -> tuple[dict[str, Any], bytes]:
    source = path.expanduser().resolve(strict=True)
    raw = source.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_pairs)
    except UnicodeDecodeError as exc:
        raise OperationsEvidenceError(f"JSON file is not UTF-8: {source}") from exc
    except json.JSONDecodeError as exc:
        raise OperationsEvidenceError(f"invalid JSON in {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise OperationsEvidenceError(f"JSON root must be an object: {source}")
    return value, raw


def _full_commit(value: object, *, field: str) -> str:
    candidate = str(value or "").strip()
    if len(candidate) not in {40, 64} or any(character not in "0123456789abcdef" for character in candidate):
        raise OperationsEvidenceError(f"{field} must be a full lowercase 40- or 64-hex commit identity")
    return candidate


def _uuid(value: object, *, field: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError) as exc:
        raise OperationsEvidenceError(f"{field} must be a UUID") from exc


def _nonblank(value: object, *, field: str) -> str:
    candidate = str(value or "").strip()
    if not candidate:
        raise OperationsEvidenceError(f"{field} must be nonblank")
    return candidate


def _lower_sha256(value: object, *, field: str) -> str:
    candidate = str(value or "").strip()
    if len(candidate) != 64 or any(character not in "0123456789abcdef" for character in candidate):
        raise OperationsEvidenceError(f"{field} must be a lowercase hexadecimal SHA-256")
    return candidate


def _owner_only_regular_file(path: object, *, field: str) -> Path:
    source = Path(_nonblank(path, field=field)).expanduser()
    if not source.is_absolute():
        raise OperationsEvidenceError(f"{field} must be an absolute path")
    try:
        metadata = source.lstat()
    except OSError as exc:
        raise OperationsEvidenceError(f"{field} is not readable: {source}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise OperationsEvidenceError(f"{field} must be a regular non-symlink file: {source}")
    if stat.S_IMODE(metadata.st_mode) & 0o077:
        raise OperationsEvidenceError(f"{field} must be owner-only (mode 0600 or stricter): {source}")
    return source.resolve(strict=True)


def _normalize_database_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise OperationsEvidenceError("database fingerprint cannot encode non-finite floats")
        return {"type": "float", "value": repr(value)}
    if isinstance(value, Decimal):
        return {"type": "decimal", "value": str(value)}
    if isinstance(value, uuid.UUID):
        return {"type": "uuid", "value": str(value)}
    if isinstance(value, datetime):
        rendered = value
        if rendered.tzinfo is not None and rendered.utcoffset() is not None:
            rendered = rendered.astimezone(timezone.utc)
            return {"type": "datetime", "value": rendered.isoformat().replace("+00:00", "Z")}
        return {"type": "datetime-naive", "value": rendered.isoformat()}
    if isinstance(value, date):
        return {"type": "date", "value": value.isoformat()}
    if isinstance(value, time):
        return {"type": "time", "value": value.isoformat()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "bytes", "value": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Mapping):
        return {
            "type": "mapping",
            "value": [
                [str(key), _normalize_database_value(item)]
                for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            ],
        }
    if isinstance(value, (list, tuple)):
        return {"type": "sequence", "value": [_normalize_database_value(item) for item in value]}
    return {"type": f"python:{type(value).__module__}.{type(value).__qualname__}", "value": str(value)}


def fingerprint_database(
    *,
    database_url: str,
    database_url_env: str,
    expected_database_name: str | None,
    expected_schema_head: str,
) -> dict[str, Any]:
    if not database_url.strip():
        raise OperationsEvidenceError(f"{database_url_env} is required")
    engine = create_database_engine(DatabaseSettings(url=database_url))
    try:
        if engine.dialect.name != "postgresql":
            raise OperationsEvidenceError("database fingerprint requires native PostgreSQL")
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            transaction = connection.begin()
            try:
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                database_name, server_version_num = connection.execute(
                    text("SELECT current_database(), current_setting('server_version_num')")
                ).one()
                if expected_database_name and database_name != expected_database_name:
                    raise OperationsEvidenceError(
                        f"selected database is {database_name!r}, expected {expected_database_name!r}"
                    )
                heads = tuple(
                    str(row[0])
                    for row in connection.execute(text("SELECT version_num FROM alembic_version"))
                )
                if len(heads) != 1:
                    raise OperationsEvidenceError(
                        f"alembic_version must contain exactly one head, found {len(heads)}"
                    )
                actual_schema_head = heads[0]
                inspector = inspect(connection)
                table_reports: list[dict[str, Any]] = []
                for table_name in sorted(inspector.get_table_names(schema="public")):
                    if table_name in _NON_AUTHORITY_TABLES:
                        continue
                    table = Table(table_name, MetaData(), schema="public", autoload_with=connection)
                    primary_key = tuple(column.name for column in table.primary_key.columns)
                    if not primary_key:
                        raise OperationsEvidenceError(
                            f"public.{table_name} has no primary key; deterministic fingerprint is blocked"
                        )
                    columns = tuple(column.name for column in table.columns)
                    statement = select(table).order_by(*(table.c[name] for name in primary_key))
                    digest = hashlib.sha256()
                    row_count = 0
                    for row in connection.execution_options(stream_results=True).execute(statement):
                        payload = {
                            "columns": columns,
                            "values": [_normalize_database_value(value) for value in row],
                        }
                        digest.update(_canonical_json_bytes(payload))
                        digest.update(b"\n")
                        row_count += 1
                    table_reports.append(
                        {
                            "table": f"public.{table_name}",
                            "columns": list(columns),
                            "primary_key": list(primary_key),
                            "row_count": row_count,
                            "rows_sha256": digest.hexdigest(),
                        }
                    )
                identity = {
                    "schema_head": actual_schema_head,
                    "tables": table_reports,
                }
                report = {
                    "format": FINGERPRINT_FORMAT,
                    "status": "pass" if actual_schema_head == expected_schema_head else "fail",
                    "database_url_env": database_url_env,
                    "database_name": database_name,
                    "server_version_num": str(server_version_num),
                    "expected_schema_head": expected_schema_head,
                    "actual_schema_head": actual_schema_head,
                    "tables": table_reports,
                    "database_fingerprint_sha256": _sha256_bytes(_canonical_json_bytes(identity)),
                }
                report["ok"] = report["status"] == "pass"
                report["report_sha256"] = _sha256_bytes(_canonical_json_bytes(report))
                return report
            finally:
                transaction.rollback()
    finally:
        engine.dispose()


def compare_fingerprint_reports(
    *, source_path: Path, restored_path: Path
) -> dict[str, Any]:
    source, source_raw = _load_json_object(source_path)
    restored, restored_raw = _load_json_object(restored_path)
    for label, value in (("source", source), ("restored", restored)):
        if value.get("format") != FINGERPRINT_FORMAT:
            raise OperationsEvidenceError(f"{label} report has the wrong format")
        if value.get("ok") is not True:
            raise OperationsEvidenceError(f"{label} fingerprint is not passing evidence")
        _lower_sha256(value.get("database_fingerprint_sha256"), field=f"{label}.database_fingerprint_sha256")
    source_tables = {item["table"]: item for item in source.get("tables", [])}
    restored_tables = {item["table"]: item for item in restored.get("tables", [])}
    differences: list[dict[str, Any]] = []
    for table in sorted(set(source_tables) | set(restored_tables)):
        left = source_tables.get(table)
        right = restored_tables.get(table)
        if left != right:
            differences.append({"table": table, "source": left, "restored": right})
    matched = (
        source.get("actual_schema_head") == restored.get("actual_schema_head")
        and source.get("database_fingerprint_sha256")
        == restored.get("database_fingerprint_sha256")
        and not differences
    )
    report = {
        "format": COMPARISON_FORMAT,
        "status": "pass" if matched else "fail",
        "ok": matched,
        "source_report_path": str(source_path.expanduser().resolve(strict=True)),
        "source_report_sha256": _sha256_bytes(source_raw),
        "restored_report_path": str(restored_path.expanduser().resolve(strict=True)),
        "restored_report_sha256": _sha256_bytes(restored_raw),
        "schema_head": source.get("actual_schema_head"),
        "source_database_fingerprint_sha256": source.get("database_fingerprint_sha256"),
        "restored_database_fingerprint_sha256": restored.get("database_fingerprint_sha256"),
        "differences": differences,
    }
    report["report_sha256"] = _sha256_bytes(_canonical_json_bytes(report))
    return report


def validate_legacy_writer_inventory(
    *,
    inventory_path: Path,
    expected_candidate_id: str | None = None,
    expected_cutover_run_id: str | None = None,
    expected_source_commit: str | None = None,
) -> dict[str, Any]:
    inventory_file = _owner_only_regular_file(inventory_path, field="inventory file")
    inventory, inventory_raw = _load_json_object(inventory_file)
    if inventory.get("format") != WRITER_INVENTORY_FORMAT:
        raise OperationsEvidenceError(
            f"inventory format must be {WRITER_INVENTORY_FORMAT}"
        )
    candidate_id = _uuid(inventory.get("candidate_id"), field="candidate_id")
    cutover_run_id = _uuid(inventory.get("cutover_run_id"), field="cutover_run_id")
    source_commit = _full_commit(inventory.get("source_commit"), field="source_commit")
    if expected_candidate_id is not None and candidate_id != _uuid(
        expected_candidate_id, field="expected_candidate_id"
    ):
        raise OperationsEvidenceError("inventory candidate_id does not match the expected candidate")
    if expected_cutover_run_id is not None and cutover_run_id != _uuid(
        expected_cutover_run_id, field="expected_cutover_run_id"
    ):
        raise OperationsEvidenceError(
            "inventory cutover_run_id does not match the expected cutover run"
        )
    if expected_source_commit is not None and source_commit != _full_commit(
        expected_source_commit, field="expected_source_commit"
    ):
        raise OperationsEvidenceError(
            "inventory source_commit does not match the expected source commit"
        )
    categories = inventory.get("categories")
    if not isinstance(categories, list):
        raise OperationsEvidenceError("categories must be an array")
    by_kind: dict[str, dict[str, Any]] = {}
    observations: list[dict[str, Any]] = []
    writer_ids: set[str] = set()
    for index, category in enumerate(categories):
        if not isinstance(category, dict):
            raise OperationsEvidenceError(f"categories[{index}] must be an object")
        kind = _nonblank(category.get("kind"), field=f"categories[{index}].kind")
        if kind not in REQUIRED_WRITER_KINDS:
            raise OperationsEvidenceError(f"unsupported writer category: {kind}")
        if kind in by_kind:
            raise OperationsEvidenceError(f"duplicate writer category: {kind}")
        applicable = category.get("applicable")
        if not isinstance(applicable, bool):
            raise OperationsEvidenceError(f"categories[{index}].applicable must be boolean")
        discovery_path = _owner_only_regular_file(
            category.get("discovery_evidence_path"),
            field=f"categories[{index}].discovery_evidence_path",
        )
        expected_discovery_sha = _lower_sha256(
            category.get("discovery_evidence_sha256"),
            field=f"categories[{index}].discovery_evidence_sha256",
        )
        actual_discovery_sha = _sha256_path(discovery_path)
        if actual_discovery_sha != expected_discovery_sha:
            raise OperationsEvidenceError(
                f"discovery evidence SHA-256 mismatch for writer category {kind}"
            )
        writers = category.get("writers")
        if not isinstance(writers, list):
            raise OperationsEvidenceError(f"categories[{index}].writers must be an array")
        if applicable and not writers:
            raise OperationsEvidenceError(f"applicable writer category {kind} has no writers")
        if not applicable:
            if writers:
                raise OperationsEvidenceError(f"non-applicable writer category {kind} cannot list writers")
            _nonblank(
                category.get("not_applicable_reason"),
                field=f"categories[{index}].not_applicable_reason",
            )
        category_writers: list[dict[str, Any]] = []
        for writer_index, writer in enumerate(writers):
            if not isinstance(writer, dict):
                raise OperationsEvidenceError(
                    f"categories[{index}].writers[{writer_index}] must be an object"
                )
            writer_id = _nonblank(
                writer.get("writer_id"),
                field=f"categories[{index}].writers[{writer_index}].writer_id",
            )
            if writer_id in writer_ids:
                raise OperationsEvidenceError(f"duplicate writer_id: {writer_id}")
            writer_ids.add(writer_id)
            identity = _nonblank(
                writer.get("identity"),
                field=f"categories[{index}].writers[{writer_index}].identity",
            )
            state = _nonblank(
                writer.get("state"),
                field=f"categories[{index}].writers[{writer_index}].state",
            )
            if state not in ALLOWED_WRITER_STATES[kind]:
                raise OperationsEvidenceError(
                    f"writer {writer_id} state {state!r} is not closed for category {kind}"
                )
            evidence_path = _owner_only_regular_file(
                writer.get("evidence_path"),
                field=f"categories[{index}].writers[{writer_index}].evidence_path",
            )
            expected_sha = _lower_sha256(
                writer.get("evidence_sha256"),
                field=f"categories[{index}].writers[{writer_index}].evidence_sha256",
            )
            actual_sha = _sha256_path(evidence_path)
            if actual_sha != expected_sha:
                raise OperationsEvidenceError(f"evidence SHA-256 mismatch for writer {writer_id}")
            observation = {
                "writer_id": writer_id,
                "kind": kind,
                "identity": identity,
                "state": state,
                "evidence_path": str(evidence_path),
                "evidence_sha256": actual_sha,
            }
            category_writers.append(observation)
            observations.append(observation)
        by_kind[kind] = {
            "kind": kind,
            "applicable": applicable,
            "discovery_evidence_path": str(discovery_path),
            "discovery_evidence_sha256": actual_discovery_sha,
            "writer_count": len(category_writers),
            "writers": category_writers,
            "not_applicable_reason": (
                _nonblank(category.get("not_applicable_reason"), field="not_applicable_reason")
                if not applicable
                else None
            ),
        }
    missing = sorted(set(REQUIRED_WRITER_KINDS) - set(by_kind))
    if missing:
        raise OperationsEvidenceError("writer inventory is missing categories: " + ", ".join(missing))
    report = {
        "format": WRITER_INVENTORY_REPORT_FORMAT,
        "status": "pass",
        "ok": True,
        "inventory_path": str(inventory_file),
        "inventory_sha256": _sha256_bytes(inventory_raw),
        "candidate_id": candidate_id,
        "cutover_run_id": cutover_run_id,
        "source_commit": source_commit,
        "required_categories": list(REQUIRED_WRITER_KINDS),
        "categories": [by_kind[kind] for kind in REQUIRED_WRITER_KINDS],
        "writer_count": len(observations),
    }
    report["report_sha256"] = _sha256_bytes(_canonical_json_bytes(report))
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dish-pg-operations-evidence")
    subcommands = parser.add_subparsers(dest="command", required=True)

    fingerprint = subcommands.add_parser(
        "database-fingerprint",
        help="fingerprint one repeatable-read PostgreSQL snapshot without writing to it",
    )
    fingerprint.add_argument("--database-url-env", default="DISH_PG_URL")
    fingerprint.add_argument("--expected-database-name")
    fingerprint.add_argument("--expected-schema-head", default=ALEMBIC_HEAD)
    fingerprint.add_argument("--output", type=Path, required=True)

    compare = subcommands.add_parser(
        "compare-database-fingerprints",
        help="compare source and restored database fingerprints",
    )
    compare.add_argument("--source", type=Path, required=True)
    compare.add_argument("--restored", type=Path, required=True)
    compare.add_argument("--output", type=Path, required=True)

    inventory = subcommands.add_parser(
        "validate-legacy-writer-inventory",
        help="verify inventory coverage and every local evidence-file digest",
    )
    inventory.add_argument("--file", type=Path, required=True)
    inventory.add_argument("--expected-candidate-id", required=True)
    inventory.add_argument("--expected-cutover-run-id", required=True)
    inventory.add_argument("--expected-source-commit", required=True)
    inventory.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "database-fingerprint":
            report = fingerprint_database(
                database_url=os.environ.get(args.database_url_env, ""),
                database_url_env=args.database_url_env,
                expected_database_name=args.expected_database_name,
                expected_schema_head=args.expected_schema_head,
            )
        elif args.command == "compare-database-fingerprints":
            report = compare_fingerprint_reports(
                source_path=args.source,
                restored_path=args.restored,
            )
        else:
            report = validate_legacy_writer_inventory(
                inventory_path=args.file,
                expected_candidate_id=args.expected_candidate_id,
                expected_cutover_run_id=args.expected_cutover_run_id,
                expected_source_commit=args.expected_source_commit,
            )
    except (
        OperationsEvidenceError,
        OSError,
        RuntimeError,
        SQLAlchemyError,
        TypeError,
        ValueError,
    ) as exc:
        report = {
            "format": "dish-postgresql-operations-evidence-error-v1",
            "status": "fail",
            "ok": False,
            "command": args.command,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        report["report_sha256"] = _sha256_bytes(_canonical_json_bytes(report))
        _write_atomic(args.output, report)
        print(json.dumps({"ok": False, "path": str(args.output), "error": str(exc)}, sort_keys=True, separators=(",", ":")))
        return 1
    _write_atomic(args.output, report)
    print(
        json.dumps(
            {
                "ok": bool(report.get("ok")),
                "path": str(args.output),
                "report_sha256": report["report_sha256"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0 if report.get("ok") is True else 1
