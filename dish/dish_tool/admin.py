"""Marco-only lifecycle commands for the separate ``dish-admin`` surface."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .application_service import OperationApplicationService
from .command_support import reject_undeclared_arguments
from .database import atomic_persistence, complete_operation_step, declare_operation_step, process_command_audit_repairs, record_audit
from .invocation_audit import record_invocation_audit
from .errors import DishRuleError
from .results import error_envelope, result_envelope


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


class DishAdminApplication:
    """Admin dispatcher with one local audit event per invocation."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        backend: Any | None = None,
        release_loader: Callable[[], Any] | None = None,
        invocation_request_id: str | None = None,
        invocation_run_id: str | None = None,
    ) -> None:
        self.conn = conn
        self.backend = backend
        self.release_loader = release_loader
        self.invocation_request_id = invocation_request_id
        self.invocation_run_id = invocation_run_id
        self.operation_service = (
            None
            if backend is None
            else OperationApplicationService(
                conn, backend, request_id=invocation_request_id
            )
        )

    def execute(self, command: str, **arguments: Any) -> dict[str, Any]:
        try:
            process_command_audit_repairs(self.conn)
        except Exception:
            pass
        trace = AdminTrace(submission_id=arguments.get("submission_id"))
        handler = CURRENT_ADMIN_COMMAND_HANDLERS.get(command)
        try:
            handler = self.validate_arguments(command, arguments)
            result = handler(self, trace=trace, **arguments)
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
            if exc.code == "BACKEND_UNCERTAIN" and exc.details.get("execution_id"):
                result.setdefault("data", {}).update(exc.details)
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

    @staticmethod
    def validate_arguments(command: str, arguments: Mapping[str, Any]):
        """Return the selected handler after deterministic signature validation."""
        handler = CURRENT_ADMIN_COMMAND_HANDLERS.get(command)
        if handler is None:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                f"unknown dish-admin command: {command}",
                rule="invalid_command",
            )
        reject_undeclared_arguments(handler, arguments)
        return handler

    def record_argument_failure(
        self,
        command: str,
        error: DishRuleError,
        *,
        submission_id: str | None = None,
    ) -> dict[str, Any]:
        trace = AdminTrace(submission_id=submission_id)
        if submission_id:
            row = self.conn.execute(
                "SELECT task_gid, status FROM operations WHERE operation_id=?",
                (submission_id,),
            ).fetchone()
            if row is not None:
                trace.task_gid = row["task_gid"]
                trace.state = row["status"]
        result = error_envelope(
            command, error, task_gid=trace.task_gid,
            submission_id=trace.submission_id, state=trace.state,
        )
        self._record_invocation(command, trace, result)
        return result

    def _record_invocation(
        self,
        command: str,
        trace: AdminTrace,
        result: Mapping[str, Any],
    ) -> None:
        record_invocation_audit(
            self.conn,
            surface="dish-admin",
            command=command,
            result=result,
            task_gid=trace.task_gid,
            submission_id=trace.submission_id,
            actor_role="marco",
            audit_details=trace.audit_details,
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



def _step5_admin_reopen_planning(
    self, *, trace: AdminTrace, task_gid: str, reason: str
) -> dict[str, Any]:
    from .constants import COOKING_PROJECT_GID
    from .models import SectionRegistry, is_protocol_managed
    from .step5 import diagnostics_for
    from .task_store import read_complete_task, reopen_completed_task_for_planning

    clean = _clean_required(task_gid, rule="task_gid_required", label="task GID")
    clean_reason = _clean_required(
        reason, rule="planning_reopen_reason_required", label="reopen reason"
    )
    trace.task_gid = clean
    if self.backend is None or self.release_loader is None:
        raise DishRuleError(
            "INTERNAL_ERROR", "planning reopen backend is unavailable",
            rule="planning_reopen_backend_unavailable",
        )
    active = self.conn.execute(
        """SELECT operation_id FROM operations
             WHERE task_gid=? AND status IN ('open','uncertain') LIMIT 1""",
        (clean,),
    ).fetchone()
    if active is not None:
        raise DishRuleError(
            "CONFLICT", "task already has an active operation",
            rule="active_operation_exists",
            details={"operation_id": active["operation_id"]},
        )
    live = read_complete_task(
        self.backend, task_gid=clean, project_gid=COOKING_PROJECT_GID
    )
    registry = SectionRegistry.from_sections(
        self.backend.list_sections(COOKING_PROJECT_GID)
    )
    if not is_protocol_managed(live.section_gid, registry):
        raise DishRuleError(
            "UNMANAGED_TASK", f"task {clean} is in an excluded Cooking section",
            rule="task_in_excluded_section",
        )
    release = self.release_loader()
    diagnostics = diagnostics_for(live, release)
    if live.notes:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "a completed task can be reopened for Planning only while it is bare",
            rule="planning_reopen_notes_not_empty",
            errors=diagnostics["validation"],
        )
    reopened, attempt_id = reopen_completed_task_for_planning(
        self.conn,
        self.backend,
        task_gid=clean,
        project_gid=COOKING_PROJECT_GID,
        reason=clean_reason,
        actor_run_id=self.invocation_run_id,
        request_id=self.invocation_request_id,
    )
    trace.audit_details.update({
        "attempt_id": attempt_id,
        "reason": clean_reason,
        "completed_before": True,
        "completed_after": False,
        "identity": reopened.identity,
        "section_gid": reopened.section_gid,
    })
    return result_envelope(
        command="reopen-planning",
        task_gid=clean,
        allowed_actions=["start"],
        data={
            "attempt_id": attempt_id,
            "reason": clean_reason,
            "task": {
                "gid": reopened.gid,
                "completed": reopened.completed,
                "identity": reopened.identity,
                "section_gid": reopened.section_gid,
            },
            "required_start_kind": "planning",
        },
    )


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


# Step 9 Marco-only destination repair after an unrecoverable final movement failure.
def _step9_admin_repair_destination(
    self,
    *,
    trace: AdminTrace,
    submission_id: str,
    destination_section_gid: str,
    reason: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    operation_id = _clean_required(
        submission_id, rule="operation_id_required", label="operation ID"
    )
    row = self.conn.execute(
        "SELECT task_gid FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if row is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    if self.backend is None or self.release_loader is None:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "destination repair requires backend and current release",
            rule="destination_repair_unavailable",
        )
    from .step9 import repair_destination_live

    release = self.release_loader()
    trace.submission_id = operation_id
    trace.task_gid = row["task_gid"]
    data, view = self.operation_service.current.repair_destination(
        operation_id,
        lambda: repair_destination_live(
            self.conn,
            self.backend,
            operation_id=operation_id,
            destination_section_gid=destination_section_gid,
            reason=reason,
            actor_run_id=run_id,
            schema=release.schema,
        ),
        schema=release.schema,
    )
    trace.state = view["status"]
    trace.audit_details.update({
        "approved_identity": data.get("approved_identity"),
        "repaired_identity": data.get("repaired_identity"),
        "before_destination": data.get("before_destination"),
        "after_destination": data.get("after_destination"),
    })
    return result_envelope(
        command="repair-destination",
        task_gid=trace.task_gid,
        submission_id=operation_id,
        state=view["status"],
        allowed_actions=view["legal_actions"],
        data=data,
    )


# Step 9 live-evidence recovery inspection for operation-backed work.
def _step9_admin_recover(self, *, trace: AdminTrace, submission_id: str, outcome: str = "inspect", reason: str = "live inspection") -> dict[str, Any]:
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    exists = self.conn.execute("SELECT task_gid FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if exists is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
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




def _command_authorize_governed_change(self, *, trace: AdminTrace, submission_id: str, field: str, before: Any, after: Any, reason: str, run_id: str | None = None) -> dict[str, Any]:
    from .database import record_marco_authorization
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    op = self.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    if op["status"] != "open":
        raise DishRuleError(
            "WRONG_STATE",
            "governed changes may only be authorized for an open operation",
            rule="authorization_operation_not_open",
            details={"actual": op["status"]},
        )
    field_name = _clean_required(field, rule="authorization_field_required", label="field")
    if field_name == "Decisions":
        if not isinstance(before, list) or not isinstance(after, list) or not all(
            isinstance(item, str) for item in before + after
        ):
            raise DishRuleError(
                "INVALID_ARGUMENT", "Decisions authorization requires JSON arrays of strings",
                rule="authorization_value_type_mismatch",
            )
        before_value, after_value = tuple(before), tuple(after)
    else:
        if not isinstance(before, str) or not isinstance(after, str):
            raise DishRuleError(
                "INVALID_ARGUMENT", f"{field_name} authorization requires JSON string values",
                rule="authorization_value_type_mismatch",
            )
        before_value, after_value = before, after
    row = record_marco_authorization(
        self.conn, task_gid=op["task_gid"], operation_id=operation_id,
        field_name=field_name, before=before_value, after=after_value,
        reason=reason, actor_run_id=run_id,
    )
    trace.submission_id = operation_id
    trace.task_gid = op["task_gid"]
    trace.state = op["status"]
    return result_envelope(
        command="authorize-governed-change",
        task_gid=op["task_gid"],
        submission_id=operation_id,
        state=op["status"],
        data={
            "authorization_id": row["authorization_id"],
            "field": row["field_name"],
            "before": before_value,
            "after": after_value,
            "reason": row["reason"],
            "run_id": row["actor_run_id"],
        },
    )


# Current-operation cancellation. Historical submissions are read-only.
def _current_operation_discard(self, *, trace: AdminTrace, submission_id: str, reason: str) -> dict[str, Any]:
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    op = self.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
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
    clean_reason = _clean_required(
        reason, rule="discard_reason_required", label="discard reason"
    )
    cancel_step = "operation_cancel"

    def finalize_cancel():
        # Declare the decision intent only after the operation execution claim
        # exists, so a failed cancellation suffix is visible as changed durable
        # evidence and fences later mutations.
        declare_operation_step(
            self.conn, operation_id, cancel_step, {"reason": clean_reason}
        )
        with atomic_persistence(self.conn, "operation_cancel"):
            final = transition_operation(
                self.conn, operation_id, phase="terminal", status="cancelled",
                terminal_outcome="cancelled_by_marco",
            )
            record_audit(
                self.conn, submission_id=None, task_gid=op["task_gid"],
                operation_id=operation_id, event_type="operation.cancelled",
                actor_agent=None, details={"reason": clean_reason},
                result_code="OK", result_ok=True, governed_kind="decision",
                before_state={"status": "open"},
                after_state={"status": "cancelled"},
                actor_source="marco-admin",
            )
            complete_operation_step(self.conn, operation_id, cancel_step)
        return {"reason": clean_reason}

    data, view = self.operation_service.current.cancel(
        operation_id, finalize_cancel
    )
    trace.submission_id = operation_id
    trace.task_gid = op["task_gid"]
    trace.state = view["status"]
    return result_envelope(
        command="discard", task_gid=op["task_gid"],
        submission_id=operation_id, state=view["status"],
        allowed_actions=view["legal_actions"], data=data,
    )

CURRENT_ADMIN_COMMAND_HANDLERS = {
    "migrate": _step5_admin_migrate,
    "reopen-planning": _step5_admin_reopen_planning,
    "reopen": _step8_admin_reopen,
    "recover": _step9_admin_recover,
    "repair-destination": _step9_admin_repair_destination,
    "supply-evidence": _command_supply_evidence,
    "record-human-decision": _command_record_human_decision,
    "authorize-governed-change": _command_authorize_governed_change,
    "discard": _current_operation_discard,
}
