#!/usr/bin/env python3
"""One-off offline initializer for Dish corpus-migration durable state.

This script never accesses Asana or the network. It resolves the two offline
placeholders, validates every resulting canonical document with Dish's own
parser/validator, and then records the exact confirmed content baseline plus an
append-only migration-assignment audit event through Dish's persistence layer.

It intentionally creates no workflow operation, Research evidence,
Verification cycle, verifier inspection fact, or signoff binding. The assigned
Status is therefore explicitly migration authority, not evidence that a live
Dish workflow produced it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from dish_tool.database import confirm_task_content, record_audit
from dish_tool.database_schema import _validate_semantic_evidence, initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.task_document import (
    DocumentParseError,
    parse_task_document,
    validate_task_document,
)
from dish_tool.transactions import immediate_transaction
from dish_service.database_ownership import ServiceDatabaseOwnership

STATE_PLACEHOLDER = "{{MIGRATED_DISH_STATE_BLOCK}}"
SECTION_PLACEHOLDER = "{{TARGET_SECTION_GID}}"
STATE_FIELDS = (
    "Status",
    "Status detail",
    "Resume status",
    "Verification protocol release",
    "Researched by",
    "Verified by",
    "Self-verified",
)
ALLOWED_STATUSES = {
    "pending-research",
    "pending-evidence",
    "pending-human-review",
    "pending-verification",
    "ready",
}
OFFLINE_ACK = "I CONFIRM THIS DATABASE IS OFFLINE AND THROWAWAY OR FRESH PRODUCTION"


class ImportFailure(Exception):
    pass


@dataclass(frozen=True)
class PreparedTask:
    source_gid: str
    target_gid: str
    source_name: str
    title: str
    notes: str
    schema_version: str
    status: str
    assignment: Mapping[str, Any]
    template_path: Path
    template_sha256: str
    final_sha256: str


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ImportFailure(f"missing file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ImportFailure(f"invalid JSON in {path}: {exc}") from exc


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _as_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ImportFailure(f"{label} must be a JSON object")
    return value


def _load_assignments(path: Path) -> Mapping[str, Mapping[str, Any]]:
    raw = _as_mapping(_load_json(path), "assignments")
    result: dict[str, Mapping[str, Any]] = {}
    for gid, value in raw.items():
        clean_gid = str(gid).strip()
        if not clean_gid.isdigit():
            raise ImportFailure(f"assignment key is not a numeric source_gid: {gid!r}")
        assignment = _as_mapping(value, f"assignment {clean_gid}")
        missing = [
            field
            for field in ("target_gid", *STATE_FIELDS, "authority", "rationale")
            if field not in assignment
        ]
        if missing:
            raise ImportFailure(f"assignment {clean_gid} missing fields: {', '.join(missing)}")
        target_gid = str(assignment["target_gid"]).strip()
        if not target_gid.isdigit():
            raise ImportFailure(f"assignment {clean_gid} has invalid target_gid {target_gid!r}")
        status = str(assignment["Status"]).strip()
        if status not in ALLOWED_STATUSES:
            raise ImportFailure(f"assignment {clean_gid} has unsupported Status {status!r}")
        if not str(assignment["authority"]).strip() or not str(assignment["rationale"]).strip():
            raise ImportFailure(f"assignment {clean_gid} requires non-empty authority and rationale")
        result[clean_gid] = assignment
    target_gids = [str(value["target_gid"]).strip() for value in result.values()]
    if len(target_gids) != len(set(target_gids)):
        raise ImportFailure("assignments contain duplicate target_gid values")
    return result


def _load_registry(path: Path) -> Mapping[str, str]:
    raw = _as_mapping(_load_json(path), "section registry")
    registry: dict[str, str] = {}
    for name, gid in raw.items():
        clean_name = str(name).strip()
        clean_gid = str(gid).strip()
        if not clean_name or not clean_gid.isdigit():
            raise ImportFailure(f"invalid section registry entry: {name!r} -> {gid!r}")
        registry[clean_name] = clean_gid
    return registry


def _state_block(assignment: Mapping[str, Any]) -> str:
    return "\n".join(f"{field}: {str(assignment[field]).strip()}" for field in STATE_FIELDS)


def _split_title_notes(rendered: str) -> tuple[str, str]:
    if "\n" not in rendered:
        raise ImportFailure("resolved template does not contain a title and notes body")
    title, notes = rendered.split("\n", 1)
    return title, notes if notes.endswith("\n") else notes + "\n"


def _format_findings(findings: Any) -> str:
    return "; ".join(
        f"{item.rule} at {item.location or 'document'}: {item.message}" for item in findings
    )


def _prepare_tasks(
    *, batch_dir: Path, schema_path: Path, assignments_path: Path,
    registry_path: Path, expected_count: int,
) -> list[PreparedTask]:
    manifest_path = batch_dir / "manifest-batch-002.json"
    manifest = _load_json(manifest_path)
    if not isinstance(manifest, list):
        raise ImportFailure("batch manifest must be a JSON array")
    if len(manifest) != expected_count:
        raise ImportFailure(f"batch manifest has {len(manifest)} rows; expected {expected_count}")
    schema = _as_mapping(_load_json(schema_path), "Dish schema")
    schema_version = str(schema.get("schema_version") or "").strip()
    if not schema_version:
        raise ImportFailure("Dish schema does not declare schema_version")
    assignments = _load_assignments(assignments_path)
    registry = _load_registry(registry_path)

    rows_by_gid: dict[str, Mapping[str, Any]] = {}
    failures: list[str] = []
    for index, row_value in enumerate(manifest, start=1):
        if not isinstance(row_value, dict):
            failures.append(f"manifest row {index} is not an object")
            continue
        gid = str(row_value.get("source_gid") or "").strip()
        if not gid.isdigit():
            failures.append(f"manifest row {index} has invalid source_gid {gid!r}")
            continue
        if gid in rows_by_gid:
            failures.append(f"duplicate source_gid in manifest: {gid}")
            continue
        rows_by_gid[gid] = row_value
    manifest_gids = set(rows_by_gid)
    assignment_gids = set(assignments)
    for gid in sorted(manifest_gids - assignment_gids):
        failures.append(f"missing durable-state assignment for {gid}")
    for gid in sorted(assignment_gids - manifest_gids):
        failures.append(f"orphan durable-state assignment for {gid}")
    if failures:
        raise ImportFailure("\n".join(failures))

    prepared: list[PreparedTask] = []
    for gid in sorted(manifest_gids, key=lambda item: int(item)):
        row = rows_by_gid[gid]
        source_name = str(row.get("source_name") or row.get("expected_name") or "").strip()
        template_rel = str(row.get("template_file") or "").strip()
        captured_section = str(row.get("captured_section_name") or "").strip()
        if not source_name or not template_rel or not captured_section:
            failures.append(f"{gid}: manifest lacks source_name, template_file, or captured_section_name")
            continue
        section_gid = registry.get(captured_section)
        if section_gid is None:
            failures.append(f"{gid} {source_name}: section registry lacks {captured_section!r}")
            continue
        template_path = (batch_dir / template_rel).resolve()
        try:
            template_path.relative_to(batch_dir.resolve())
        except ValueError:
            failures.append(f"{gid} {source_name}: template path escapes batch directory")
            continue
        try:
            template_bytes = template_path.read_bytes()
        except FileNotFoundError:
            failures.append(f"{gid} {source_name}: missing template {template_rel}")
            continue
        template_hash = _sha256(template_bytes)
        manifest_hash = str(row.get("template_sha256") or "").strip()
        if template_hash != manifest_hash:
            failures.append(
                f"{gid} {source_name}: template SHA-256 {template_hash} != manifest {manifest_hash}"
            )
            continue
        try:
            template = template_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            failures.append(f"{gid} {source_name}: template is not UTF-8: {exc}")
            continue
        if template.count(STATE_PLACEHOLDER) != 1:
            failures.append(f"{gid} {source_name}: expected exactly one state placeholder")
            continue
        if template.count(SECTION_PLACEHOLDER) != 1:
            failures.append(f"{gid} {source_name}: expected exactly one section placeholder")
            continue
        resolved = template.replace(STATE_PLACEHOLDER, _state_block(assignments[gid]))
        resolved = resolved.replace(SECTION_PLACEHOLDER, section_gid)
        resolved = resolved.rstrip("\n") + f"\nSchema version: {schema_version}\n"
        if STATE_PLACEHOLDER in resolved or SECTION_PLACEHOLDER in resolved:
            failures.append(f"{gid} {source_name}: unresolved migration placeholder remains")
            continue
        try:
            document = parse_task_document(resolved)
        except DocumentParseError as exc:
            failures.append(f"{gid} {source_name}: canonical parse failed [{exc.rule}]: {exc}")
            continue
        validation = validate_task_document(
            document,
            expected_schema_version=schema_version,
            schema=schema,
        )
        if not validation.ok:
            failures.append(f"{gid} {source_name}: {_format_findings(validation.findings)}")
            continue
        title, notes = _split_title_notes(document.render())
        prepared.append(
            PreparedTask(
                source_gid=gid,
                target_gid=str(assignments[gid]["target_gid"]).strip(),
                source_name=source_name,
                title=title,
                notes=notes,
                schema_version=schema_version,
                status=str(assignments[gid]["Status"]).strip(),
                assignment=assignments[gid],
                template_path=template_path,
                template_sha256=template_hash,
                final_sha256=_sha256((title + "\n" + notes).encode("utf-8")),
            )
        )
    if failures:
        raise ImportFailure("\n".join(failures))
    if len(prepared) != expected_count:
        raise ImportFailure(f"prepared {len(prepared)} tasks; expected {expected_count}")
    return prepared


def _assert_fresh_offline_database(conn: sqlite3.Connection) -> None:
    occupied: dict[str, int] = {}
    for table in (
        "task_content_state", "content_versions", "operations", "verification_cycles",
        "write_attempts", "movement_attempts", "service_leases", "service_requests",
        "operation_executions", "audit_events",
    ):
        count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if count:
            occupied[table] = count
    if occupied:
        detail = ", ".join(f"{name}={count}" for name, count in sorted(occupied.items()))
        raise ImportFailure(f"database is not a fresh empty Dish database: {detail}")


def _write_tasks(conn: sqlite3.Connection, tasks: list[PreparedTask], *, batch_id: str) -> None:
    with immediate_transaction(conn, "corpus_migration_durable_state_import"):
        _assert_fresh_offline_database(conn)
        for task in tasks:
            identity = confirm_task_content(
                conn,
                task_gid=task.target_gid,
                title=task.title,
                notes=task.notes,
                schema_version=task.schema_version,
                boundary="corpus_migration_assigned_baseline",
            )
            record_audit(
                conn,
                submission_id=None,
                task_gid=task.target_gid,
                event_type="corpus_migration.state_assigned",
                actor_agent=None,
                actor_source="migration",
                details={
                    "batch_id": batch_id,
                    "assignment_kind": "migration-assigned",
                    "status": task.status,
                    "authority": str(task.assignment["authority"]).strip(),
                    "rationale": str(task.assignment["rationale"]).strip(),
                    "live_research_cycle_produced": False,
                    "live_verification_cycle_produced": False,
                    "template_sha256": task.template_sha256,
                    "final_document_sha256": task.final_sha256,
                    "confirmed_identity": identity.digest,
                    "source_gid": task.source_gid,
                    "target_gid": task.target_gid,
                    "source_name": task.source_name,
                },
                result_code="OK",
                result_ok=True,
            )
        _validate_semantic_evidence(conn)


def _verify_written(conn: sqlite3.Connection, tasks: list[PreparedTask], *, batch_id: str) -> list[str]:
    failures: list[str] = []
    expected = {task.target_gid: task for task in tasks}
    states = {
        row["task_gid"]: row
        for row in conn.execute("SELECT * FROM task_content_state ORDER BY task_gid")
    }
    if set(states) != set(expected):
        failures.append("task_content_state coverage differs from prepared task set")
    for gid, task in expected.items():
        state = states.get(gid)
        if state is None:
            failures.append(f"{gid}: missing task_content_state row")
            continue
        if state["last_confirmed_title"] != task.title or state["last_confirmed_notes"] != task.notes:
            failures.append(f"{gid}: confirmed content differs from rendered migration document")
        if state["schema_version"] != task.schema_version:
            failures.append(f"{gid}: stored schema version differs from migration schema")
        versions = conn.execute(
            "SELECT * FROM content_versions WHERE task_gid=? ORDER BY created_at, rowid", (gid,)
        ).fetchall()
        if len(versions) != 1:
            failures.append(f"{gid}: expected one content version, found {len(versions)}")
        elif versions[0]["boundary"] != "corpus_migration_assigned_baseline" or versions[0]["confirmed"] != 1:
            failures.append(f"{gid}: content version is not a confirmed migration baseline")
        events = conn.execute(
            "SELECT * FROM audit_events WHERE task_gid=? AND event_type='corpus_migration.state_assigned'",
            (gid,),
        ).fetchall()
        if len(events) != 1:
            failures.append(f"{gid}: expected one migration assignment audit event, found {len(events)}")
        else:
            details = json.loads(events[0]["details"])
            if details.get("batch_id") != batch_id or details.get("status") != task.status:
                failures.append(f"{gid}: migration assignment audit details do not match input")
            if details.get("source_gid") != task.source_gid or details.get("target_gid") != gid:
                failures.append(f"{gid}: migration assignment audit task mapping does not match input")
            if details.get("live_research_cycle_produced") is not False or details.get("live_verification_cycle_produced") is not False:
                failures.append(f"{gid}: audit event falsely claims live workflow evidence")
    for table in ("operations", "verification_cycles", "operation_actor_facts", "dish_inspect_facts"):
        count = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if count:
            failures.append(f"migration must not fabricate workflow evidence: {table} has {count} rows")
    _validate_semantic_evidence(conn)
    return failures


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-dir", required=True, type=Path)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--schema", required=True, type=Path)
    parser.add_argument("--assignments", required=True, type=Path)
    parser.add_argument("--section-registry", required=True, type=Path)
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--expected-count", type=int, default=99)
    parser.add_argument("--offline-confirmation", required=True)
    parser.add_argument("--prepare-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if args.offline_confirmation != OFFLINE_ACK:
            raise ImportFailure(
                "refusing to proceed: --offline-confirmation must exactly equal " + repr(OFFLINE_ACK)
            )
        if args.expected_count <= 0:
            raise ImportFailure("--expected-count must be positive")
        db_path = args.db.expanduser().resolve()
        known_test_db = Path("~/.local/state/dish/test/shared.sqlite3").expanduser().resolve()
        if db_path == known_test_db:
            raise ImportFailure("refusing to use the live test service database path")
        prepared = _prepare_tasks(
            batch_dir=args.batch_dir.expanduser().resolve(),
            schema_path=args.schema.expanduser().resolve(),
            assignments_path=args.assignments.expanduser().resolve(),
            registry_path=args.section_registry.expanduser().resolve(),
            expected_count=args.expected_count,
        )
        print(f"PREPARED {len(prepared)} schema-valid migration documents")
        if args.prepare_only:
            return 0
        if db_path.exists() and db_path.stat().st_size > 0:
            raise ImportFailure("refusing to initialize an existing non-empty database file")
        ServiceDatabaseOwnership(db_path).assert_local_access_allowed()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = initialize_database(db_path)
        try:
            _write_tasks(conn, prepared, batch_id=args.batch_id)
            failures = _verify_written(conn, prepared, batch_id=args.batch_id)
        finally:
            conn.close()
        if failures:
            for failure in failures:
                print(f"FAIL {failure}", file=sys.stderr)
            print(f"FAILED {len(failures)} post-write checks", file=sys.stderr)
            return 1
        print(f"PASS {len(prepared)} migration-assigned durable baselines written and reread")
        return 0
    except (DishRuleError, ImportFailure, OSError, sqlite3.Error) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
