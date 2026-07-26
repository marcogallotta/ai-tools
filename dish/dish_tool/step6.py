"""Step 6 guarded live prepare/check-in transactions."""
from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Any

from .constants import COOKING_PROJECT_GID
from .database import create_verification_cycle, transition_operation, declare_operation_step, complete_operation_step, record_actor_fact
from .errors import DishRuleError
from .models import ResolvedRelease, SectionRegistry, utc_now, material_editor_line
from .lifecycle import assert_transition, pending_verification
from .releases import current_verification_protocol_release
from .task_document import (
    finding_payload,
    DocumentParseError,
    PlanningBrief,
    TaskState,
    parse_planning_brief,
    parse_task_document,
    validate_planning_brief,
    validate_task_document,
)
from .task_store import LiveTask, move_exact, read_complete_task, write_exact_content
from .governed_diff import explicit_material_reasons, require_governed_authorization


def _candidate(path: str) -> str:
    clean = str(path or "").strip()
    if not clean:
        raise DishRuleError("INVALID_ARGUMENT", "candidate file is required", rule="candidate_file_required")
    try:
        return Path(clean).read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise DishRuleError("INVALID_ARGUMENT", f"candidate file not found: {clean}", rule="candidate_file_not_found") from exc
    except (OSError, UnicodeError) as exc:
        raise DishRuleError("INVALID_ARGUMENT", f"candidate file could not be read: {clean}", rule="candidate_file_unreadable") from exc


def _operation(conn: sqlite3.Connection, operation_id: str):
    row = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if row is None:
        raise DishRuleError("NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found")
    if row["status"] != "open":
        raise DishRuleError("WRONG_STATE", "operation is not open", rule="operation_not_open", details={"actual": row["status"]})
    if row["content_write_completed_at"] is not None:
        raise DishRuleError("CONFLICT", "prepare content write is already confirmed", rule="prepare_already_completed")
    return row


def _require_actor(row, agent: str) -> None:
    expected = row["researcher_agent"] if row["operation_kind"] == "initial" else row["editor_agent"]
    if agent != expected:
        raise DishRuleError("AGENT_MISMATCH", "prepare agent does not match the recorded operation actor", rule="operation_actor_mismatch", details={"expected": expected, "actual": agent})


def _render_document(document) -> tuple[str, str]:
    lines = document.render().splitlines()
    return lines[0], "\n".join(lines[1:]) + "\n"


def _body_changed(before, after) -> bool:
    return (
        before.title != after.title
        or before.recognition != after.recognition
        or before.introduction != after.introduction
        or dict(before.sections) != dict(after.sections)
        or before.planning_brief.values != after.planning_brief.values
        or before.decisions != after.decisions
        or before.research_basis != after.research_basis
    )


def prepare_live(
    conn: sqlite3.Connection,
    backend: Any,
    *,
    operation_id: str,
    agent: str,
    model: str,
    file_path: str,
    release: ResolvedRelease,
    material_classification: str | None = None,
) -> dict[str, Any]:
    op = _operation(conn, operation_id)
    _require_actor(op, agent)
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    if live.identity != op["expected_identity"]:
        raise DishRuleError("CONFLICT", "live task changed since start", rule="live_task_drift", details={"expected_identity": op["expected_identity"], "actual_identity": live.identity})
    expected_section_gid = op["expected_section_gid"] if "expected_section_gid" in op.keys() else None
    if live.section_gid != expected_section_gid:
        raise DishRuleError(
            "CONFLICT",
            "live task placement changed since start",
            rule="live_task_placement_drift",
            details={"expected_section_gid": expected_section_gid, "actual_section_gid": live.section_gid},
        )
    text = _candidate(file_path)
    registry = SectionRegistry.from_sections(backend.list_sections(COOKING_PROJECT_GID))

    if op["operation_kind"] == "planning":
        try:
            brief = parse_planning_brief(text)
        except DocumentParseError as exc:
            raise DishRuleError("VALIDATION_FAILED", "Planning candidate is malformed", rule=exc.rule) from exc
        findings = validate_planning_brief(brief).findings
        if findings:
            raise DishRuleError("VALIDATION_FAILED", "Planning candidate failed validation", errors=[finding_payload(f) for f in findings])
        notes = brief.render(heading=True).rstrip() + "\n"
        declare_operation_step(conn, operation_id, "planning_write", {"title": live.title, "notes": notes, "schema_version": release.schema_version})
        declare_operation_step(conn, operation_id, "planning_handoff", {"section_gid": registry.research_queue_gid})
        declare_operation_step(conn, operation_id, "planning_terminal", {"status": "completed", "phase": "terminal", "terminal_outcome": "planning_handoff_confirmed"})
        confirmed = write_exact_content(
            conn, backend, operation_id=operation_id, task_gid=live.gid,
            project_gid=COOKING_PROJECT_GID, expected_identity=live.identity,
            expected_section_gid=live.section_gid, title=live.title, notes=notes,
            schema_version=release.schema_version,
        )
        complete_operation_step(conn, operation_id, "planning_write")
        if confirmed.section_gid != registry.research_queue_gid:
            confirmed = move_exact(
                conn, backend, operation_id=operation_id, task_gid=live.gid,
                project_gid=COOKING_PROJECT_GID, expected_identity=confirmed.identity,
                expected_section_gid=confirmed.section_gid,
                intended_section_gid=registry.research_queue_gid, purpose="planning_handoff",
            )
        complete_operation_step(conn, operation_id, "planning_handoff")
        transition_operation(conn, operation_id, phase="terminal", status="completed", terminal_outcome="planning_handoff_confirmed")
        complete_operation_step(conn, operation_id, "planning_terminal")
        return {"operation_id": operation_id, "task": dataclasses.asdict(confirmed), "handoff": "planning-to-research", "validation_scope": "structural-only"}

    try:
        candidate = parse_task_document(text)
    except DocumentParseError as exc:
        raise DishRuleError("VALIDATION_FAILED", "candidate is not a canonical complete task", rule=exc.rule) from exc

    prior = None
    if live.notes:
        try:
            prior = parse_task_document(f"{live.title}\n{live.notes}")
        except DocumentParseError as exc:
            if op["operation_kind"] == "initial":
                try:
                    brief = parse_planning_brief(live.notes)
                except DocumentParseError:
                    raise DishRuleError("VALIDATION_FAILED", "live baseline is neither canonical nor a Planning brief", rule=exc.rule) from exc
                findings = validate_planning_brief(brief).findings
                if findings:
                    raise DishRuleError("VALIDATION_FAILED", "live Planning brief failed validation", errors=[finding_payload(f) for f in findings])
            else:
                raise DishRuleError("VALIDATION_FAILED", "live baseline is not canonical", rule=exc.rule) from exc

    if prior is not None and op["operation_kind"] == "change":
        candidate_state = dict(candidate.state.values)
        candidate_state["Researched by"] = prior.state.values["Researched by"]
        candidate = dataclasses.replace(candidate, state=TaskState(candidate_state))

    verification_snapshot = None
    material_changes = list(candidate.material_changes)
    body_changed = prior is not None and _body_changed(prior, candidate)

    if op["operation_kind"] == "initial":
        verification_snapshot = current_verification_protocol_release(release.root)
        before_status = None if prior is None else prior.state.values["Status"]
        assert_transition(action="research_handoff", before=before_status, after="pending-verification")
        state_values = dict(pending_verification(candidate.state.values, protocol_release=verification_snapshot.identity).values)
        actor_line = material_editor_line(agent, model, utc_now()[:10])
        state_values["Researched by"] = actor_line
        state_values["Self-verified"] = actor_line
        state = TaskState(state_values)
    elif op["operation_kind"] == "change" and body_changed:
        classification = str(material_classification or "").strip()
        if classification not in {"material", "non-material"}:
            raise DishRuleError("INVALID_ARGUMENT", "body edits require material or non-material classification", rule="material_classification_required")
        requested_classification = classification
        forced_reasons = explicit_material_reasons(prior, candidate)
        if classification == "non-material" and forced_reasons:
            classification = "material"
        material_changes.append(
            f"{utc_now()[:10]} — {agent}: requested {requested_classification}; enforced {classification}"
            + (f" ({', '.join(forced_reasons)})" if forced_reasons else "")
        )
        if classification == "material":
            verification_snapshot = current_verification_protocol_release(release.root)
            assert_transition(action="material_edit", before=prior.state.values["Status"], after="pending-verification")
            state_values = dict(pending_verification(candidate.state.values, protocol_release=verification_snapshot.identity).values)
            state_values["Self-verified"] = material_editor_line(agent, utc_now()[:10])
            state = TaskState(state_values)
        else:
            assert_transition(action="non_material_edit", before=prior.state.values["Status"], after="ready")
            # A non-material edit cannot smuggle a lifecycle rewrite. Preserve the
            # exact signed state block while recording the new confirmed version.
            state = prior.state
    elif prior is not None:
        state = prior.state
    else:
        state = candidate.state

    candidate = dataclasses.replace(candidate, state=state, material_changes=tuple(material_changes))
    validation = validate_task_document(candidate, expected_schema_version=release.schema_version, schema=release.schema)
    if not validation.ok:
        raise DishRuleError("VALIDATION_FAILED", "candidate failed current validation", errors=[finding_payload(f) for f in validation.findings])

    authorization_ids = ()
    if prior is not None and body_changed:
        authorization_ids = require_governed_authorization(
            conn, prior, candidate, task_gid=op["task_gid"], operation_id=operation_id
        )

    title, notes = _render_document(candidate)
    declare_operation_step(conn, operation_id, "candidate_write", {"title": title, "notes": notes, "schema_version": release.schema_version})
    if state.values["Status"] == "pending-verification":
        declare_operation_step(conn, operation_id, "verification_cycle", {"protocol_release": state.values["Verification protocol release"], "protocol_text": verification_snapshot.text})
        declare_operation_step(conn, operation_id, "verification_handoff", {"section_gid": registry.verification_queue_gid})
        declare_operation_step(conn, operation_id, "verification_phase", {"phase": "await_verification", "status": "open"})
    if op["operation_kind"] == "change" and state.values["Status"] != "pending-verification":
        approved = conn.execute("SELECT cycle_id FROM verification_cycles WHERE task_gid=? AND outcome='approved' ORDER BY completed_at DESC LIMIT 1", (live.gid,)).fetchone()
        declare_operation_step(conn, operation_id, "non_material_terminal", {
            "phase": "terminal", "status": "completed",
            "terminal_outcome": "non_material_checkin",
            "inherited_signoff_cycle_id": None if approved is None else approved["cycle_id"],
        })

    confirmed = write_exact_content(
        conn, backend, operation_id=operation_id, task_gid=live.gid,
        project_gid=COOKING_PROJECT_GID, expected_identity=live.identity,
        expected_section_gid=live.section_gid, title=title, notes=notes,
        schema_version=release.schema_version,
        context={"authorization_ids": list(authorization_ids)} if authorization_ids else None,
    )
    complete_operation_step(conn, operation_id, "candidate_write")
    if op["operation_kind"] == "initial" or (op["operation_kind"] == "change" and body_changed and state.values["Status"] == "pending-verification"):
        record_actor_fact(conn, operation_id=operation_id, task_gid=live.gid, role="constructor" if op["operation_kind"] == "initial" else "material_editor", agent=agent, run_id=op["run_id"], candidate_identity=confirmed.identity)
    exact = parse_task_document(f"{confirmed.title}\n{confirmed.notes}")
    check = validate_task_document(exact, expected_schema_version=release.schema_version, schema=release.schema)
    if not check.ok:
        raise DishRuleError("BACKEND_UNCERTAIN", "confirmed live candidate failed deterministic handoff validation", rule="handoff_validation_failed")

    cycle = None
    if exact.state.values["Status"] == "pending-verification":
        number = conn.execute("SELECT COALESCE(MAX(cycle_number), 0) + 1 FROM verification_cycles WHERE task_gid = ?", (live.gid,)).fetchone()[0]
        cycle = create_verification_cycle(conn, operation_id=operation_id, task_gid=live.gid, cycle_number=number, protocol_release=exact.state.values["Verification protocol release"], protocol_text=verification_snapshot.text)
        complete_operation_step(conn, operation_id, "verification_cycle")
        if confirmed.section_gid != registry.verification_queue_gid:
            confirmed = move_exact(
                conn, backend, operation_id=operation_id, task_gid=live.gid,
                project_gid=COOKING_PROJECT_GID, expected_identity=confirmed.identity,
                expected_section_gid=confirmed.section_gid,
                intended_section_gid=registry.verification_queue_gid, purpose="verification_handoff",
            )
        complete_operation_step(conn, operation_id, "verification_handoff")

    if cycle is not None:
        transition_operation(conn, operation_id, phase="await_verification")
        complete_operation_step(conn, operation_id, "verification_phase")
    elif op["operation_kind"] == "change":
        terminal_step = conn.execute("SELECT intended_json FROM operation_steps WHERE operation_id=? AND step_name='non_material_terminal'", (operation_id,)).fetchone()
        import json as _json
        intended = _json.loads(terminal_step["intended_json"])
        transition_operation(conn, operation_id, phase=intended["phase"], status=intended["status"], terminal_outcome=intended["terminal_outcome"], inherited_signoff_cycle_id=intended.get("inherited_signoff_cycle_id"))
        complete_operation_step(conn, operation_id, "non_material_terminal")

    return {
        "operation_id": operation_id,
        "task": dataclasses.asdict(confirmed),
        "verification_cycle": None if cycle is None else {k: cycle[k] for k in cycle.keys()},
        "handoff": "verification" if cycle is not None else "checked-in",
    }
