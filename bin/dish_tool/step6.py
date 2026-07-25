"""Step 6 guarded live prepare/check-in transactions."""
from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Any

from .constants import COOKING_PROJECT_GID
from .database import create_verification_cycle
from .errors import DishRuleError
from .models import ResolvedRelease, SectionRegistry, utc_now
from .releases import current_verification_protocol_release
from .task_document import (
    DocumentParseError,
    PlanningBrief,
    TaskState,
    parse_planning_brief,
    parse_task_document,
    validate_planning_brief,
    validate_task_document,
)
from .task_store import LiveTask, move_exact, read_complete_task, write_exact_content


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
    file_path: str,
    release: ResolvedRelease,
    material_classification: str | None = None,
) -> dict[str, Any]:
    op = _operation(conn, operation_id)
    _require_actor(op, agent)
    live = read_complete_task(backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    if live.identity != op["expected_identity"]:
        raise DishRuleError("CONFLICT", "live task changed since start", rule="live_task_drift", details={"expected_identity": op["expected_identity"], "actual_identity": live.identity})
    text = _candidate(file_path)
    registry = SectionRegistry.from_sections(backend.list_sections(COOKING_PROJECT_GID))

    if op["operation_kind"] == "planning":
        try:
            brief = parse_planning_brief(text)
        except DocumentParseError as exc:
            raise DishRuleError("VALIDATION_FAILED", "Planning candidate is malformed", rule=exc.rule) from exc
        findings = validate_planning_brief(brief).findings
        if findings:
            raise DishRuleError("VALIDATION_FAILED", "Planning candidate failed validation", errors=[{"rule": f.rule, "kind": f.kind.value} for f in findings])
        notes = brief.render(heading=True).rstrip() + "\n"
        confirmed = write_exact_content(
            conn, backend, operation_id=operation_id, task_gid=live.gid,
            project_gid=COOKING_PROJECT_GID, expected_identity=live.identity,
            expected_section_gid=live.section_gid, title=live.title, notes=notes,
            schema_version=release.schema_version,
        )
        if confirmed.section_gid != registry.research_queue_gid:
            confirmed = move_exact(
                conn, backend, operation_id=operation_id, task_gid=live.gid,
                project_gid=COOKING_PROJECT_GID, expected_identity=confirmed.identity,
                expected_section_gid=confirmed.section_gid,
                intended_section_gid=registry.research_queue_gid, purpose="planning_handoff",
            )
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
            raise DishRuleError("VALIDATION_FAILED", "live baseline is not canonical", rule=exc.rule) from exc

    state = dict(candidate.state.values)
    verification_snapshot = None
    if op["operation_kind"] in {"initial", "change"}:
        verification_snapshot = current_verification_protocol_release(release.root)
        state.update({
            "Status": "pending-verification",
            "Status detail": "None",
            "Resume status": "None",
            "Verification protocol release": verification_snapshot.identity,
            "Verified by": "None",
        })

    material_changes = list(candidate.material_changes)
    if op["operation_kind"] == "change" and prior is not None and _body_changed(prior, candidate):
        classification = str(material_classification or "").strip()
        if classification not in {"material", "non-material"}:
            raise DishRuleError("INVALID_ARGUMENT", "body edits require material or non-material classification", rule="material_classification_required")
        material_changes.append(f"{utc_now()[:10]} — {agent}: {classification}")
        if classification == "non-material":
            state["Verified by"] = prior.state.values["Verified by"]

    candidate = dataclasses.replace(candidate, state=TaskState(state), material_changes=tuple(material_changes))
    validation = validate_task_document(candidate, expected_schema_version=release.schema_version)
    if not validation.ok:
        raise DishRuleError("VALIDATION_FAILED", "candidate failed current validation", errors=[{"rule": f.rule, "kind": f.kind.value, "message": f.message, "location": f.location} for f in validation.findings])

    title, notes = _render_document(candidate)
    confirmed = write_exact_content(
        conn, backend, operation_id=operation_id, task_gid=live.gid,
        project_gid=COOKING_PROJECT_GID, expected_identity=live.identity,
        expected_section_gid=live.section_gid, title=title, notes=notes,
        schema_version=release.schema_version,
    )
    exact = parse_task_document(f"{confirmed.title}\n{confirmed.notes}")
    check = validate_task_document(exact, expected_schema_version=release.schema_version)
    if not check.ok:
        raise DishRuleError("BACKEND_UNCERTAIN", "confirmed live candidate failed deterministic handoff validation", rule="handoff_validation_failed")

    cycle = None
    if exact.state.values["Status"] == "pending-verification":
        number = conn.execute("SELECT COALESCE(MAX(cycle_number), 0) + 1 FROM verification_cycles WHERE task_gid = ?", (live.gid,)).fetchone()[0]
        cycle = create_verification_cycle(conn, operation_id=operation_id, task_gid=live.gid, cycle_number=number, protocol_release=exact.state.values["Verification protocol release"], protocol_text=verification_snapshot.text)
        if confirmed.section_gid != registry.verification_queue_gid:
            confirmed = move_exact(
                conn, backend, operation_id=operation_id, task_gid=live.gid,
                project_gid=COOKING_PROJECT_GID, expected_identity=confirmed.identity,
                expected_section_gid=confirmed.section_gid,
                intended_section_gid=registry.verification_queue_gid, purpose="verification_handoff",
            )

    return {
        "operation_id": operation_id,
        "task": dataclasses.asdict(confirmed),
        "verification_cycle": None if cycle is None else {k: cycle[k] for k in cycle.keys()},
        "handoff": "verification" if cycle is not None else "checked-in",
    }
