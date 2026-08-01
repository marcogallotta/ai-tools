"""Exact live-task transactions with drift detection and reread confirmation."""

from __future__ import annotations

import shlex
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
)
from .transactions import immediate_transaction
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
    matching_memberships = []
    for item in memberships:
        if not isinstance(item, Mapping):
            raise DishRuleError(
                "VALIDATION_FAILED",
                "task membership entry is malformed",
                rule="task_membership_malformed",
            )
        project = item.get("project")
        if not isinstance(project, Mapping):
            raise DishRuleError(
                "VALIDATION_FAILED",
                "task membership project is malformed",
                rule="task_membership_malformed",
            )
        membership_project_gid = _gid(project)
        if membership_project_gid is None:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "task membership project is malformed",
                rule="task_membership_malformed",
            )
        section = item.get("section")
        if section is not None and not isinstance(section, Mapping):
            raise DishRuleError(
                "VALIDATION_FAILED",
                "task membership section is malformed",
                rule="task_membership_malformed",
            )
        if section is not None and _gid(section) is None:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "task membership section is malformed",
                rule="task_membership_malformed",
            )
        if membership_project_gid == project_gid:
            matching_memberships.append(item)
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


def _attempt_value(attempt: Mapping[str, Any], key: str) -> Any:
    try:
        return attempt[key]
    except (KeyError, IndexError):
        return None

def _planning_reopen_attempt(
    conn: sqlite3.Connection, *, attempt_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM planning_reopen_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if row is None:
        raise DishRuleError(
            "NOT_FOUND", "planning reopen attempt was not found",
            rule="planning_reopen_attempt_not_found",
            details={"attempt_id": attempt_id},
        )
    return row


def planning_reopen_recovery_details(
    attempt: Mapping[str, Any], *, safe_to_resume: bool = False
) -> dict[str, Any]:
    request_id = str(_attempt_value(attempt, "request_id") or "").strip() or None
    task_gid = str(attempt["task_gid"])
    reason = str(attempt["reason"])
    request_status = (
        str(_attempt_value(attempt, "request_status") or "").strip() or None
    )
    replay_available = request_id is not None and request_status in {None, "pending"}
    details = {
        "attempt_id": attempt["attempt_id"],
        "task_gid": task_gid,
        "original_request_id": request_id,
        "original_request_status": request_status,
        "reopen_reason": reason,
        "attempt_outcome": attempt["outcome"],
        "safe_to_resume": bool(safe_to_resume),
        "replay_original_request": replay_available,
    }
    if not replay_available:
        if request_id is None:
            conflict = "original service request identity is unavailable"
        else:
            conflict = (
                "original service request journal is already terminal while the "
                "Planning reopen attempt remains unresolved"
            )
        details.update({
            "required_admin_action": "manual-reconciliation",
            "resolver": (
                "Marco must authorize recovery of the Planning reopen attempt; "
                "exact request replay is unavailable"
            ),
            "admin_command": None,
            "authority_conflict": conflict,
        })
        return details
    replay_command = (
        "dish-admin reopen-planning "
        f"{shlex.quote(task_gid)} --reason {shlex.quote(reason)} "
        f"--request-id {shlex.quote(request_id)}"
    )
    details.update({
        "required_admin_action": "reopen-planning",
        "resolver": "Marco/admin replay the original reopen-planning request",
        "admin_command": replay_command,
        "directive": (
            f"Tell the human to run: {replay_command}\n"
            "Then wait for confirmation it succeeded before continuing — do not create a "
            "replacement operation; retry the original request once it succeeds."
        ),
    })
    return details


def planning_reopen_success_data(
    attempt: Mapping[str, Any], *, live: LiveTask | None = None
) -> dict[str, Any]:
    return {
        "attempt_id": attempt["attempt_id"],
        "reason": attempt["reason"],
        "task": {
            "gid": attempt["task_gid"],
            "completed": False,
            "identity": live.identity if live is not None else attempt["expected_identity"],
            "section_gid": (
                live.section_gid if live is not None else attempt["expected_section_gid"]
            ),
        },
        "required_start_kind": "planning",
    }


def _planning_reopen_live_matches(
    attempt: Mapping[str, Any], live: LiveTask
) -> bool:
    return (
        live.identity == attempt["expected_identity"]
        and live.section_gid == attempt["expected_section_gid"]
    )


def _planning_reopen_not_applied_is_proven(
    attempt: Mapping[str, Any], live: LiveTask
) -> bool:
    expected_modified_at = str(
        _attempt_value(attempt, "expected_modified_at") or ""
    ).strip()
    return bool(
        live.completed
        and _planning_reopen_live_matches(attempt, live)
        and expected_modified_at
        and live.modified_at == expected_modified_at
    )


def _mark_planning_reopen_uncertain(
    conn: sqlite3.Connection, *, attempt: Mapping[str, Any], live: LiveTask | None = None
) -> sqlite3.Row:
    if attempt["outcome"] == "uncertain":
        return _planning_reopen_attempt(conn, attempt_id=attempt["attempt_id"])
    return finish_planning_reopen_attempt(
        conn,
        attempt_id=attempt["attempt_id"],
        outcome="uncertain",
        confirmed_modified_at=None if live is None else live.modified_at,
    )


def _confirm_planning_reopen(
    conn: sqlite3.Connection,
    *,
    attempt: Mapping[str, Any],
    live: LiveTask | None,
    recovered_by: str | None = None,
) -> sqlite3.Row:
    with immediate_transaction(conn, "planning_reopen_confirmed"):
        finished = finish_planning_reopen_attempt(
            conn,
            attempt_id=attempt["attempt_id"],
            outcome="confirmed",
            confirmed_modified_at=(
                live.modified_at if live is not None else _attempt_value(attempt, "confirmed_modified_at")
            ),
        )
        existing = conn.execute(
            """SELECT event_id FROM audit_events
                 WHERE event_type='planning.task_reopened'
                   AND json_extract(details, '$.attempt_id')=?
                 LIMIT 1""",
            (attempt["attempt_id"],),
        ).fetchone()
        if existing is None:
            details = {
                "attempt_id": attempt["attempt_id"],
                "reason": attempt["reason"],
                "expected_identity": attempt["expected_identity"],
                "section_gid": attempt["expected_section_gid"],
                "completed_before": True,
                "completed_after": False,
            }
            if recovered_by:
                details["recovered_by"] = recovered_by
            record_audit(
                conn, submission_id=None, task_gid=attempt["task_gid"],
                operation_id=None, event_type="planning.task_reopened",
                actor_agent=None, actor_run_id=attempt["actor_run_id"],
                actor_source="marco-admin", details=details,
                result_code="OK", result_ok=True,
            )
    return finished


def _planning_reopen_uncertain_error(
    attempt: Mapping[str, Any],
    message: str,
    *,
    live: LiveTask | None = None,
) -> BackendFailure:
    details = planning_reopen_recovery_details(attempt)
    details.update({
        "expected_identity": attempt["expected_identity"],
        "expected_section_gid": attempt["expected_section_gid"],
        "expected_modified_at": attempt["expected_modified_at"],
    })
    if live is not None:
        details.update({
            "actual_identity": live.identity,
            "actual_section_gid": live.section_gid,
            "actual_completed": live.completed,
            "actual_modified_at": live.modified_at,
        })
    return BackendFailure(
        "BACKEND_UNCERTAIN", message,
        rule="planning_reopen_outcome_uncertain", retryable=False, details=details,
    )


def _finish_planning_reopen_after_effect(
    conn: sqlite3.Connection,
    *,
    attempt: Mapping[str, Any],
    after: LiveTask,
    backend_error: BackendFailure | None,
    recovered_by: str | None = None,
) -> tuple[LiveTask, sqlite3.Row]:
    if not _planning_reopen_live_matches(attempt, after):
        uncertain = _mark_planning_reopen_uncertain(
            conn, attempt=attempt, live=after
        )
        raise _planning_reopen_uncertain_error(
            uncertain,
            "planning reopen coincided with unexpected task content or placement drift",
            live=after,
        )
    if not after.completed:
        finished = _confirm_planning_reopen(
            conn, attempt=attempt, live=after, recovered_by=recovered_by
        )
        return after, finished

    finished = finish_planning_reopen_attempt(
        conn, attempt_id=attempt["attempt_id"], outcome="not_applied",
        confirmed_modified_at=after.modified_at,
    )
    if backend_error is not None:
        raise BackendFailure(
            "BACKEND_REJECTED", str(backend_error), rule=backend_error.rule,
            status=backend_error.status, phase=backend_error.phase,
            retryable=backend_error.retryable,
            details={
                "attempt_id": finished["attempt_id"],
                "task_gid": finished["task_gid"],
            },
        )
    raise BackendFailure(
        "BACKEND_REJECTED", "task completion state was not reopened",
        rule="planning_reopen_not_applied", retryable=True,
        details={
            "attempt_id": finished["attempt_id"],
            "task_gid": finished["task_gid"],
        },
    )


def _apply_planning_reopen_effect(
    conn: sqlite3.Connection,
    backend: TaskBackend,
    *,
    attempt: Mapping[str, Any],
    project_gid: str,
    recovered_by: str | None = None,
) -> tuple[LiveTask, sqlite3.Row]:
    backend_error: BackendFailure | None = None
    try:
        backend.update_task_completed(task_gid=attempt["task_gid"], completed=False)
    except BackendFailure as exc:
        backend_error = exc

    try:
        after = read_complete_task(
            backend, task_gid=attempt["task_gid"], project_gid=project_gid
        )
    except Exception as exc:
        uncertain = _mark_planning_reopen_uncertain(conn, attempt=attempt)
        raise _planning_reopen_uncertain_error(
            uncertain, "planning reopen outcome could not be confirmed by reread"
        ) from exc
    return _finish_planning_reopen_after_effect(
        conn, attempt=attempt, after=after, backend_error=backend_error,
        recovered_by=recovered_by,
    )


def _reconcile_planning_reopen_attempt(
    conn: sqlite3.Connection,
    backend: TaskBackend,
    *,
    attempt_id: str,
    project_gid: str,
    allow_external_retry: bool,
    recovered_by: str,
    finalize_observed_applied: bool,
) -> dict[str, Any]:
    attempt = _planning_reopen_attempt(conn, attempt_id=attempt_id)
    if attempt["outcome"] == "confirmed":
        finished = _confirm_planning_reopen(
            conn, attempt=attempt, live=None, recovered_by=recovered_by
        )
        return {
            "state": "confirmed", "attempt": finished, "live": None,
            "external_update_attempted": False,
        }
    if attempt["outcome"] == "not_applied":
        return {
            "state": "not_applied", "attempt": attempt, "live": None,
            "external_update_attempted": False,
        }

    try:
        live = read_complete_task(
            backend, task_gid=attempt["task_gid"], project_gid=project_gid
        )
    except Exception as exc:
        uncertain = _mark_planning_reopen_uncertain(conn, attempt=attempt)
        raise _planning_reopen_uncertain_error(
            uncertain, "planning reopen outcome could not be reconciled by reread"
        ) from exc

    if not _planning_reopen_live_matches(attempt, live):
        uncertain = _mark_planning_reopen_uncertain(
            conn, attempt=attempt, live=live
        )
        raise _planning_reopen_uncertain_error(
            uncertain,
            "live task identity or placement no longer proves the Planning reopen outcome",
            live=live,
        )
    if not live.completed:
        if not finalize_observed_applied:
            return {
                "state": "applied_pending_replay",
                "attempt": attempt,
                "live": live,
                "external_update_attempted": False,
            }
        finished = _confirm_planning_reopen(
            conn, attempt=attempt, live=live, recovered_by=recovered_by
        )
        return {
            "state": "confirmed", "attempt": finished, "live": live,
            "external_update_attempted": False,
        }
    if not _planning_reopen_not_applied_is_proven(attempt, live):
        uncertain = _mark_planning_reopen_uncertain(
            conn, attempt=attempt, live=live
        )
        raise _planning_reopen_uncertain_error(
            uncertain,
            "live completion state does not prove whether the Planning reopen applied",
            live=live,
        )
    if not allow_external_retry:
        return {
            "state": "resume_safe", "attempt": attempt, "live": live,
            "external_update_attempted": False,
        }
    after, finished = _apply_planning_reopen_effect(
        conn, backend, attempt=attempt, project_gid=project_gid,
        recovered_by=recovered_by,
    )
    return {
        "state": "confirmed", "attempt": finished, "live": after,
        "external_update_attempted": True,
    }


def reconcile_planning_reopen_attempt(
    conn: sqlite3.Connection,
    backend: TaskBackend,
    *,
    attempt_id: str,
    project_gid: str,
    allow_external_retry: bool,
    recovered_by: str,
    finalize_observed_applied: bool = True,
) -> dict[str, Any]:
    """Reconcile one exact reopen attempt without blindly repeating its effect.

    Exact replay holds the SQLite writer boundary from the authoritative reread
    through any external reopen and terminal evidence. Concurrent replays
    therefore re-read the attempt only after the winner has committed, rather
    than both issuing the same external update. A process death rolls back the
    local transaction; the next replay reconciles the live task before acting.
    """
    if allow_external_retry:
        with immediate_transaction(conn, "planning_reopen_reconciliation"):
            return _reconcile_planning_reopen_attempt(
                conn,
                backend,
                attempt_id=attempt_id,
                project_gid=project_gid,
                allow_external_retry=True,
                recovered_by=recovered_by,
                finalize_observed_applied=finalize_observed_applied,
            )
    return _reconcile_planning_reopen_attempt(
        conn,
        backend,
        attempt_id=attempt_id,
        project_gid=project_gid,
        allow_external_retry=False,
        recovered_by=recovered_by,
        finalize_observed_applied=finalize_observed_applied,
    )


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
    after, _finished = _apply_planning_reopen_effect(
        conn, backend, attempt=attempt, project_gid=project_gid
    )
    return after, attempt["attempt_id"]


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
