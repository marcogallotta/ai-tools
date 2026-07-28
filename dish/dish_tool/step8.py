"""Step 8 protocol-native Verification correction and hold routes."""
from __future__ import annotations

import dataclasses
import difflib
import json
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .constants import COOKING_PROJECT_GID
from .database import create_verification_cycle, record_audit, record_actor_fact, transition_operation, declare_operation_step, complete_operation_step, content_identity, release_marco_authorization_reservations
from .errors import DishRuleError
from .models import material_change_line, material_editor_line, utc_now
from .lifecycle import assert_transition, hold, pending_verification, require_status, resumed
from .task_document import DocumentParseError, TaskState, document_parse_error_payloads, parse_task_document, validate_task_document, finding_payload
from .task_store import read_complete_task, write_exact_content
from .releases import current_verification_protocol_release
from .governed_diff import (
    require_governed_authorization,
    preserve_material_change_history,
    require_small_scope,
)
from .step7 import approve_live, assert_verifier_authority, bind_cycle_review

ROUTES = {"large", "evidence", "human-review"}
RESET_CATEGORIES = {"evidence", "premise", "method", "scope"}


def _preconstruction_research_hold(
    conn: sqlite3.Connection,
    *,
    op,
    agent: str,
    route: str,
    reason: str,
    resume_status: str | None,
    run_id: str | None,
    request_id: str | None,
    file_path: str | None,
    model: str | None,
) -> dict[str, Any]:
    if route not in {"evidence", "human-review"}:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "Research may be held before construction only for evidence or human review",
            rule="preconstruction_hold_route_invalid",
        )
    if resume_status != "pending-research":
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "pre-construction Research holds must resume to pending-research",
            rule="preconstruction_resume_status_invalid",
            details={"expected": "pending-research", "actual": resume_status},
        )
    if file_path:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "pre-construction Research holds cannot include candidate content",
            rule="hold_candidate_unexpected",
        )
    if model:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "pre-construction Research holds do not accept a model field",
            rule="hold_model_unexpected",
        )
    if agent != op["researcher_agent"]:
        raise DishRuleError(
            "AGENT_MISMATCH",
            "Research hold agent does not match the recorded researcher",
            rule="operation_actor_mismatch",
            details={"expected": op["researcher_agent"], "actual": agent},
        )
    if str(op["run_id"] or "").strip() != str(run_id or "").strip():
        raise DishRuleError(
            "AGENT_MISMATCH",
            "Research hold run does not match the originating Research run",
            rule="service_run_id_conflict",
        )
    resolver = "Marco/admin supply-evidence" if route == "evidence" else "Marco/admin record-human-decision"
    intended = {
        "description": "Research blocked before construction",
        "route": route,
        "reason": reason,
        "task_gid": op["task_gid"],
        "originating_agent": agent,
        "originating_run_id": run_id,
        "request_id": request_id,
        "timestamp": utc_now(),
        "resolver": resolver,
        "resume_status": "pending-research",
        "candidate_content_existed": False,
    }
    declare_operation_step(conn, op["operation_id"], "research_preconstruction_hold", intended)
    target_phase = "held_evidence" if route == "evidence" else "held_human"
    transition_operation(conn, op["operation_id"], phase=target_phase)
    complete_operation_step(conn, op["operation_id"], "research_preconstruction_hold")
    record_audit(
        conn,
        submission_id=None,
        task_gid=op["task_gid"],
        operation_id=op["operation_id"],
        event_type="research.preconstruction_blocked",
        actor_agent=agent,
        actor_run_id=run_id,
        details=intended,
        result_code="OK",
        result_ok=True,
        governed_kind="decision",
        before_state={"phase": "prepare_required", "candidate_content_existed": False},
        after_state={"phase": target_phase, "resume_status": "pending-research"},
        actor_source="research-command",
    )
    return {"operation_id": op["operation_id"], **intended, "phase": target_phase}


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
        raise DishRuleError("VALIDATION_FAILED", "corrected candidate is not canonical", errors=document_parse_error_payloads(exc)) from exc


def _render(document):
    lines = document.render().splitlines()
    return lines[0], "\n".join(lines[1:]) + "\n"


def _confirmed_version(conn: sqlite3.Connection, *, operation_id: str, task_gid: str, identity: str):
    row = conn.execute(
        """SELECT * FROM content_versions
             WHERE operation_id=? AND task_gid=? AND identity=? AND confirmed=1
             ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (operation_id, task_gid, identity),
    ).fetchone()
    if row is None:
        raise DishRuleError(
            "CONFLICT", "confirmed content evidence is missing",
            rule="content_version_missing", details={"identity": identity},
        )
    return row


def _held_document(conn: sqlite3.Connection, *, cycle, live):
    if not cycle["hold_content_version_id"] or not cycle["hold_identity"] or not cycle["hold_section_gid"]:
        raise DishRuleError(
            "WRONG_STATE",
            "held operation requires migration reconciliation before it can resume",
            rule="hold_baseline_reconciliation_required",
            details={"cycle_id": cycle["cycle_id"]},
        )
    version = conn.execute(
        "SELECT * FROM content_versions WHERE content_version_id=?",
        (cycle["hold_content_version_id"],),
    ).fetchone()
    if (
        version is None
        or version["confirmed"] != 1
        or version["operation_id"] != cycle["operation_id"]
        or version["task_gid"] != cycle["task_gid"]
        or version["identity"] != cycle["hold_identity"]
    ):
        raise DishRuleError(
            "CONFLICT", "persisted hold baseline is inconsistent",
            rule="hold_baseline_invalid", details={"cycle_id": cycle["cycle_id"]},
        )
    if live.identity != cycle["hold_identity"]:
        raise DishRuleError(
            "CONFLICT", "live task content changed while the hold was open",
            rule="hold_content_drift",
            details={"expected_identity": cycle["hold_identity"], "actual_identity": live.identity},
        )
    if live.section_gid != cycle["hold_section_gid"]:
        raise DishRuleError(
            "CONFLICT", "live task placement changed while the hold was open",
            rule="hold_placement_drift",
            details={"expected_section_gid": cycle["hold_section_gid"], "actual_section_gid": live.section_gid},
        )
    return parse_task_document(f"{version['title']}\n{version['notes']}")


def _write_document(conn, backend, op, live, document, *, schema=None, authorization_ids=()):
    check = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not check.ok:
        raise DishRuleError("VALIDATION_FAILED", "candidate failed deterministic validation", errors=[finding_payload(f) for f in check.findings])
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


def approve_small(conn: sqlite3.Connection, backend: Any, *, operation_id: str, agent: str, model: str | None = None, file_path: str, reviewed_identity: str, semantic_review_complete: bool, provenance_complete: bool, run_id: str | None = None, independence_attestation: str | None = None, schema=None):
    op, cycle = _rows(conn, operation_id)
    assert_verifier_authority(cycle, agent=agent, run_id=run_id, independence_attestation=independence_attestation)
    if not semantic_review_complete or not provenance_complete:
        raise DishRuleError("VALIDATION_FAILED", "semantic self-review and provenance completion are required", rule="verification_inputs_incomplete")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    persisted_reviewed = cycle["reviewed_identity"]
    if not persisted_reviewed or not cycle["reviewed_content_version_id"]:
        raise DishRuleError("WRONG_STATE", "Verification cycle has no persisted reviewed content", rule="reviewed_content_missing")
    if reviewed_identity != persisted_reviewed:
        raise DishRuleError("CONFLICT", "caller review identity does not match the persisted review", rule="reviewed_identity_mismatch", retryable=True)
    if live.identity != persisted_reviewed:
        raise DishRuleError("CONFLICT", "live candidate changed after verifier review", rule="stale_verifier_review")
    corrected = _candidate(file_path)
    reviewed_document = parse_task_document(f"{live.title}\n{live.notes}")
    corrected_state = dict(corrected.state.values)
    corrected_state["Researched by"] = reviewed_document.state.values["Researched by"]
    corrected = dataclasses.replace(corrected, state=TaskState(corrected_state))
    corrected = preserve_material_change_history(reviewed_document, corrected)
    require_small_scope(reviewed_document, corrected)
    state = dict(corrected.state.values)
    state.update({"Status": "pending-verification", "Status detail": "None", "Resume status": "None", "Verified by": "None", "Verification protocol release": cycle["protocol_release"], "Self-verified": material_editor_line(agent, model, utc_now()[:10])})
    changes = tuple(corrected.material_changes) + (material_change_line(
        agent,
        model,
        utc_now()[:10],
        change="applied a small Verification correction",
        reason="exact candidate replaced and self-reviewed",
        materiality="Small",
    ),)
    corrected = dataclasses.replace(corrected, state=TaskState(state), material_changes=changes)
    precheck = validate_task_document(corrected, expected_schema_version=op["schema_version"], schema=schema)
    if not precheck.ok:
        raise DishRuleError("VALIDATION_FAILED", "candidate failed deterministic validation", errors=[finding_payload(f) for f in precheck.findings])
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
    bind_cycle_review(
        conn, cycle_id=cycle["cycle_id"], operation_id=operation_id,
        task_gid=op["task_gid"], identity=confirmed.identity, correction_class="small",
    )
    complete_operation_step(conn, operation_id, "small_review_binding")
    result = approve_live(conn, backend, operation_id=operation_id, agent=agent, model=model, reviewed_identity=confirmed.identity, semantic_review_complete=True, provenance_complete=True, correction_class="small", run_id=run_id, independence_attestation=independence_attestation, schema=schema)
    complete_operation_step(conn, operation_id, "small_signoff")
    return result


def _validate_rejection_route_arguments(
    *,
    route: str,
    model: str | None,
    file_path: str | None,
    resume_status: str | None,
    independence_attestation: str | None,
) -> None:
    permitted = {
        "large": [
            "submission_id", "agent", "reason", "route", "model", "file_path",
            "independence_attestation",
        ],
        "evidence": [
            "submission_id", "agent", "reason", "route", "resume_status",
        ],
        "human-review": [
            "submission_id", "agent", "reason", "route", "resume_status",
        ],
    }[route]
    errors: list[dict[str, Any]] = []

    def add(rule: str, field: str, message: str) -> None:
        errors.append({
            "rule": rule,
            "field": field,
            "message": message,
            "route": route,
            "permitted_arguments": permitted,
        })

    if route == "large":
        if resume_status is not None:
            add(
                "large_resume_status_unexpected", "resume_status",
                "Large correction sets pending-verification automatically",
            )
        if not str(model or "").strip():
            add("large_model_required", "model", "Large correction requires model")
        if not str(file_path or "").strip():
            add(
                "large_candidate_required", "file_path",
                "Large correction requires a complete corrected candidate",
            )
    else:
        if str(file_path or "").strip():
            add(
                "hold_candidate_unexpected", "file_path",
                "hold routes do not accept candidate content",
            )
        if str(model or "").strip():
            add("hold_model_unexpected", "model", "hold routes do not accept model")
        if str(independence_attestation or "").strip():
            add(
                "hold_independence_attestation_unexpected",
                "independence_attestation",
                "hold routes use the verifier run already bound by Verification start",
            )
        if resume_status not in {"pending-verification", "pending-research"}:
            add(
                "resume_status_required", "resume_status",
                "hold routes require pending-research or pending-verification",
            )

    if errors:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "arguments are incompatible with the selected rejection route",
            rule="rejection_route_arguments_invalid",
            retryable=True,
            details={"route": route, "permitted_arguments": permitted},
            errors=errors,
        )


def reject_route(conn: sqlite3.Connection, backend: Any, *, operation_id: str, agent: str, model: str | None = None, route: str, reason: str, file_path: str | None = None, resume_status: str | None = None, run_id: str | None = None, independence_attestation: str | None = None, request_id: str | None = None, schema=None, honest_root=None):

    route = str(route or "").strip()
    reason = str(reason or "").strip()
    if route not in ROUTES:
        raise DishRuleError("INVALID_ARGUMENT", "route must be large, evidence, or human-review", rule="invalid_rejection_route")
    if not reason:
        raise DishRuleError("INVALID_ARGUMENT", "route reason is required", rule="rejection_reason_required")
    _validate_rejection_route_arguments(
        route=route,
        model=model,
        file_path=file_path,
        resume_status=resume_status,
        independence_attestation=independence_attestation,
    )
    op = conn.execute(
        "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if op is None:
        raise DishRuleError(
            "NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found"
        )
    if (
        op["status"] == "open"
        and op["phase"] == "prepare_required"
        and op["operation_kind"] == "initial"
        and op["content_write_completed_at"] is None
    ):
        return _preconstruction_research_hold(
            conn,
            op=op,
            agent=agent,
            route=route,
            reason=reason,
            resume_status=resume_status,
            run_id=run_id,
            request_id=request_id,
            file_path=file_path,
            model=model,
        )
    op, cycle = _rows(conn, operation_id)
    authority_attestation = (
        independence_attestation
        if route == "large"
        else cycle["independence_attestation"]
    )
    assert_verifier_authority(
        cycle,
        agent=agent,
        run_id=run_id,
        independence_attestation=authority_attestation,
    )
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
        corrected = _candidate(file_path)
        corrected_state = dict(corrected.state.values)
        corrected_state["Researched by"] = document.state.values["Researched by"]
        corrected = dataclasses.replace(corrected, state=TaskState(corrected_state))
        corrected = preserve_material_change_history(document, corrected)
        if honest_root is None:
            raise DishRuleError("INTERNAL_ERROR", "current Honest checkout is required for a new Verification cycle", rule="honest_root_required")
        snapshot = current_verification_protocol_release(honest_root)
        assert_transition(action="large_correction", before="pending-verification", after="pending-verification")
        state = dict(pending_verification(corrected.state.values, protocol_release=snapshot.identity).values)
        state["Self-verified"] = material_editor_line(agent, model, utc_now()[:10])
        changes = tuple(corrected.material_changes) + (material_change_line(
            agent,
            model,
            utc_now()[:10],
            change="applied a large Verification correction",
            reason=reason,
            materiality="Large",
        ),)
        document = dataclasses.replace(corrected, state=TaskState(state), material_changes=changes)
    elif route == "evidence":
        assert_transition(action="request_evidence", before="pending-verification", after="pending-evidence")
        document = dataclasses.replace(document, state=hold(state, target="pending-evidence", detail=reason, resume_status=resume_status))
    else:
        assert_transition(action="request_human_review", before="pending-verification", after="pending-human-review")
        document = dataclasses.replace(document, state=hold(state, target="pending-human-review", detail=reason, resume_status=resume_status))

    completed = conn.execute("SELECT COUNT(*) FROM verification_cycles WHERE operation_id = ? AND completed_at IS NOT NULL AND outcome != 'approved'", (operation_id,)).fetchone()[0]
    two_pass = completed + 1 >= 2 and route == "large"
    if two_pass:
        assert_transition(action="two_pass_hold", before="pending-verification", after="pending-human-review")
        document = dataclasses.replace(document, state=hold(document.state.values, target="pending-human-review", detail=f"Two independent Verification passes ended without a signable task: {reason}", resume_status="pending-verification"))

    precheck = validate_task_document(document, expected_schema_version=op["schema_version"], schema=schema)
    if not precheck.ok:
        raise DishRuleError("VALIDATION_FAILED", "candidate failed deterministic validation", errors=[finding_payload(f) for f in precheck.findings])
    authorization_ids = require_governed_authorization(
        conn, parse_task_document(f"{live.title}\n{live.notes}"), document,
        task_gid=op["task_gid"], operation_id=operation_id,
    )
    intended_title, intended_notes = _render(document)
    outcome = "two-pass-hold" if two_pass else "rejected"
    target_phase = "held_human" if (two_pass or route == "human-review") else ("held_evidence" if route == "evidence" else "await_verification")
    route_suffix = cycle["cycle_id"]
    route_write_step = f"route_write:{route_suffix}"
    route_actor_step = f"route_actor:{route_suffix}"
    route_cycle_step = f"route_cycle_finalize:{route_suffix}"
    route_new_cycle_step = f"route_new_cycle:{route_suffix}"
    route_phase_step = f"route_phase:{route_suffix}"
    declare_operation_step(conn, operation_id, route_write_step, {"title": intended_title, "notes": intended_notes, "route": route})
    if route == "large":
        declare_operation_step(conn, operation_id, route_actor_step, {
            "role": "material_editor",
            "agent": agent,
            "run_id": cycle["run_id"],
            "independence_attestation": cycle["independence_attestation"],
            "candidate_identity": content_identity(intended_title, intended_notes).digest,
            "source_cycle_id": cycle["cycle_id"],
        })
    declare_operation_step(conn, operation_id, route_cycle_step, {
        "cycle_id": cycle["cycle_id"], "correction_class": "large" if route == "large" else None,
        "outcome": outcome, "route": {"evidence": "evidence", "human-review": "human_review"}.get(route),
        "resume_state": document.state.values["Resume status"],
        "hold_identity": content_identity(intended_title, intended_notes).digest if (two_pass or route in {"evidence", "human-review"}) else None,
        "hold_section_gid": live.section_gid if (two_pass or route in {"evidence", "human-review"}) else None,
    })
    if route == "large" and not two_pass:
        declare_operation_step(conn, operation_id, route_new_cycle_step, {"protocol_release": snapshot.identity, "protocol_text": snapshot.text})
    declare_operation_step(conn, operation_id, route_phase_step, {"phase": target_phase})
    confirmed = _write_document(conn, backend, op, live, document, schema=schema, authorization_ids=authorization_ids)
    complete_operation_step(conn, operation_id, route_write_step)
    if route == "large":
        record_actor_fact(
            conn, operation_id=operation_id, task_gid=op["task_gid"],
            role="material_editor", agent=agent, run_id=cycle["run_id"],
            independence_attestation=cycle["independence_attestation"],
            candidate_identity=confirmed.identity, source_cycle_id=cycle["cycle_id"],
        )
        complete_operation_step(conn, operation_id, route_actor_step)
        conn.execute(
            "UPDATE operations SET editor_agent=?, verifier_agent=NULL, run_id=?, independence_attestation=? WHERE operation_id=?",
            (agent, cycle["run_id"], cycle["independence_attestation"], operation_id),
        )
    if two_pass or route == "human-review":
        transition_operation(conn, operation_id, phase="held_human")
    elif route == "evidence":
        transition_operation(conn, operation_id, phase="held_evidence")
    hold_fields = (None, None, None)
    if two_pass or route in {"evidence", "human-review"}:
        hold_version = _confirmed_version(
            conn, operation_id=operation_id, task_gid=op["task_gid"], identity=confirmed.identity
        )
        hold_fields = (hold_version["content_version_id"], confirmed.identity, confirmed.section_gid)
    conn.execute(
        """UPDATE verification_cycles
              SET correction_class=?, outcome=?, route=?, resume_state=?, completed_at=?,
                  hold_content_version_id=?, hold_identity=?, hold_section_gid=?
            WHERE cycle_id=?""",
        (
            "large" if route == "large" else None,
            outcome,
            {"evidence": "evidence", "human-review": "human_review"}.get(route),
            document.state.values["Resume status"], utc_now(),
            hold_fields[0], hold_fields[1], hold_fields[2], cycle["cycle_id"],
        ),
    )
    complete_operation_step(conn, operation_id, route_cycle_step)
    if route == "large" and not two_pass:
        next_number = conn.execute("SELECT COALESCE(MAX(cycle_number), 0) + 1 FROM verification_cycles WHERE task_gid = ?", (op["task_gid"],)).fetchone()[0]
        new_cycle = create_verification_cycle(conn, operation_id=operation_id, task_gid=op["task_gid"], cycle_number=next_number, protocol_release=snapshot.identity, protocol_text=snapshot.text, route=None)
        complete_operation_step(conn, operation_id, route_new_cycle_step)
        transition_operation(conn, operation_id, phase="await_verification")
    else:
        new_cycle = None
    complete_operation_step(conn, operation_id, route_phase_step)
    record_audit(conn, submission_id=None, task_gid=op["task_gid"], operation_id=operation_id, event_type="verification.rejected", actor_agent=agent, details={"cycle_id": cycle["cycle_id"], "route": route, "reason": reason, "two_pass_hold": two_pass, "identity": confirmed.identity}, result_code="OK", result_ok=True, governed_kind="decision", before_state={"outcome": None, "reviewed_identity": cycle["reviewed_identity"], "status": "pending-verification"}, after_state={"outcome": outcome, "route": route, "resume_state": document.state.values["Resume status"], "status": document.state.values["Status"]}, actor_run_id=run_id, actor_attestation=authority_attestation)
    return {"operation_id": operation_id, "route": route, "two_pass_hold": two_pass, "new_cycle_id": None if new_cycle is None else new_cycle["cycle_id"], "task": dataclasses.asdict(confirmed)}




def _reset_path(document, category: str) -> dict[str, str]:
    if category == "method":
        return {"sections.HOW TO COOK IT": document.sections.get("HOW TO COOK IT", "")}
    if category == "evidence":
        return {"research_basis": "\n".join(document.research_basis)}
    if category == "premise":
        return {
            "recognition": document.recognition,
            "introduction": "\n".join(document.introduction),
            "sections.WHY COOK IT": document.sections.get("WHY COOK IT", ""),
            "planning.Purpose": document.planning_brief.values.get("Purpose", ""),
            "planning.Research emphasis": document.planning_brief.values.get("Research emphasis", ""),
            "decisions": "\n".join(document.decisions),
        }
    if category == "scope":
        return {
            "sections.QUANTITIES": document.sections.get("QUANTITIES", ""),
            "planning.Role": document.planning_brief.values.get("Role", ""),
            "planning.Purpose": document.planning_brief.values.get("Purpose", ""),
        }
    return {}



_RESET_QUANTITY_RE = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s*(kg|g|mg|l|ml|cl)\b", re.I)


def _reset_normal_form(value: str) -> str:
    from decimal import Decimal

    def canonical_quantity(match: re.Match[str]) -> str:
        amount = Decimal(match.group(1).replace(",", "."))
        unit = match.group(2).casefold()
        if unit == "kg":
            amount *= 1000
            unit = "g"
        elif unit == "mg":
            amount /= 1000
            unit = "g"
        elif unit == "l":
            amount *= 1000
            unit = "ml"
        elif unit == "cl":
            amount *= 10
            unit = "ml"
        text = format(amount.normalize(), "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return f"{text}{unit}"

    value = str(value).casefold().replace("\u00a0", " ")
    value = _RESET_QUANTITY_RE.sub(canonical_quantity, value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def _prove_reset(original, candidate, category: str, before: str, after: str) -> str:
    """Prove one operative replacement hunk at a category-owned canonical path."""
    before = str(before).strip()
    after = str(after).strip()
    before_norm = _reset_normal_form(before)
    after_norm = _reset_normal_form(after)
    old_paths = _reset_path(original, category)
    new_paths = _reset_path(candidate, category)
    matches: list[str] = []
    for path, old_value in old_paths.items():
        new_value = new_paths.get(path, "")
        if old_value == new_value:
            continue
        old_lines = [_reset_normal_form(line) for line in str(old_value).splitlines() if line.strip()]
        new_lines = [_reset_normal_form(line) for line in str(new_value).splitlines() if line.strip()]
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        path_matches = 0
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag not in {"replace", "delete", "insert"}:
                continue
            deleted = "\n".join(old_lines[i1:i2])
            inserted = "\n".join(new_lines[j1:j2])
            if (
                before_norm in deleted
                and after_norm in inserted
                and before_norm not in inserted
                and after_norm not in deleted
            ):
                path_matches += 1
        if path_matches == 1:
            matches.append(path)
    if len(matches) == 1:
        return matches[0]
    raise DishRuleError(
        "VALIDATION_FAILED",
        "corrected candidate must replace the declared operative value in one diff hunk",
        rule="two_pass_reset_not_applied",
        details={"category": category, "before": before, "after": after, "matching_paths": matches},
    )

def reopen_two_pass(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    category: str,
    before: str,
    after: str,
    editor: str,
    model: str,
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
    cycle = conn.execute(
        """SELECT * FROM verification_cycles
             WHERE operation_id=? AND outcome='two-pass-hold'
             ORDER BY cycle_number DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if cycle is None:
        raise DishRuleError("WRONG_STATE", "operation has no two-pass hold", rule="two_pass_hold_required")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    original = _held_document(conn, cycle=cycle, live=live)
    if original.state.values["Status"] != "pending-human-review" or original.state.values["Resume status"] != "pending-verification":
        raise DishRuleError("WRONG_STATE", "task is not on the two-pass Verification hold", rule="two_pass_hold_required")
    candidate = _candidate(file_path)
    candidate_state = dict(candidate.state.values)
    candidate_state["Researched by"] = original.state.values["Researched by"]
    candidate = dataclasses.replace(candidate, state=TaskState(candidate_state))
    candidate = preserve_material_change_history(original, candidate)
    changed_path = _prove_reset(original, candidate, category, before, after)
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
        "Self-verified": material_editor_line(editor, model, date),
    })
    entry = material_change_line(
        editor,
        model,
        date,
        change=f"reset {category} at {changed_path} from {before} to {after}",
        reason="Marco-authorized substantive reset after two unsuccessful passes",
        materiality="Large",
    )
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
            errors=[finding_payload(f) for f in check.findings],
        )
    authorization_ids = require_governed_authorization(
        conn, original, document, task_gid=op["task_gid"], operation_id=operation_id
    )
    intended_title, intended_notes = _render(document)
    intended_identity = content_identity(intended_title, intended_notes).digest
    declare_operation_step(conn, operation_id, "reopen_write", {"title": intended_title, "notes": intended_notes, "reset_path": changed_path})
    declare_operation_step(conn, operation_id, "reopen_reset", {
        "source_cycle_id": cycle["cycle_id"], "candidate_identity": intended_identity,
        "canonical_path": changed_path, "category": category,
        "before": before, "after": after,
    })
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
    conn.execute(
        """INSERT OR IGNORE INTO two_pass_resets(
               reset_id, operation_id, source_cycle_id, candidate_identity,
               canonical_path, category, before_json, after_json, created_at
           ) VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            str(uuid.uuid4()), operation_id, cycle["cycle_id"], confirmed.identity,
            changed_path, category, json.dumps(before), json.dumps(after), utc_now(),
        ),
    )
    complete_operation_step(conn, operation_id, "reopen_reset")
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
    model: str | None = None,
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
    preconstruction = conn.execute(
        """SELECT intended_json FROM operation_steps
             WHERE operation_id=? AND step_name='research_preconstruction_hold'
               AND completed_at IS NOT NULL""",
        (operation_id,),
    ).fetchone()
    if preconstruction is not None:
        hold_record = json.loads(preconstruction["intended_json"])
        if hold_record.get("route") != (
            "evidence" if resolution_kind == "evidence" else "human-review"
        ):
            raise DishRuleError(
                "WRONG_STATE",
                "hold resolution kind does not match the persisted Research hold",
                rule="hold_resolution_kind_mismatch",
            )
        if resume_status != "pending-research":
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "pre-construction Research holds must resume to pending-research",
                rule="preconstruction_resume_status_invalid",
                details={"expected": "pending-research", "actual": resume_status},
            )
        if file_path or editor or model:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "pre-construction Research hold resolution cannot install candidate content",
                rule="preconstruction_resolution_candidate_unexpected",
            )
        live = read_complete_task(
            backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID
        )
        if live.identity != op["expected_identity"] or live.section_gid != op["expected_section_gid"]:
            raise DishRuleError(
                "CONFLICT",
                "live task changed while Research was blocked before construction",
                rule="preconstruction_hold_baseline_drift",
                details={
                    "expected_identity": op["expected_identity"],
                    "actual_identity": live.identity,
                    "expected_section_gid": op["expected_section_gid"],
                    "actual_section_gid": live.section_gid,
                },
            )
        resolution = {
            "description": "Research block resolved before construction",
            "resolution_kind": resolution_kind,
            "detail": clean_detail,
            "resume_status": "pending-research",
            "candidate_content_existed": False,
        }
        declare_operation_step(
            conn, operation_id, "research_preconstruction_hold_resolution", resolution
        )
        transition_operation(conn, operation_id, phase="prepare_required")
        complete_operation_step(
            conn, operation_id, "research_preconstruction_hold_resolution"
        )
        record_audit(
            conn,
            submission_id=None,
            task_gid=op["task_gid"],
            operation_id=operation_id,
            event_type="research.preconstruction_resolved",
            actor_agent=editor if editor in {"gpt", "codex", "claude"} else None,
            actor_run_id=run_id,
            details=resolution,
            result_code="OK",
            result_ok=True,
            governed_kind="decision",
            before_state={"phase": expected_phase, "candidate_content_existed": False},
            after_state={"phase": "prepare_required", "resume_status": "pending-research"},
            actor_source="marco-hold-resolution",
        )
        return {
            "operation_id": operation_id,
            **resolution,
            "phase": "prepare_required",
            "task": dataclasses.asdict(live),
        }
    cycle = conn.execute(
        "SELECT * FROM verification_cycles WHERE operation_id=? AND route=? ORDER BY cycle_number DESC LIMIT 1",
        (operation_id, resolution_kind),
    ).fetchone()
    if cycle is None:
        raise DishRuleError("WRONG_STATE", "hold has no persisted Verification decision", rule="hold_cycle_missing")
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    before_doc = _held_document(conn, cycle=cycle, live=live)
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
        candidate = preserve_material_change_history(before_doc, candidate)
        snapshot = current_verification_protocol_release(honest_root)
        values = dict(candidate.state.values)
        values.update({
            "Status": resume_status,
            "Status detail": "None",
            "Resume status": "None",
            "Verified by": "None",
            "Verification protocol release": snapshot.identity if resume_status == "pending-verification" else "None",
            "Self-verified": material_editor_line(editor, model, utc_now()[:10]),
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
        raise DishRuleError("VALIDATION_FAILED", "candidate failed deterministic validation", errors=[finding_payload(f) for f in precheck.findings])
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
