"""Step 5 command primitives: exact reads, claims, inspection, and migration."""
from __future__ import annotations

import re
import sqlite3
from typing import Any, Mapping

from .constants import COOKING_PROJECT_GID
from .database import confirm_task_content, create_operation, content_identity, legal_operation_actions, transition_operation, declare_operation_step, complete_operation_step
from .errors import DishRuleError
from .models import OperationActors, ResolvedRelease
from .migrations import migrate_task_document
from .task_document import DocumentParseError, document_shape, parse_planning_brief, parse_task_document, validate_task_document, finding_payload
from .task_store import LiveTask, read_complete_task, write_exact_content

_SCHEMA_VERSION_LINE = re.compile(r"^Schema version:\s*(.+)$", re.MULTILINE)


def parse_live_document(live: LiveTask):
    return parse_task_document(f"{live.title}\n{live.notes}")


def _unparseable_migration_required(live: LiveTask, release: ResolvedRelease) -> bool:
    """Fallback migration signal for documents that failed structural parse.

    A parse failure means the document is malformed or mid-stage, not
    necessarily stale-schema. An empty task (nothing written yet) and a bare
    Planning brief are both stage-appropriate and never need migration.
    Anything else is judged by comparing its own Schema version line (if any)
    against the current release; a missing line is the genuine
    pre-schema/legacy case migration exists for.
    """
    if not live.notes.strip():
        return False
    try:
        parse_planning_brief(live.notes)
        return False
    except DocumentParseError:
        pass
    match = _SCHEMA_VERSION_LINE.search(live.notes)
    if match is None:
        return True
    return match.group(1).strip() != release.schema_version


def diagnostics_for(live: LiveTask, release: ResolvedRelease) -> dict[str, Any]:
    """Report validation findings against the grammar the document actually claims.

    Shape is decided by marker presence (`document_shape`), not by trying a
    canonical parse and retrying as a Planning brief on failure: a document
    with process-record markers is asserting canonical shape, so any parse
    failure on it is a genuine defect and must be reported as one, never
    reinterpreted as an ordinary Planning-stage document.
    """
    shape = document_shape(live.notes)
    if shape == "bare":
        return {"parsed": None, "validation": [], "schema_version": None, "migration_required": False}
    if shape == "planning_brief":
        try:
            parse_planning_brief(live.notes)
        except DocumentParseError as exc:
            return {"parsed": None, "validation": [{"rule": exc.rule, "message": str(exc)}], "schema_version": None, "migration_required": False}
        return {"parsed": None, "validation": [], "schema_version": None, "migration_required": False}
    try:
        document = parse_live_document(live)
    except DocumentParseError as exc:
        return {"parsed": None, "validation": [{"rule": exc.rule, "message": str(exc)}], "schema_version": None, "migration_required": _unparseable_migration_required(live, release)}
    validation = validate_task_document(document, expected_schema_version=release.schema_version, schema=release.schema)
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
    return create_operation(conn, task_gid=live.gid, operation_kind=kind, expected_identity=live.identity, schema_version=release.schema_version, expected_section_gid=live.section_gid, actors=actors)


def inspect_operation(conn: sqlite3.Connection, operation_id: str) -> dict[str, Any]:
    op = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found")
    state = conn.execute("SELECT * FROM task_content_state WHERE task_gid = ?", (op["task_gid"],)).fetchone()
    cycles = conn.execute("SELECT * FROM verification_cycles WHERE operation_id = ? ORDER BY cycle_number", (operation_id,)).fetchall()
    actions = legal_operation_actions(op)
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
    result = migrate_task_document(f"{live.title}\n{live.notes}", migration, schema=release.schema)
    if not result.ok or result.document is None:
        raise DishRuleError("VALIDATION_FAILED", "migration could not safely produce the current schema", rule="migration_quarantined", errors=[finding_payload(f) for f in result.findings])
    candidate = result.document
    confirm_task_content(conn, task_gid=task_gid, title=live.title, notes=live.notes, schema_version=document.schema_version, boundary="migration_baseline")
    op = create_operation(conn, task_gid=task_gid, operation_kind="migration", expected_identity=live.identity, schema_version=document.schema_version, expected_section_gid=live.section_gid)
    rendered = candidate.render().splitlines()
    title, notes = rendered[0], "\n".join(rendered[1:]) + "\n"
    declare_operation_step(conn, op["operation_id"], "migration_write", {"title": title, "notes": notes, "schema_version": release.schema_version})
    declare_operation_step(conn, op["operation_id"], "migration_terminal", {"status": "completed", "phase": "terminal", "terminal_outcome": "migration_confirmed"})
    confirmed = write_exact_content(conn, backend, operation_id=op["operation_id"], task_gid=task_gid, project_gid=COOKING_PROJECT_GID, expected_identity=live.identity, expected_section_gid=live.section_gid, title=title, notes=notes, schema_version=release.schema_version)
    complete_operation_step(conn, op["operation_id"], "migration_write")
    transition_operation(conn, op["operation_id"], phase="terminal", status="completed", terminal_outcome="migration_confirmed")
    complete_operation_step(conn, op["operation_id"], "migration_terminal")
    return confirmed
