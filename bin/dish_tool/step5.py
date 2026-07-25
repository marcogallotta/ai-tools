"""Step 5 command primitives: exact reads, claims, inspection, and migration."""
from __future__ import annotations

import dataclasses
import sqlite3
from typing import Any, Mapping

from .constants import COOKING_PROJECT_GID
from .database import confirm_task_content, create_operation, content_identity
from .errors import DishRuleError
from .models import OperationActors, ResolvedRelease
from .task_document import DocumentParseError, parse_task_document, validate_task_document
from .task_store import LiveTask, read_complete_task, write_exact_content


def parse_live_document(live: LiveTask):
    return parse_task_document(f"{live.title}\n{live.notes}")


def diagnostics_for(live: LiveTask, release: ResolvedRelease) -> dict[str, Any]:
    try:
        document = parse_live_document(live)
    except DocumentParseError as exc:
        return {"parsed": None, "validation": [{"rule": exc.rule, "message": str(exc)}], "schema_version": None, "migration_required": bool(live.notes)}
    validation = validate_task_document(document, expected_schema_version=release.schema_version)
    return {
        "parsed": {
            "title": document.title,
            "state": dict(document.state.values),
            "planning_brief": dict(document.planning_brief.values),
            "schema_version": document.schema_version,
        },
        "validation": [
            {"rule": f.rule, "kind": f.kind.value, "message": f.message, "location": f.location}
            for f in validation.findings
        ],
        "schema_version": document.schema_version,
        "migration_required": document.schema_version != release.schema_version,
    }


def claim_operation(conn: sqlite3.Connection, *, live: LiveTask, release: ResolvedRelease, kind: str, agent: str, run_id: str | None = None):
    existing = conn.execute("SELECT * FROM task_content_state WHERE task_gid = ?", (live.gid,)).fetchone()
    if existing is not None and existing["last_confirmed_identity"] != live.identity:
        raise DishRuleError("CONFLICT", "live task content changed outside the tool; re-verification is required", rule="live_task_drift", details={"expected_identity": existing["last_confirmed_identity"], "actual_identity": live.identity})
    if existing is None:
        confirm_task_content(conn, task_gid=live.gid, title=live.title, notes=live.notes, schema_version=release.schema_version, boundary="start_baseline")
    actors = OperationActors(editor_agent=agent if kind in {"planning", "change"} else None, researcher_agent=agent if kind == "initial" else None, run_id=str(run_id or "").strip() or None)
    return create_operation(conn, task_gid=live.gid, operation_kind=kind, expected_identity=live.identity, schema_version=release.schema_version, actors=actors)


def inspect_operation(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any]:
    op = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found")
    state = conn.execute("SELECT * FROM task_content_state WHERE task_gid = ?", (op["task_gid"],)).fetchone()
    cycles = conn.execute("SELECT * FROM verification_cycles WHERE operation_id = ? ORDER BY cycle_number", (operation_id,)).fetchall()
    actions = []
    if op["status"] == "open":
        actions = ["prepare"] if op["content_write_completed_at"] is None else (["approve", "reject"] if op["signoff_completed_at"] is None else ["submit"])
    return {
        "operation": {k: op[k] for k in op.keys()},
        "content": None if state is None else {"expected_identity": op["expected_identity"], "confirmed_identity": state["last_confirmed_identity"], "schema_version": state["schema_version"]},
        "actors": {"editor": op["editor_agent"], "researcher": op["researcher_agent"], "verifier": op["verifier_agent"], "run_id": op["run_id"], "independence_attestation": op["independence_attestation"]},
        "verification_cycles": [{k: row[k] for k in row.keys()} for row in cycles],
        "completion": {"content_write": op["content_write_completed_at"], "signoff": op["signoff_completed_at"], "movement": op["movement_completed_at"]},
        "legal_next_actions": actions,
    }


def migrate_live_task(conn: sqlite3.Connection, backend, *, task_gid: str, release: ResolvedRelease) -> LiveTask:
    live = read_complete_task(backend, task_gid=task_gid, project_gid=COOKING_PROJECT_GID)
    try:
        document = parse_live_document(live)
    except DocumentParseError as exc:
        raise DishRuleError("VALIDATION_FAILED", "older-schema task is not safely parseable", rule=exc.rule) from exc
    if document.schema_version == release.schema_version:
        raise DishRuleError("CONFLICT", "task already uses the current schema", rule="migration_not_required")
    migration = next((m for m in release.migration_metadata.values() if m["from_schema_version"] == document.schema_version and m["to_schema_version"] == release.schema_version), None)
    if migration is None:
        raise DishRuleError("VALIDATION_FAILED", "no supported migration path", rule="migration_path_missing", details={"from": document.schema_version, "to": release.schema_version})
    candidate = dataclasses.replace(document, schema_version=release.schema_version)
    validation = validate_task_document(candidate, expected_schema_version=release.schema_version)
    if not validation.ok:
        raise DishRuleError("VALIDATION_FAILED", "migrated candidate failed validation", errors=[{"rule": f.rule, "kind": f.kind.value} for f in validation.findings])
    confirm_task_content(conn, task_gid=task_gid, title=live.title, notes=live.notes, schema_version=document.schema_version, boundary="migration_baseline")
    op = create_operation(conn, task_gid=task_gid, operation_kind="migration", expected_identity=live.identity, schema_version=document.schema_version)
    rendered = candidate.render().splitlines()
    title, notes = rendered[0], "\n".join(rendered[1:]) + "\n"
    return write_exact_content(conn, backend, operation_id=op["operation_id"], task_gid=task_gid, project_gid=COOKING_PROJECT_GID, expected_identity=live.identity, expected_section_gid=live.section_gid, title=title, notes=notes, schema_version=release.schema_version)
