"""Marco-only lifecycle commands for the separate ``dish-admin`` surface."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .database import get_submission, record_audit, transition_submission
from .errors import DishRuleError
from .models import ProcessIdentity, utc_now
from .recovery import (
    process_identity_is_live,
    recover_write_attempt,
    validate_recovery_window,
)
from .results import error_envelope, result_envelope

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

    def execute(self, command: str, **arguments: Any) -> dict[str, Any]:
        trace = AdminTrace(submission_id=arguments.get("submission_id"))
        handler = getattr(self, f"_command_{command}", None)
        try:
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
    live = migrate_live_task(self.conn, self.backend, task_gid=clean, release=release)
    trace.audit_details.update({"schema_version": release.schema_version, "confirmed_identity": live.identity})
    return result_envelope(command="migrate", task_gid=clean, data={"task_gid": clean, "schema_version": release.schema_version, "content_identity": live.identity, "confirmed": True})

DishAdminApplication._command_migrate = _step5_admin_migrate


# Step 8 Marco-only two-pass hold reopen.
def _step8_admin_reopen(self, *, trace: AdminTrace, submission_id: str, category: str, before: str, after: str, editor: str, date: str) -> dict[str, Any]:
    if self.backend is None:
        raise DishRuleError("INTERNAL_ERROR", "admin backend is required", rule="backend_required")
    from .step8 import reopen_two_pass
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    row = self.conn.execute("SELECT task_gid FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if row is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    trace.submission_id = operation_id; trace.task_gid = row["task_gid"]; trace.state = "open"
    data = reopen_two_pass(self.conn, self.backend, operation_id=operation_id, category=category, before=before, after=after, editor=editor, date=date)
    return result_envelope(command="reopen", task_gid=trace.task_gid, submission_id=operation_id, state="open", data=data)

DishAdminApplication._command_reopen = _step8_admin_reopen
