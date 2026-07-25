"""Step 8 protocol-native Verification correction and hold routes."""
from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Any

from .constants import COOKING_PROJECT_GID
from .database import create_verification_cycle, record_audit
from .errors import DishRuleError
from .models import utc_now
from .lifecycle import assert_transition, hold, pending_verification, require_status, resumed
from .task_document import DocumentParseError, TaskState, parse_task_document, validate_task_document
from .task_store import read_complete_task, write_exact_content
from .step7 import approve_live, assert_verifier_authority, bind_cycle_review

ROUTES = {"large", "evidence", "human-review"}
RESET_CATEGORIES = {"evidence", "premise", "method", "scope"}


def _rows(conn: sqlite3.Connection, operation_id: str):
    op = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found")
    if op["status"] != "open":
        raise DishRuleError("WRONG_STATE", "operation is not open", rule="operation_not_open")
    cycle = conn.execute("SELECT * FROM verification_cycles WHERE operation_id = ? AND completed_at IS NULL ORDER BY cycle_number DESC LIMIT 1", (operation_id,)).fetchone()
    if cycle is None:
        raise DishRuleError("WRONG_STATE", "operation has no pending Verification cycle", rule="verification_cycle_missing")
    return op, cycle


def _candidate(path: str):
    try:
        return parse_task_document(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise DishRuleError("INVALID_ARGUMENT", "corrected candidate could not be read", rule="candidate_file_unreadable") from exc
    except DocumentParseError as exc:
        raise DishRuleError("VALIDATION_FAILED", "corrected candidate is not canonical", rule=exc.rule) from exc


def _render(document):
    lines = document.render().splitlines()
    return lines[0], "\n".join(lines[1:]) + "\n"


def _write_document(conn, backend, op, live, document):
    check = validate_task_document(document, expected_schema_version=op["schema_version"])
    if not check.ok:
        raise DishRuleError("VALIDATION_FAILED", "candidate failed deterministic validation", errors=[{"rule": f.rule, "kind": f.kind.value} for f in check.findings])
    title, notes = _render(document)
    return write_exact_content(conn, backend, operation_id=op["operation_id"], task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID, expected_identity=live.identity, expected_section_gid=live.section_gid, title=title, notes=notes, schema_version=op["schema_version"])


def approve_small(conn: sqlite3.Connection, backend: Any, *, operation_id: str, agent: str, file_path: str, reviewed_identity: str, semantic_review_complete: bool, provenance_complete: bool, run_id: str | None = None, independence_attestation: str | None = None):
    op, cycle = _rows(conn, operation_id)
    assert_verifier_authority(cycle, agent=agent, run_id=run_id, independence_attestation=independence_attestation)
    if not semantic_review_complete or not provenance_complete:
        raise DishRuleError("VALIDATION_FAILED", "semantic self-review and provenance completion are required", rule="verification_inputs_incomplete")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    persisted_reviewed = cycle["reviewed_identity"]
    if not persisted_reviewed or not cycle["reviewed_content_version_id"]:
        raise DishRuleError("WRONG_STATE", "Verification cycle has no persisted reviewed content", rule="reviewed_content_missing")
    if reviewed_identity != persisted_reviewed:
        raise DishRuleError("CONFLICT", "caller review identity does not match the persisted review", rule="reviewed_identity_mismatch")
    if live.identity != persisted_reviewed:
        raise DishRuleError("CONFLICT", "live candidate changed after verifier review", rule="stale_verifier_review")
    corrected = _candidate(file_path)
    state = dict(corrected.state.values)
    state.update({"Status": "pending-verification", "Status detail": "None", "Resume status": "None", "Verified by": "None", "Verification protocol release": cycle["protocol_release"]})
    changes = tuple(corrected.material_changes) + (f"{utc_now()[:10]} — {agent}: small verification correction; exact candidate replaced and self-reviewed",)
    corrected = dataclasses.replace(corrected, state=TaskState(state), material_changes=changes)
    confirmed = _write_document(conn, backend, op, live, corrected)
    bind_cycle_review(conn, cycle_id=cycle["cycle_id"], operation_id=operation_id, task_gid=op["task_gid"], identity=confirmed.identity)
    conn.execute("UPDATE verification_cycles SET correction_class = 'small' WHERE cycle_id = ?", (cycle["cycle_id"],))
    return approve_live(conn, backend, operation_id=operation_id, agent=agent, reviewed_identity=confirmed.identity, semantic_review_complete=True, provenance_complete=True, correction_class="small", run_id=run_id, independence_attestation=independence_attestation)


def reject_route(conn: sqlite3.Connection, backend: Any, *, operation_id: str, agent: str, route: str, reason: str, file_path: str | None = None, resume_status: str | None = None, run_id: str | None = None, independence_attestation: str | None = None):
    op, cycle = _rows(conn, operation_id)
    assert_verifier_authority(cycle, agent=agent, run_id=run_id, independence_attestation=independence_attestation)
    route = str(route or "").strip()
    reason = str(reason or "").strip()
    if route not in ROUTES:
        raise DishRuleError("INVALID_ARGUMENT", "route must be large, evidence, or human-review", rule="invalid_rejection_route")
    if not reason:
        raise DishRuleError("INVALID_ARGUMENT", "route reason is required", rule="rejection_reason_required")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    persisted_reviewed = cycle["reviewed_identity"]
    if not persisted_reviewed or not cycle["reviewed_content_version_id"]:
        raise DishRuleError("WRONG_STATE", "Verification cycle has no persisted reviewed content", rule="reviewed_content_missing")
    if live.identity != persisted_reviewed:
        raise DishRuleError("CONFLICT", "live candidate changed after verifier review", rule="stale_verifier_review")
    document = parse_task_document(f"{live.title}\n{live.notes}")
    require_status(document.state, {"pending-verification"}, action="Verification outcome")
    state = dict(document.state.values)
    changes = tuple(document.material_changes)

    if route == "large":
        if not file_path:
            raise DishRuleError("INVALID_ARGUMENT", "Large correction requires a complete corrected candidate", rule="large_candidate_required")
        document = _candidate(file_path)
        assert_transition(action="large_correction", before="pending-verification", after="pending-verification")
        state = dict(pending_verification(document.state.values, protocol_release=cycle["protocol_release"]).values)
        changes = tuple(document.material_changes) + (f"{utc_now()[:10]} — {agent}: large verification correction — {reason}",)
        document = dataclasses.replace(document, state=TaskState(state), material_changes=changes)
    elif route == "evidence":
        if resume_status not in {"pending-verification", "pending-research"}:
            raise DishRuleError("INVALID_ARGUMENT", "Evidence route requires a valid resume status", rule="resume_status_required")
        assert_transition(action="request_evidence", before="pending-verification", after="pending-evidence")
        document = dataclasses.replace(document, state=hold(state, target="pending-evidence", detail=reason, resume_status=resume_status))
    else:
        if resume_status not in {"pending-verification", "pending-research"}:
            raise DishRuleError("INVALID_ARGUMENT", "Human Review route requires a valid resume status", rule="resume_status_required")
        assert_transition(action="request_human_review", before="pending-verification", after="pending-human-review")
        document = dataclasses.replace(document, state=hold(state, target="pending-human-review", detail=reason, resume_status=resume_status))

    completed = conn.execute("SELECT COUNT(*) FROM verification_cycles WHERE operation_id = ? AND completed_at IS NOT NULL AND outcome != 'approved'", (operation_id,)).fetchone()[0]
    two_pass = completed + 1 >= 2 and route == "large"
    if two_pass:
        assert_transition(action="two_pass_hold", before="pending-verification", after="pending-human-review")
        document = dataclasses.replace(document, state=hold(document.state.values, target="pending-human-review", detail=f"Two independent Verification passes ended without a signable task: {reason}", resume_status="pending-verification"))

    confirmed = _write_document(conn, backend, op, live, document)
    outcome = "two-pass-hold" if two_pass else "rejected"
    conn.execute("UPDATE verification_cycles SET correction_class = ?, outcome = ?, route = ?, resume_state = ?, completed_at = ? WHERE cycle_id = ?", ("large" if route == "large" else None, outcome, {"evidence": "evidence", "human-review": "human_review"}.get(route), document.state.values["Resume status"], utc_now(), cycle["cycle_id"]))
    if route == "large" and not two_pass:
        next_number = conn.execute("SELECT COALESCE(MAX(cycle_number), 0) + 1 FROM verification_cycles WHERE task_gid = ?", (op["task_gid"],)).fetchone()[0]
        new_cycle = create_verification_cycle(conn, operation_id=operation_id, task_gid=op["task_gid"], cycle_number=next_number, protocol_release=document.state.values["Verification protocol release"], protocol_text=cycle["protocol_text"], route=None)
        conn.execute("UPDATE operations SET editor_agent = ?, verifier_agent = NULL, run_id = ?, independence_attestation = NULL WHERE operation_id = ?", (agent, cycle["run_id"], operation_id))
    else:
        new_cycle = None
    record_audit(conn, submission_id=None, task_gid=op["task_gid"], operation_id=operation_id, event_type="verification.rejected", actor_agent=agent, details={"cycle_id": cycle["cycle_id"], "route": route, "reason": reason, "two_pass_hold": two_pass, "identity": confirmed.identity}, result_code="OK", result_ok=True)
    return {"operation_id": operation_id, "route": route, "two_pass_hold": two_pass, "new_cycle_id": None if new_cycle is None else new_cycle["cycle_id"], "task": dataclasses.asdict(confirmed)}


def reopen_two_pass(conn: sqlite3.Connection, backend: Any, *, operation_id: str, category: str, before: str, after: str, editor: str, date: str):
    op = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    if category not in RESET_CATEGORIES or not all(str(x or "").strip() for x in (before, after, editor, date)):
        raise DishRuleError("INVALID_ARGUMENT", "reopen requires substantive category, before, after, editor, and date", rule="substantive_reset_required")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    document = parse_task_document(f"{live.title}\n{live.notes}")
    if document.state.values["Status"] != "pending-human-review" or document.state.values["Resume status"] != "pending-verification":
        raise DishRuleError("WRONG_STATE", "task is not on the two-pass Verification hold", rule="two_pass_hold_required")
    state = resumed(document.state.values)
    entry = f"{date} — {editor}: {category}; before: {before}; after: {after}"
    document = dataclasses.replace(document, state=state, material_changes=tuple(document.material_changes) + (entry,))
    confirmed = _write_document(conn, backend, op, live, document)
    number = conn.execute("SELECT COALESCE(MAX(cycle_number), 0) + 1 FROM verification_cycles WHERE task_gid = ?", (op["task_gid"],)).fetchone()[0]
    cycle = create_verification_cycle(conn, operation_id=operation_id, task_gid=op["task_gid"], cycle_number=number, protocol_release=document.state.values["Verification protocol release"], protocol_text=conn.execute("SELECT protocol_text FROM verification_cycles WHERE operation_id = ? ORDER BY cycle_number DESC LIMIT 1", (operation_id,)).fetchone()[0], route=None)
    conn.execute("UPDATE operations SET editor_agent = ?, verifier_agent = NULL, run_id = NULL, independence_attestation = NULL WHERE operation_id = ?", (editor, operation_id))
    return {"operation_id": operation_id, "cycle_id": cycle["cycle_id"], "task": dataclasses.asdict(confirmed), "material_change": entry}
