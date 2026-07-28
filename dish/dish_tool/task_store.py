"""Exact live-task transactions with drift detection and reread confirmation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .database import (
    content_identity,
    finalize_confirmed_movement_attempt,
    finalize_confirmed_write_attempt,
    finalize_not_applied_movement_attempt,
    finalize_not_applied_write_attempt,
    begin_planning_reopen_attempt,
    finish_planning_reopen_attempt,
    record_audit,
    atomic_persistence,
)
from .errors import BackendFailure, DishRuleError
from .recovery import (
    begin_movement_attempt,
    begin_operation_write_attempt,
    finish_movement_attempt,
    finish_operation_write_attempt,
)


class TaskBackend(Protocol):
    def read_task(self, task_gid: str) -> dict[str, Any]: ...
    def update_task_content(self, *, task_gid: str, title: str, notes: str) -> None: ...
    def update_task_completed(self, *, task_gid: str, completed: bool) -> None: ...
    def move_task_to_section(self, *, task_gid: str, section_gid: str) -> None: ...


@dataclass(frozen=True)
class LiveTask:
    gid: str
    title: str
    notes: str
    section_gid: str | None
    completed: bool
    modified_at: str | None

    @property
    def identity(self) -> str:
        return content_identity(self.title, self.notes).digest


def _gid(value: Any) -> str | None:
    if isinstance(value, Mapping):
        value = value.get("gid")
    clean = str(value or "").strip()
    return clean or None


def _section_gid(task: Mapping[str, Any], project_gid: str) -> str | None:
    memberships = task.get("memberships") or []
    if not isinstance(memberships, list):
        raise DishRuleError("VALIDATION_FAILED", "task memberships are malformed", rule="task_membership_malformed")
    matching_memberships = [
        item
        for item in memberships
        if isinstance(item, Mapping) and _gid(item.get("project")) == project_gid
    ]
    projects = task.get("projects") or []
    if not isinstance(projects, list):
        raise DishRuleError("VALIDATION_FAILED", "task projects are malformed", rule="task_projects_malformed")
    in_project = bool(matching_memberships) or any(_gid(project) == project_gid for project in projects)
    if not in_project:
        raise DishRuleError(
            "UNMANAGED_TASK",
            f"task {_gid(task.get('gid')) or '<unknown>'} is not in the Cooking project",
            rule="task_not_in_cooking",
        )
    matches = {
        gid
        for item in matching_memberships
        if (gid := _gid(item.get("section"))) is not None
    }
    if len(matches) > 1:
        raise DishRuleError("VALIDATION_FAILED", "task has ambiguous project placement", rule="task_membership_ambiguous")
    return next(iter(matches), None)


def read_complete_task(backend: TaskBackend, *, task_gid: str, project_gid: str) -> LiveTask:
    raw = backend.read_task(task_gid)
    gid = _gid(raw.get("gid"))
    if gid != task_gid:
        raise DishRuleError("INTERNAL_ERROR", "backend returned the wrong task", rule="backend_response_malformed")
    return LiveTask(
        gid=gid,
        title=str(raw.get("name") or ""),
        notes=str(raw.get("notes") or ""),
        section_gid=_section_gid(raw, project_gid),
        completed=bool(raw.get("completed")),
        modified_at=str(raw.get("modified_at") or "").strip() or None,
    )


def _assert_expected(live: LiveTask, *, expected_identity: str, expected_section_gid: str | None) -> None:
    if live.identity != expected_identity:
        raise DishRuleError(
            "CONFLICT",
            "live task content changed outside the guarded operation",
            rule="live_task_drift",
            details={"expected_identity": expected_identity, "actual_identity": live.identity},
        )
    if live.section_gid != expected_section_gid:
        raise DishRuleError(
            "CONFLICT",
            "live task placement changed outside the guarded operation",
            rule="live_task_placement_drift",
            details={"expected_section_gid": expected_section_gid, "actual_section_gid": live.section_gid},
        )


def assert_live_matches_confirmed(
    conn: sqlite3.Connection,
    backend: TaskBackend,
    *,
    task_gid: str,
    project_gid: str,
    expected_section_gid: str | None,
) -> LiveTask:
    row = conn.execute("SELECT last_confirmed_identity FROM task_content_state WHERE task_gid = ?", (task_gid,)).fetchone()
    if row is None:
        raise DishRuleError("CONFLICT", "task has no confirmed content baseline", rule="confirmed_content_missing")
    live = read_complete_task(backend, task_gid=task_gid, project_gid=project_gid)
    _assert_expected(live, expected_identity=row["last_confirmed_identity"], expected_section_gid=expected_section_gid)
    return live


def reopen_completed_task_for_planning(
    conn: sqlite3.Connection,
    backend: TaskBackend,
    *,
    task_gid: str,
    project_gid: str,
    reason: str,
    actor_run_id: str | None,
    request_id: str | None,
) -> tuple[LiveTask, str]:
    """Marco-authorized exact reopen of one completed bare task for Planning."""
    before = read_complete_task(backend, task_gid=task_gid, project_gid=project_gid)
    if not before.completed:
        raise DishRuleError(
            "WRONG_STATE", "task is not completed", rule="planning_reopen_task_not_completed",
        )
    attempt = begin_planning_reopen_attempt(
        conn,
        task_gid=task_gid,
        expected_identity=before.identity,
        expected_section_gid=before.section_gid,
        expected_modified_at=before.modified_at,
        reason=reason,
        actor_run_id=actor_run_id,
        request_id=request_id,
    )
    backend_error: BackendFailure | None = None
    try:
        backend.update_task_completed(task_gid=task_gid, completed=False)
    except BackendFailure as exc:
        backend_error = exc

    try:
        after = read_complete_task(backend, task_gid=task_gid, project_gid=project_gid)
    except Exception as exc:
        finish_planning_reopen_attempt(
            conn, attempt_id=attempt["attempt_id"], outcome="uncertain"
        )
        raise BackendFailure(
            "BACKEND_UNCERTAIN",
            "planning reopen outcome could not be confirmed by reread",
            retryable=False,
            details={"attempt_id": attempt["attempt_id"], "task_gid": task_gid},
        ) from exc

    if after.identity != before.identity or after.section_gid != before.section_gid:
        finish_planning_reopen_attempt(
            conn, attempt_id=attempt["attempt_id"], outcome="uncertain",
            confirmed_modified_at=after.modified_at,
        )
        raise BackendFailure(
            "BACKEND_UNCERTAIN",
            "planning reopen coincided with unexpected task content or placement drift",
            retryable=False,
            details={
                "attempt_id": attempt["attempt_id"],
                "task_gid": task_gid,
                "expected_identity": before.identity,
                "actual_identity": after.identity,
                "expected_section_gid": before.section_gid,
                "actual_section_gid": after.section_gid,
            },
        )
    if not after.completed:
        with atomic_persistence(conn, "planning_reopen_confirmed"):
            finish_planning_reopen_attempt(
                conn, attempt_id=attempt["attempt_id"], outcome="confirmed",
                confirmed_modified_at=after.modified_at,
            )
            record_audit(
                conn, submission_id=None, task_gid=task_gid, operation_id=None,
                event_type="planning.task_reopened", actor_agent=None,
                actor_run_id=actor_run_id, actor_source="marco-admin",
                details={
                    "attempt_id": attempt["attempt_id"],
                    "reason": reason,
                    "expected_identity": before.identity,
                    "section_gid": before.section_gid,
                    "completed_before": True,
                    "completed_after": False,
                },
                result_code="OK", result_ok=True,
            )
        return after, attempt["attempt_id"]

    finish_planning_reopen_attempt(
        conn, attempt_id=attempt["attempt_id"], outcome="not_applied",
        confirmed_modified_at=after.modified_at,
    )
    if backend_error is not None:
        raise BackendFailure(
            "BACKEND_REJECTED", str(backend_error), rule=backend_error.rule,
            status=backend_error.status, phase=backend_error.phase,
            retryable=backend_error.retryable,
            details={"attempt_id": attempt["attempt_id"], "task_gid": task_gid},
        )
    raise BackendFailure(
        "BACKEND_REJECTED", "task completion state was not reopened",
        retryable=True,
        details={"attempt_id": attempt["attempt_id"], "task_gid": task_gid},
    )


def write_exact_content(
    conn: sqlite3.Connection,
    backend: TaskBackend,
    *,
    operation_id: str,
    task_gid: str,
    project_gid: str,
    expected_identity: str,
    expected_section_gid: str | None,
    title: str,
    notes: str,
    schema_version: str,
    purpose: str = "content_write",
    context: dict[str, object] | None = None,
) -> LiveTask:
    before = read_complete_task(backend, task_gid=task_gid, project_gid=project_gid)
    _assert_expected(before, expected_identity=expected_identity, expected_section_gid=expected_section_gid)
    intended = content_identity(title, notes)
    attempt_id = begin_operation_write_attempt(
        conn, operation_id=operation_id, expected_identity=expected_identity, intended_identity=intended.digest,
        intended_title=intended.title, intended_notes=intended.notes, schema_version=schema_version,
        purpose=purpose, context=context,
    )
    backend_error: BackendFailure | None = None
    try:
        backend.update_task_content(task_gid=task_gid, title=title, notes=notes)
    except BackendFailure as exc:
        backend_error = exc

    try:
        after = read_complete_task(backend, task_gid=task_gid, project_gid=project_gid)
    except Exception as exc:
        finish_operation_write_attempt(conn, attempt_id=attempt_id, outcome="uncertain")
        raise BackendFailure("BACKEND_UNCERTAIN", "content write outcome could not be confirmed by reread", retryable=False) from exc

    if after.identity == intended.digest and after.section_gid == expected_section_gid:
        finalize_confirmed_write_attempt(
            conn, attempt_id=attempt_id, task_gid=task_gid, title=after.title, notes=after.notes,
            schema_version=schema_version,
        )
        return after

    if after.identity == before.identity and after.section_gid == before.section_gid:
        finalize_not_applied_write_attempt(conn, attempt_id=attempt_id)
        if backend_error is not None:
            raise BackendFailure(
                "BACKEND_REJECTED",
                str(backend_error),
                rule=backend_error.rule,
                status=backend_error.status,
                phase=backend_error.phase,
                retryable=backend_error.retryable,
            )
        raise BackendFailure("BACKEND_REJECTED", "content write was not applied", retryable=True)

    finish_operation_write_attempt(conn, attempt_id=attempt_id, outcome="uncertain")
    raise BackendFailure(
        "BACKEND_UNCERTAIN",
        "content write produced an unexpected live task state",
        retryable=False,
        details={"expected_identity": intended.digest, "actual_identity": after.identity},
    )


def rewrite_state_exact(
    conn: sqlite3.Connection,
    backend: TaskBackend,
    **kwargs: Any,
) -> LiveTask:
    """State rewrites are complete title-and-notes replacements, never partial patches."""
    return write_exact_content(conn, backend, **kwargs)


def move_exact(
    conn: sqlite3.Connection,
    backend: TaskBackend,
    *,
    operation_id: str,
    task_gid: str,
    project_gid: str,
    expected_identity: str,
    expected_section_gid: str | None,
    intended_section_gid: str,
    purpose: str = "unspecified",
) -> LiveTask:
    before = read_complete_task(backend, task_gid=task_gid, project_gid=project_gid)
    _assert_expected(before, expected_identity=expected_identity, expected_section_gid=expected_section_gid)
    attempt_id = begin_movement_attempt(
        conn, operation_id=operation_id, expected_section_gid=expected_section_gid,
        intended_section_gid=intended_section_gid, purpose=purpose,
    )
    backend_error: BackendFailure | None = None
    try:
        backend.move_task_to_section(task_gid=task_gid, section_gid=intended_section_gid)
    except BackendFailure as exc:
        backend_error = exc

    try:
        after = read_complete_task(backend, task_gid=task_gid, project_gid=project_gid)
    except Exception as exc:
        finish_movement_attempt(conn, attempt_id=attempt_id, outcome="uncertain")
        raise BackendFailure("BACKEND_UNCERTAIN", "movement outcome could not be confirmed by reread", retryable=False) from exc

    if after.identity != before.identity:
        finish_movement_attempt(conn, attempt_id=attempt_id, outcome="uncertain")
        raise BackendFailure("BACKEND_UNCERTAIN", "movement unexpectedly changed task content", retryable=False)
    if after.section_gid == intended_section_gid:
        finalize_confirmed_movement_attempt(conn, attempt_id=attempt_id, live_section_gid=after.section_gid)
        return after
    if after.section_gid == expected_section_gid:
        finalize_not_applied_movement_attempt(conn, attempt_id=attempt_id)
        if backend_error is not None:
            raise BackendFailure(
                "BACKEND_REJECTED",
                str(backend_error),
                rule=backend_error.rule,
                status=backend_error.status,
                phase=backend_error.phase,
                retryable=backend_error.retryable,
            )
        raise BackendFailure("BACKEND_REJECTED", "movement was not applied", retryable=True)
    finish_movement_attempt(conn, attempt_id=attempt_id, outcome="uncertain")
    raise BackendFailure("BACKEND_UNCERTAIN", "movement produced an unexpected placement", retryable=False)
