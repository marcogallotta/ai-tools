"""Step 8 protocol-native Verification correction and hold routes."""
from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Any

from .constants import COOKING_PROJECT_GID
from .database import create_verification_cycle, record_audit, record_actor_fact, transition_operation, declare_operation_step, complete_operation_step, content_identity, release_marco_authorization_reservations
from .errors import DishRuleError
from .models import utc_now, material_editor_line
from .lifecycle import assert_transition, hold, pending_verification, require_status, resumed
from .task_document import DocumentParseError, TaskState, parse_task_document, validate_task_document
from .task_store import read_complete_task, write_exact_content
from .releases import current_verification_protocol_release
from .governed_diff import require_governed_authorization, require_small_scope
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


def _write_document(conn, backend, op, live, document, *, schema=None, authorization_ids=()):
    check = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not check.ok:
        raise DishRuleError("VALIDATION_FAILED", "candidate failed deterministic validation", errors=[{"rule": f.rule, "kind": f.kind.value} for f in check.findings])
    title, notes = _render(document)
    try:
        return write_exact_content(
            conn, backend, operation_id=op["operation_id"], task_gid=op["task_gid"],
            project_gid=COOKING_PROJECT_GID, expected_identity=live.identity,
            expected_section_gid=live.section_gid, title=title, notes=notes,
            schema_version=op["schema_version"],
            context={"authorization_ids": list(authorization_ids)} if authorization_ids else None,
        )
    except DishRuleError as exc:
        if authorization_ids and exc.code != "BACKEND_UNCERTAIN":
            release_marco_authorization_reservations(
                conn, operation_id=op["operation_id"], authorization_ids=authorization_ids
            )
        raise


def approve_small(conn: sqlite3.Connection, backend: Any, *, operation_id: str, agent: str, file_path: str, reviewed_identity: str, semantic_review_complete: bool, provenance_complete: bool, run_id: str | None = None, independence_attestation: str | None = None, schema=None):
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
    reviewed_document = parse_task_document(f"{live.title}\n{live.notes}")
    corrected_state = dict(corrected.state.values)
    corrected_state["Researched by"] = reviewed_document.state.values["Researched by"]
    corrected = dataclasses.replace(corrected, state=TaskState(corrected_state))
    require_small_scope(reviewed_document, corrected)
    state = dict(corrected.state.values)
    state.update({"Status": "pending-verification", "Status detail": "None", "Resume status": "None", "Verified by": "None", "Verification protocol release": cycle["protocol_release"], "Self-verified": material_editor_line(agent, utc_now()[:10])})
    changes = tuple(corrected.material_changes) + (f"{utc_now()[:10]} — {agent}: small verification correction; exact candidate replaced and self-reviewed",)
    corrected = dataclasses.replace(corrected, state=TaskState(state), material_changes=changes)
    precheck = validate_task_document(corrected, expected_schema_version=op["schema_version"], schema=schema)
    if not precheck.ok:
        raise DishRuleError("VALIDATION_FAILED", "candidate failed deterministic validation", errors=[{"rule": f.rule, "kind": f.kind.value} for f in precheck.findings])
    authorization_ids = require_governed_authorization(
        conn, reviewed_document, corrected, task_gid=op["task_gid"], operation_id=operation_id
    )
    intended_title, intended_notes = _render(corrected)
    intended_identity = content_identity(intended_title, intended_notes).digest
    declare_operation_step(conn, operation_id, "small_corrected_write", {"title": intended_title, "notes": intended_notes, "identity": intended_identity})
    declare_operation_step(conn, operation_id, "small_review_binding", {"cycle_id": cycle["cycle_id"], "identity": intended_identity})
    declare_operation_step(conn, operation_id, "small_signoff", {"cycle_id": cycle["cycle_id"], "agent": agent, "run_id": run_id, "independence_attestation": independence_attestation})
    confirmed = _write_document(conn, backend, op, live, corrected, schema=schema, authorization_ids=authorization_ids)
    complete_operation_step(conn, operation_id, "small_corrected_write")
    bind_cycle_review(conn, cycle_id=cycle["cycle_id"], operation_id=operation_id, task_gid=op["task_gid"], identity=confirmed.identity)
    complete_operation_step(conn, operation_id, "small_review_binding")
    conn.execute("UPDATE verification_cycles SET correction_class = 'small' WHERE cycle_id = ?", (cycle["cycle_id"],))
    result = approve_live(conn, backend, operation_id=operation_id, agent=agent, reviewed_identity=confirmed.identity, semantic_review_complete=True, provenance_complete=True, correction_class="small", run_id=run_id, independence_attestation=independence_attestation, schema=schema)
    complete_operation_step(conn, operation_id, "small_signoff")
    return result


def reject_route(conn: sqlite3.Connection, backend: Any, *, operation_id: str, agent: str, route: str, reason: str, file_path: str | None = None, resume_status: str | None = None, run_id: str | None = None, independence_attestation: str | None = None, schema=None, honest_root=None):
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
        corrected = _candidate(file_path)
        corrected_state = dict(corrected.state.values)
        corrected_state["Researched by"] = document.state.values["Researched by"]
        corrected = dataclasses.replace(corrected, state=TaskState(corrected_state))
        if honest_root is None:
            raise DishRuleError("INTERNAL_ERROR", "current Honest checkout is required for a new Verification cycle", rule="honest_root_required")
        snapshot = current_verification_protocol_release(honest_root)
        assert_transition(action="large_correction", before="pending-verification", after="pending-verification")
        state = dict(pending_verification(corrected.state.values, protocol_release=snapshot.identity).values)
        state["Self-verified"] = material_editor_line(agent, utc_now()[:10])
        changes = tuple(corrected.material_changes) + (f"{utc_now()[:10]} — {agent}: large verification correction — {reason}",)
        document = dataclasses.replace(corrected, state=TaskState(state), material_changes=changes)
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

    precheck = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not precheck.ok:
        raise DishRuleError("VALIDATION_FAILED", "candidate failed deterministic validation", errors=[{"rule": f.rule, "kind": f.kind.value} for f in precheck.findings])
    authorization_ids = require_governed_authorization(
        conn, parse_task_document(f"{live.title}\n{live.notes}"), document,
        task_gid=op["task_gid"], operation_id=operation_id,
    )
    intended_title, intended_notes = _render(document)
    outcome = "two-pass-hold" if two_pass else "rejected"
    target_phase = "held_human" if (two_pass or route == "human-review") else ("held_evidence" if route == "evidence" else "await_verification")
    route_suffix = cycle["cycle_id"]
    route_write_step = f"route_write:{route_suffix}"
    route_cycle_step = f"route_cycle_finalize:{route_suffix}"
    route_new_cycle_step = f"route_new_cycle:{route_suffix}"
    route_phase_step = f"route_phase:{route_suffix}"
    declare_operation_step(conn, operation_id, route_write_step, {"title": intended_title, "notes": intended_notes, "route": route})
    declare_operation_step(conn, operation_id, route_cycle_step, {
        "cycle_id": cycle["cycle_id"], "correction_class": "large" if route == "large" else None,
        "outcome": outcome, "route": {"evidence": "evidence", "human-review": "human_review"}.get(route),
        "resume_state": document.state.values["Resume status"],
    })
    if route == "large" and not two_pass:
        declare_operation_step(conn, operation_id, route_new_cycle_step, {"protocol_release": snapshot.identity, "protocol_text": snapshot.text})
    declare_operation_step(conn, operation_id, route_phase_step, {"phase": target_phase})
    confirmed = _write_document(conn, backend, op, live, document, schema=schema, authorization_ids=authorization_ids)
    complete_operation_step(conn, operation_id, route_write_step)
    if two_pass or route == "human-review":
        transition_operation(conn, operation_id, phase="held_human")
    elif route == "evidence":
        transition_operation(conn, operation_id, phase="held_evidence")
    conn.execute("UPDATE verification_cycles SET correction_class = ?, outcome = ?, route = ?, resume_state = ?, completed_at = ? WHERE cycle_id = ?", ("large" if route == "large" else None, outcome, {"evidence": "evidence", "human-review": "human_review"}.get(route), document.state.values["Resume status"], utc_now(), cycle["cycle_id"]))
    complete_operation_step(conn, operation_id, route_cycle_step)
    if route == "large" and not two_pass:
        next_number = conn.execute("SELECT COALESCE(MAX(cycle_number), 0) + 1 FROM verification_cycles WHERE task_gid = ?", (op["task_gid"],)).fetchone()[0]
        new_cycle = create_verification_cycle(conn, operation_id=operation_id, task_gid=op["task_gid"], cycle_number=next_number, protocol_release=snapshot.identity, protocol_text=snapshot.text, route=None)
        complete_operation_step(conn, operation_id, route_new_cycle_step)
        record_actor_fact(conn, operation_id=operation_id, task_gid=op["task_gid"], role="material_editor", agent=agent, run_id=cycle["run_id"], independence_attestation=cycle["independence_attestation"], candidate_identity=confirmed.identity, source_cycle_id=cycle["cycle_id"])
        conn.execute("UPDATE operations SET editor_agent = ?, verifier_agent = NULL, run_id = ?, independence_attestation = NULL WHERE operation_id = ?", (agent, cycle["run_id"], operation_id))
        transition_operation(conn, operation_id, phase="await_verification")
    else:
        new_cycle = None
    complete_operation_step(conn, operation_id, route_phase_step)
    record_audit(conn, submission_id=None, task_gid=op["task_gid"], operation_id=operation_id, event_type="verification.rejected", actor_agent=agent, details={"cycle_id": cycle["cycle_id"], "route": route, "reason": reason, "two_pass_hold": two_pass, "identity": confirmed.identity}, result_code="OK", result_ok=True, governed_kind="decision", before_state={"outcome": None, "reviewed_identity": cycle["reviewed_identity"], "status": "pending-verification"}, after_state={"outcome": outcome, "route": route, "resume_state": document.state.values["Resume status"], "status": document.state.values["Status"]}, actor_run_id=run_id, actor_attestation=independence_attestation)
    return {"operation_id": operation_id, "route": route, "two_pass_hold": two_pass, "new_cycle_id": None if new_cycle is None else new_cycle["cycle_id"], "task": dataclasses.asdict(confirmed)}


def reopen_two_pass(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    category: str,
    before: str,
    after: str,
    editor: str,
    run_id: str,
    file_path: str,
    date: str,
    honest_root=None,
    schema=None,
):
    op = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    if category not in RESET_CATEGORIES or not all(str(x or "").strip() for x in (before, after, editor, run_id, file_path, date)):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "reopen requires a corrected candidate, substantive reset, editor, and run proof",
            rule="substantive_reset_required",
        )
    if editor not in {"gpt", "codex", "claude"}:
        raise DishRuleError("INVALID_ARGUMENT", "reopen editor must be an agent", rule="reopen_editor_required")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    original = parse_task_document(f"{live.title}\n{live.notes}")
    if original.state.values["Status"] != "pending-human-review" or original.state.values["Resume status"] != "pending-verification":
        raise DishRuleError("WRONG_STATE", "task is not on the two-pass Verification hold", rule="two_pass_hold_required")
    candidate = _candidate(file_path)
    candidate_state = dict(candidate.state.values)
    candidate_state["Researched by"] = original.state.values["Researched by"]
    candidate = dataclasses.replace(candidate, state=TaskState(candidate_state))
    original_text = original.render()
    candidate_text = candidate.render()
    if before not in original_text or after not in candidate_text or candidate_text == original_text:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "corrected candidate does not demonstrate the declared substantive reset",
            rule="two_pass_reset_not_applied",
            details={"category": category, "before": before, "after": after},
        )
    if honest_root is None:
        previous = conn.execute(
            "SELECT protocol_release, protocol_text FROM verification_cycles WHERE operation_id=? ORDER BY cycle_number DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        snapshot = type("Snapshot", (), {"identity": previous["protocol_release"], "text": previous["protocol_text"]})()
    else:
        snapshot = current_verification_protocol_release(honest_root)
    state_values = dict(candidate.state.values)
    state_values.update({
        "Status": "pending-verification",
        "Status detail": "None",
        "Resume status": "None",
        "Verification protocol release": snapshot.identity,
        "Verified by": "None",
        "Self-verified": material_editor_line(editor, date),
    })
    entry = f"{date} — {editor}: {category}; before: {before}; after: {after}"
    document = dataclasses.replace(
        candidate,
        state=TaskState(state_values),
        material_changes=tuple(candidate.material_changes) + (entry,),
    )
    check = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not check.ok:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "corrected reopen candidate failed deterministic validation",
            errors=[{"rule": f.rule, "kind": f.kind.value} for f in check.findings],
        )
    authorization_ids = require_governed_authorization(
        conn, original, document, task_gid=op["task_gid"], operation_id=operation_id
    )
    intended_title, intended_notes = _render(document)
    intended_identity = content_identity(intended_title, intended_notes).digest
    declare_operation_step(conn, operation_id, "reopen_write", {"title": intended_title, "notes": intended_notes})
    declare_operation_step(conn, operation_id, "reopen_actor", {
        "role": "material_editor", "agent": editor, "run_id": run_id,
        "candidate_identity": intended_identity,
    })
    declare_operation_step(conn, operation_id, "reopen_cycle", {"protocol_release": snapshot.identity, "protocol_text": snapshot.text})
    declare_operation_step(conn, operation_id, "reopen_phase", {"phase": "await_verification"})
    confirmed = _write_document(
        conn, backend, op, live, document, schema=schema, authorization_ids=authorization_ids
    )
    complete_operation_step(conn, operation_id, "reopen_write")
    record_actor_fact(
        conn, operation_id=operation_id, task_gid=op["task_gid"], role="material_editor",
        agent=editor, run_id=run_id, candidate_identity=confirmed.identity,
    )
    complete_operation_step(conn, operation_id, "reopen_actor")
    number = conn.execute(
        "SELECT COALESCE(MAX(cycle_number), 0) + 1 FROM verification_cycles WHERE task_gid = ?",
        (op["task_gid"],),
    ).fetchone()[0]
    cycle = create_verification_cycle(
        conn, operation_id=operation_id, task_gid=op["task_gid"], cycle_number=number,
        protocol_release=snapshot.identity, protocol_text=snapshot.text, route=None,
    )
    complete_operation_step(conn, operation_id, "reopen_cycle")
    conn.execute(
        "UPDATE operations SET editor_agent=?, verifier_agent=NULL, run_id=?, independence_attestation=NULL WHERE operation_id=?",
        (editor, run_id, operation_id),
    )
    transition_operation(conn, operation_id, phase="await_verification")
    complete_operation_step(conn, operation_id, "reopen_phase")
    return {
        "operation_id": operation_id,
        "cycle_id": cycle["cycle_id"],
        "task": dataclasses.asdict(confirmed),
        "material_change": entry,
    }


def resolve_hold(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    resolution_kind: str,
    detail: str,
    resume_status: str,
    honest_root,
    schema=None,
    file_path: str | None = None,
    editor: str | None = None,
    run_id: str | None = None,
):
    """Resolve an Evidence or Human Review hold from exact live state.

    Resume-to-Research terminates the held operation so a fresh Research/change
    operation can be claimed. Resume-to-Verification creates a new cycle; a
    supplied candidate is treated as a material edit and freezes the current
    Verification release.
    """
    if resolution_kind not in {"evidence", "human_review"}:
        raise DishRuleError("INVALID_ARGUMENT", "invalid hold resolution kind", rule="invalid_hold_resolution")
    if resume_status not in {"pending-research", "pending-verification"}:
        raise DishRuleError("INVALID_ARGUMENT", "invalid hold resume status", rule="resume_status_required")
    clean_detail = str(detail or "").strip()
    if not clean_detail:
        raise DishRuleError("INVALID_ARGUMENT", "resolution detail is required", rule="resolution_detail_required")
    op = conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    expected_phase = "held_evidence" if resolution_kind == "evidence" else "held_human"
    if op["status"] != "open" or op["phase"] != expected_phase:
        raise DishRuleError("WRONG_STATE", "operation is not on the requested hold", rule="hold_not_active")
    cycle = conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id=? AND route=? ORDER BY cycle_number DESC LIMIT 1",
        (operation_id, resolution_kind),
    ).fetchone()
    if cycle is None:
        raise DishRuleError("WRONG_STATE", "hold has no persisted Verification decision", rule="hold_cycle_missing")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    before_doc = parse_task_document(f"{live.title}\n{live.notes}")
    expected_status = "pending-evidence" if resolution_kind == "evidence" else "pending-human-review"
    if before_doc.state.values["Status"] != expected_status:
        raise DishRuleError("WRONG_STATE", "live task does not match the persisted hold", rule="hold_state_mismatch")

    material = bool(file_path)
    snapshot = None
    if material:
        if not editor or editor not in {"gpt", "codex", "claude"}:
            raise DishRuleError("INVALID_ARGUMENT", "material hold resolution requires a named editor agent", rule="hold_editor_required")
        candidate = _candidate(file_path)
        candidate_state = dict(candidate.state.values)
        candidate_state["Researched by"] = before_doc.state.values["Researched by"]
        candidate = dataclasses.replace(candidate, state=TaskState(candidate_state))
        snapshot = current_verification_protocol_release(honest_root)
        values = dict(candidate.state.values)
        values.update({
            "Status": resume_status,
            "Status detail": "None",
            "Resume status": "None",
            "Verified by": "None",
            "Verification protocol release": snapshot.identity if resume_status == "pending-verification" else "None",
            "Self-verified": material_editor_line(editor, utc_now()[:10]),
        })
        decision = f"Human — Marco: {resolution_kind} resolved — {clean_detail}"
        authorization_decisions = tuple(candidate.decisions)
        decisions = authorization_decisions
        if decision not in decisions:
            decisions += (decision,)
        document = dataclasses.replace(candidate, state=TaskState(values), decisions=decisions)
    else:
        values = dict(resumed(before_doc.state.values).values)
        values["Status"] = resume_status
        values["Verification protocol release"] = "None" if resume_status == "pending-research" else cycle["protocol_release"]
        decision = f"Human — Marco: {resolution_kind} resolved — {clean_detail}"
        authorization_decisions = tuple(before_doc.decisions)
        decisions = authorization_decisions
        if decision not in decisions:
            decisions += (decision,)
        document = dataclasses.replace(before_doc, state=TaskState(values), decisions=decisions)

    precheck = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not precheck.ok:
        raise DishRuleError("VALIDATION_FAILED", "candidate failed deterministic validation", errors=[{"rule": f.rule, "kind": f.kind.value} for f in precheck.findings])
    authorization_document = dataclasses.replace(document, decisions=authorization_decisions)
    authorization_ids = require_governed_authorization(
        conn, before_doc, authorization_document, task_gid=op["task_gid"], operation_id=operation_id
    )
    intended_title, intended_notes = _render(document)
    declare_operation_step(conn, operation_id, "hold_resolution_write", {"title": intended_title, "notes": intended_notes, "resolution_kind": resolution_kind})
    declare_operation_step(conn, operation_id, "hold_resolution_decision", {"detail": clean_detail, "resume_status": resume_status, "material": material})
    if material:
        intended_identity = content_identity(intended_title, intended_notes).digest
        declare_operation_step(conn, operation_id, "hold_resolution_actor", {
            "role": "material_editor", "agent": editor, "run_id": run_id,
            "candidate_identity": intended_identity,
        })
    if resume_status == "pending-verification":
        if snapshot is None:
            next_release, next_text = cycle["protocol_release"], cycle["protocol_text"]
        else:
            next_release, next_text = snapshot.identity, snapshot.text
        declare_operation_step(conn, operation_id, "hold_resolution_cycle", {"protocol_release": next_release, "protocol_text": next_text})
        declare_operation_step(conn, operation_id, "hold_resolution_phase", {"phase": "await_verification", "status": "open"})
    else:
        declare_operation_step(conn, operation_id, "hold_resolution_phase", {"phase": "terminal", "status": "completed", "terminal_outcome": f"{resolution_kind}_resolved_to_research"})
    confirmed = _write_document(conn, backend, op, live, document, schema=schema, authorization_ids=authorization_ids)
    complete_operation_step(conn, operation_id, "hold_resolution_write")
    record_audit(
        conn, submission_id=None, task_gid=op["task_gid"], operation_id=operation_id,
        event_type="hold.resolved", actor_agent=editor if editor in {"gpt", "codex", "claude"} else None,
        details={"kind": resolution_kind, "detail": clean_detail, "resume_status": resume_status, "material": material, "identity": confirmed.identity},
        result_code="OK", result_ok=True, governed_kind="decision",
        before_state={"status": expected_status, "resume_status": before_doc.state.values["Resume status"]},
        after_state={"status": resume_status, "identity": confirmed.identity},
        actor_run_id=run_id, actor_source="marco-hold-resolution",
    )
    complete_operation_step(conn, operation_id, "hold_resolution_decision")
    if material:
        record_actor_fact(
            conn, operation_id=operation_id, task_gid=op["task_gid"], role="material_editor",
            agent=editor, run_id=run_id, candidate_identity=confirmed.identity,
        )
        complete_operation_step(conn, operation_id, "hold_resolution_actor")

    if resume_status == "pending-research":
        transition_operation(
            conn, operation_id, phase="terminal", status="completed",
            terminal_outcome=f"{resolution_kind}_resolved_to_research",
        )
        new_cycle = None
        complete_operation_step(conn, operation_id, "hold_resolution_phase")
    else:
        number = conn.execute(
            "SELECT COALESCE(MAX(cycle_number),0)+1 FROM verification_cycles WHERE task_gid=?",
            (op["task_gid"],),
        ).fetchone()[0]
        if snapshot is None:
            protocol_release = cycle["protocol_release"]
            protocol_text = cycle["protocol_text"]
        else:
            protocol_release = snapshot.identity
            protocol_text = snapshot.text
        new_cycle = create_verification_cycle(
            conn, operation_id=operation_id, task_gid=op["task_gid"], cycle_number=number,
            protocol_release=protocol_release, protocol_text=protocol_text,
        )
        complete_operation_step(conn, operation_id, "hold_resolution_cycle")
        conn.execute(
            "UPDATE operations SET verifier_agent=NULL, independence_attestation=NULL WHERE operation_id=?",
            (operation_id,),
        )
        transition_operation(conn, operation_id, phase="await_verification")
        complete_operation_step(conn, operation_id, "hold_resolution_phase")
    return {
        "operation_id": operation_id,
        "resolution_kind": resolution_kind,
        "resume_status": resume_status,
        "material": material,
        "new_cycle_id": None if new_cycle is None else new_cycle["cycle_id"],
        "task": dataclasses.asdict(confirmed),
    }
