"""Marco-only lifecycle commands for the separate ``dish-admin`` surface."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .database import get_submission, record_audit, transition_submission
from .application_service import OperationApplicationService
from .constants import LEGACY_WORKFLOW_NAME
from .errors import DishRuleError
from .models import ProcessIdentity, utc_now
from .recovery import (
    process_identity_is_live,
    recover_write_attempt,
    validate_recovery_window,
)
from .results import error_envelope, result_envelope, label_unsupported_legacy_workflow

_RECOVERABLE_STATES = {"in_flight", "uncertain"}
_DISCARDABLE_STATES = {
    "drafting",
    "research_handoff",
    "awaiting_verification",
    "awaiting_human",
    "ready",
    "written",
}
_RECOVERY_TARGETS = {"not-applied": "ready", "applied": "written"}


@dataclass
class AdminTrace:
    task_gid: str | None = None
    submission_id: str | None = None
    state: str | None = None
    known_submission: bool = False
    audit_details: dict[str, Any] = field(default_factory=dict)


def _clean_required(value: Any, *, rule: str, label: str) -> str:
    clean = str(value or "").strip()
    if not clean:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            f"{label} is required",
            rule=rule,
        )
    return clean


def _utc_datetime_now() -> datetime:
    return datetime.now(timezone.utc)


class DishAdminApplication:
    """Admin dispatcher with one local audit event per invocation."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        now_provider: Callable[[], datetime] | None = None,
        process_liveness_checker: Callable[[ProcessIdentity], bool] | None = None,
        backend: Any | None = None,
        release_loader: Callable[[], Any] | None = None,
    ) -> None:
        self.conn = conn
        self.now_provider = now_provider or _utc_datetime_now
        self.process_liveness_checker = (process_liveness_checker or process_identity_is_live)
        self.backend = backend
        self.release_loader = release_loader
        self.operation_service = None if backend is None else OperationApplicationService(conn, backend)

    def execute(self, command: str, **arguments: Any) -> dict[str, Any]:
        trace = AdminTrace(submission_id=arguments.get("submission_id"))
        handler = getattr(self, f"_command_{command.replace(chr(45), chr(95))}", None)
        try:
            if trace.submission_id:
                legacy = self.conn.execute("SELECT status, protocol_release FROM submissions WHERE submission_id=?", (trace.submission_id,)).fetchone()
                if legacy is not None and legacy["protocol_release"] == LEGACY_WORKFLOW_NAME:
                    result = result_envelope(command=command, ok=False, code="PROTOCOL_INCOMPATIBLE", submission_id=trace.submission_id, state=legacy["status"], retryable=False, allowed_actions=[], data={})
                    result = label_unsupported_legacy_workflow(result)
                    self._record_invocation(command, trace, result)
                    return result
            if handler is None:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    f"unknown dish-admin command: {command}",
                    rule="invalid_command",
                )
            result = handler(trace=trace, **arguments)
        except DishRuleError as exc:
            if exc.code == "WRONG_STATE" and exc.details.get("actual"):
                trace.state = str(exc.details["actual"])
            result = error_envelope(
                command,
                exc,
                task_gid=trace.task_gid,
                submission_id=trace.submission_id,
                state=trace.state,
            )
        except Exception:
            error = DishRuleError(
                "INTERNAL_ERROR",
                "unexpected internal failure",
                rule="unexpected_internal_failure",
            )
            result = error_envelope(
                command,
                error,
                task_gid=trace.task_gid,
                submission_id=trace.submission_id,
                state=trace.state,
            )
        self._record_invocation(command, trace, result)
        return result

    def record_argument_failure(
        self,
        command: str,
        error: DishRuleError,
        *,
        submission_id: str | None = None,
    ) -> dict[str, Any]:
        trace = AdminTrace(submission_id=submission_id)
        if submission_id:
            try:
                row = get_submission(self.conn, submission_id)
            except DishRuleError:
                pass
            else:
                self._attach_submission(trace, row)
        legacy = self.conn.execute("SELECT status, protocol_release FROM submissions WHERE submission_id=?", (submission_id,)).fetchone() if submission_id else None
        if legacy is not None and legacy["protocol_release"] == LEGACY_WORKFLOW_NAME:
            result = result_envelope(command=command, ok=False, code="PROTOCOL_INCOMPATIBLE", task_gid=trace.task_gid, submission_id=trace.submission_id, state=legacy["status"], retryable=False, allowed_actions=[], data={})
            result = label_unsupported_legacy_workflow(result)
        else:
            result = error_envelope(
                command,
                error,
                task_gid=trace.task_gid,
                submission_id=trace.submission_id,
                state=trace.state,
            )
        self._record_invocation(command, trace, result)
        return result

    def _record_invocation(
        self,
        command: str,
        trace: AdminTrace,
        result: Mapping[str, Any],
    ) -> None:
        details = {
            "command": command,
            "actor_role": "marco",
            "ok": bool(result["ok"]),
            "code": result["code"],
            "state": result["state"],
            "retryable": bool(result["retryable"]),
            "errors": list(result["errors"]),
        }
        message = result.get("data", {}).get("message")
        if message:
            details["message"] = message
        details.update(trace.audit_details)
        record_audit(
            self.conn,
            submission_id=trace.submission_id if trace.known_submission else None,
            task_gid=trace.task_gid,
            event_type=f"dish-admin.{command}",
            actor_agent=None,
            details=details,
        )

    @staticmethod
    def _attach_submission(trace: AdminTrace, row: sqlite3.Row) -> None:
        trace.submission_id = row["submission_id"]
        trace.known_submission = True
        trace.task_gid = row["task_gid"]
        trace.state = row["status"]

    def _load_submission(
        self, *, trace: AdminTrace, submission_id: str
    ) -> sqlite3.Row:
        clean_submission_id = _clean_required(
            submission_id,
            rule="submission_id_required",
            label="submission ID",
        )
        row = get_submission(self.conn, clean_submission_id)
        self._attach_submission(trace, row)
        return row

    @staticmethod
    def _require_state(
        row: sqlite3.Row, expected_states: set[str]
    ) -> None:
        if row["status"] not in expected_states:
            expected = sorted(expected_states)
            raise DishRuleError(
                "WRONG_STATE",
                f"submission is {row['status']}, expected one of {tuple(expected)}",
                rule="wrong_state",
                details={"actual": row["status"], "expected": expected},
            )

    def _command_recover(
        self,
        *,
        trace: AdminTrace,
        submission_id: str,
        outcome: str,
        reason: str,
    ) -> dict[str, Any]:
        row = self._load_submission(trace=trace, submission_id=submission_id)
        self._require_state(row, _RECOVERABLE_STATES)
        clean_outcome = _clean_required(
            outcome,
            rule="recovery_outcome_required",
            label="recovery outcome",
        )
        if clean_outcome not in _RECOVERY_TARGETS:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "recovery outcome must be not-applied or applied",
                rule="invalid_recovery_outcome",
                details={"outcome": clean_outcome},
            )
        clean_reason = _clean_required(
            reason,
            rule="recovery_reason_required",
            label="inspection reason",
        )
        elapsed = validate_recovery_window(
            row,
            now=self.now_provider(),
            process_liveness_checker=self.process_liveness_checker,
        )
        attempt_id = str(row["write_attempt_id"] or "").strip()
        target_state = _RECOVERY_TARGETS[clean_outcome]
        trace.audit_details.update(
            {
                "decision": "recover",
                "prior_state": row["status"],
                "outcome": clean_outcome,
                "reason": clean_reason,
                "invalidated_write_attempt_id": attempt_id or None,
                "quarantine_elapsed_seconds": elapsed,
                "recovered_state": target_state,
            }
        )
        final = recover_write_attempt(
            self.conn,
            row["submission_id"],
            attempt_id=attempt_id,
            target_state=target_state,
        )
        trace.state = final["status"]
        return result_envelope(
            command="recover",
            task_gid=row["task_gid"],
            submission_id=row["submission_id"],
            state=final["status"],
            data={
                "outcome": clean_outcome,
                "reason": clean_reason,
                "prior_state": row["status"],
            },
        )

    def _command_discard(
        self,
        *,
        trace: AdminTrace,
        submission_id: str,
        reason: str,
    ) -> dict[str, Any]:
        row = self._load_submission(trace=trace, submission_id=submission_id)
        self._require_state(row, _DISCARDABLE_STATES)
        clean_reason = _clean_required(
            reason,
            rule="discard_reason_required",
            label="discard reason",
        )
        trace.audit_details.update(
            {
                "decision": "discard",
                "prior_state": row["status"],
                "reason": clean_reason,
            }
        )
        final = transition_submission(
            self.conn,
            row["submission_id"],
            _DISCARDABLE_STATES,
            "discarded",
            updates={"completed_at": utc_now()},
        )
        trace.state = final["status"]
        return result_envelope(
            command="discard",
            task_gid=row["task_gid"],
            submission_id=row["submission_id"],
            state=final["status"],
            data={"reason": clean_reason, "prior_state": row["status"]},
        )

    def _command_unblock(
        self,
        *,
        trace: AdminTrace,
        submission_id: str,
        reason: str,
    ) -> dict[str, Any]:
        row = self._load_submission(trace=trace, submission_id=submission_id)
        self._require_state(row, {"awaiting_human"})
        clean_reason = _clean_required(
            reason,
            rule="unblock_reason_required",
            label="concrete-change reason",
        )
        trace.audit_details.update(
            {
                "decision": "unblock",
                "reason": clean_reason,
                "prior_failed_verification_passes": row[
                    "failed_verification_passes"
                ],
            }
        )
        final = transition_submission(
            self.conn,
            row["submission_id"],
            {"awaiting_human"},
            "drafting",
            updates={"failed_verification_passes": 0},
        )
        trace.state = final["status"]
        return result_envelope(
            command="unblock",
            task_gid=row["task_gid"],
            submission_id=row["submission_id"],
            state=final["status"],
            data={"reason": clean_reason},
        )


def _step5_admin_migrate(self, *, trace: AdminTrace, task_gid: str) -> dict[str, Any]:
    from .step5 import migrate_live_task
    clean = _clean_required(task_gid, rule="task_gid_required", label="task GID")
    trace.task_gid = clean
    if self.backend is None or self.release_loader is None:
        raise DishRuleError("INTERNAL_ERROR", "migration backend is unavailable", rule="migration_backend_unavailable")
    release = self.release_loader()
    live = self.operation_service.current.start_operation(
        lambda: migrate_live_task(self.conn, self.backend, task_gid=clean, release=release)
    )
    trace.audit_details.update({"schema_version": release.schema_version, "confirmed_identity": live.identity})
    return result_envelope(command="migrate", task_gid=clean, data={"task_gid": clean, "schema_version": release.schema_version, "content_identity": live.identity, "confirmed": True})



# Step 8 Marco-only two-pass hold reopen.
def _step8_admin_reopen(self, *, trace: AdminTrace, submission_id: str, category: str, before: str, after: str, editor: str, model: str, date: str, run_id: str | None = None, file_path: str | None = None) -> dict[str, Any]:
    if self.backend is None:
        raise DishRuleError("INTERNAL_ERROR", "admin backend is required", rule="backend_required")
    from .step8 import reopen_two_pass
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    row = self.conn.execute("SELECT task_gid FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if row is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    trace.submission_id = operation_id; trace.task_gid = row["task_gid"]; trace.state = "open"
    release = None if self.release_loader is None else self.release_loader()
    schema = None if release is None else release.schema
    data, view = self.operation_service.current.reopen_two_pass(
        operation_id,
        lambda: reopen_two_pass(
            self.conn, self.backend, operation_id=operation_id, category=category,
            before=before, after=after, editor=editor, model=model, run_id=run_id,
            file_path=file_path, date=date, honest_root=None if release is None else release.root,
            schema=schema,
        ),
        schema=schema,
    )
    trace.state = view["status"]
    return result_envelope(command="reopen", task_gid=trace.task_gid, submission_id=operation_id, state=view["status"], allowed_actions=view["legal_actions"], data=data)


# Step 9 live-evidence recovery inspection for operation-backed work.
_step8_admin_recover = DishAdminApplication._command_recover

def _step9_admin_recover(self, *, trace: AdminTrace, submission_id: str, outcome: str = "inspect", reason: str = "live inspection") -> dict[str, Any]:
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    exists = self.conn.execute("SELECT task_gid FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if exists is None:
        return _step8_admin_recover(self, trace=trace, submission_id=submission_id, outcome=outcome, reason=reason)
    if self.backend is None:
        raise DishRuleError("INTERNAL_ERROR", "admin backend is required", rule="backend_required")
    from .step9 import recover_operation
    trace.submission_id = operation_id
    trace.task_gid = exists["task_gid"]
    data, view = self.operation_service.current.recover(
        operation_id,
        lambda: recover_operation(self.conn, self.backend, operation_id=operation_id, requested_outcome=outcome, reason=reason),
    )
    trace.state = view["status"]
    return result_envelope(command="recover", task_gid=trace.task_gid, submission_id=operation_id, state=view["status"], allowed_actions=view["legal_actions"], data=data)



def _resolve_protocol_hold(
    self,
    *,
    trace: AdminTrace,
    submission_id: str,
    resolution_kind: str,
    detail: str,
    resume_status: str,
    file_path: str | None = None,
    editor: str | None = None,
    model: str | None = None,
    run_id: str | None = None,
) -> dict[str, Any]:
    if self.backend is None or self.release_loader is None:
        raise DishRuleError("INTERNAL_ERROR", "hold resolution requires backend and Honest release", rule="hold_resolution_unavailable")
    from .step8 import resolve_hold
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    row = self.conn.execute("SELECT task_gid FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    release = self.release_loader()
    trace.submission_id = operation_id
    trace.task_gid = row["task_gid"]
    action = "supply-evidence" if resolution_kind == "evidence" else "record-human-decision"
    data, view = self.operation_service.current.resolve_hold(
        operation_id, action,
        lambda: resolve_hold(
            self.conn, self.backend, operation_id=operation_id, resolution_kind=resolution_kind,
            detail=detail, resume_status=resume_status, honest_root=release.root,
            schema=release.schema, file_path=file_path, editor=editor, model=model, run_id=run_id,
        ),
        schema=release.schema,
    )
    trace.state = view["status"]
    return result_envelope(
        command=action, task_gid=trace.task_gid, submission_id=operation_id,
        state=view["status"], allowed_actions=view["legal_actions"], data=data,
    )


def _command_supply_evidence(self, *, trace: AdminTrace, submission_id: str, detail: str, resume_status: str, file_path: str | None = None, editor: str | None = None, model: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    return _resolve_protocol_hold(
        self, trace=trace, submission_id=submission_id, resolution_kind="evidence",
        detail=detail, resume_status=resume_status, file_path=file_path, editor=editor, model=model, run_id=run_id,
    )


def _command_record_human_decision(self, *, trace: AdminTrace, submission_id: str, detail: str, resume_status: str, file_path: str | None = None, editor: str | None = None, model: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    return _resolve_protocol_hold(
        self, trace=trace, submission_id=submission_id, resolution_kind="human_review",
        detail=detail, resume_status=resume_status, file_path=file_path, editor=editor, model=model, run_id=run_id,
    )




def _command_authorize_governed_change(self, *, trace: AdminTrace, submission_id: str, field: str, before: str, after: str, reason: str, run_id: str | None = None) -> dict[str, Any]:
    from .database import record_marco_authorization
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    op = self.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    row = record_marco_authorization(
        self.conn, task_gid=op["task_gid"], operation_id=operation_id,
        field_name=_clean_required(field, rule="authorization_field_required", label="field"),
        before=before, after=after, reason=reason, actor_run_id=run_id,
    )
    trace.submission_id = operation_id
    trace.task_gid = op["task_gid"]
    trace.state = op["status"]
    return result_envelope(command="authorize-governed-change", task_gid=op["task_gid"], submission_id=operation_id, state=op["status"], data={"authorization_id": row["authorization_id"], "field": row["field_name"]})


# Current-operation cancellation. Legacy discard remains available only for
# quarantined older records through the original handler.
_legacy_discard = DishAdminApplication._command_discard

def _current_operation_discard(self, *, trace: AdminTrace, submission_id: str, reason: str) -> dict[str, Any]:
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    op = self.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if op is None:
        return _legacy_discard(self, trace=trace, submission_id=submission_id, reason=reason)
    if op["status"] not in {"open", "uncertain"}:
        raise DishRuleError("WRONG_STATE", "operation is not cancellable", rule="operation_not_cancellable", details={"actual": op["status"]})
    if self.backend is None:
        raise DishRuleError("INTERNAL_ERROR", "current-operation cancellation requires backend evidence", rule="backend_required")
    unresolved_write = self.conn.execute("SELECT 1 FROM write_attempts WHERE operation_id=? AND outcome IN ('started','uncertain') LIMIT 1", (operation_id,)).fetchone()
    unresolved_move = self.conn.execute("SELECT 1 FROM movement_attempts WHERE operation_id=? AND outcome IN ('started','uncertain') LIMIT 1", (operation_id,)).fetchone()
    if unresolved_write or unresolved_move:
        raise DishRuleError("CONFLICT", "operation has unresolved external side effects", rule="operation_cancel_side_effects_unresolved")
    applied_write = self.conn.execute("SELECT 1 FROM write_attempts WHERE operation_id=? AND outcome='confirmed' LIMIT 1", (operation_id,)).fetchone()
    applied_move = self.conn.execute("SELECT 1 FROM movement_attempts WHERE operation_id=? AND outcome='confirmed' LIMIT 1", (operation_id,)).fetchone()
    completed_step = self.conn.execute("SELECT 1 FROM operation_steps WHERE operation_id=? AND completed_at IS NOT NULL LIMIT 1", (operation_id,)).fetchone()
    if applied_write or applied_move or completed_step:
        raise DishRuleError(
            "CONFLICT",
            "operation has applied workflow effects and must be recovered or explicitly compensated",
            rule="operation_cancel_applied_effects",
        )
    from .task_store import read_complete_task
    from .constants import COOKING_PROJECT_GID
    live = read_complete_task(self.backend, task_gid=op["task_gid"], project_gid=COOKING_PROJECT_GID)
    if live.identity != op["expected_identity"]:
        raise DishRuleError(
            "CONFLICT",
            "live content does not match the operation's pre-operation baseline",
            rule="operation_cancel_live_drift",
            details={"expected_identity": op["expected_identity"], "actual_identity": live.identity},
        )
    from .database import transition_operation
    final = transition_operation(self.conn, operation_id, phase="terminal", status="cancelled", terminal_outcome="cancelled_by_marco")
    record_audit(self.conn, submission_id=None, task_gid=op["task_gid"], operation_id=operation_id, event_type="operation.cancelled", actor_agent=None, details={"reason": _clean_required(reason, rule="discard_reason_required", label="discard reason")}, result_code="OK", result_ok=True, actor_source="marco-admin")
    trace.submission_id=operation_id; trace.task_gid=op["task_gid"]; trace.state=final["status"]
    return result_envelope(command="discard", task_gid=op["task_gid"], submission_id=operation_id, state=final["status"], data={"reason": reason})

class CurrentDishAdminApplication(DishAdminApplication):
    """Current admin transport; unsupported legacy methods remain on the base."""

    _command_migrate = _step5_admin_migrate
    _command_reopen = _step8_admin_reopen
    _command_recover = _step9_admin_recover
    _command_supply_evidence = _command_supply_evidence
    _command_record_human_decision = _command_record_human_decision
    _command_authorize_governed_change = _command_authorize_governed_change
    _command_discard = _current_operation_discard


DishAdminApplication = CurrentDishAdminApplication
