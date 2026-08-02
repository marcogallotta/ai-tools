"""Agent-facing command behavior for the guarded ``dish`` CLI."""

from __future__ import annotations

from .audit_repair import attempt_command_audit_repairs, attach_audit_repair_warning

import asyncio
import inspect
import json
import sqlite3
from typing import Any, Callable, Mapping

from .application_service import OperationApplicationService
from .command_support import (
    CommandBackend,
    CommandTrace,
    _clean_required,
    _gid,
    _require_cooking_task,
    reject_undeclared_arguments,
)
from .constants import AGENT_FAMILIES, CHANGE_LEVELS, COOKING_PROJECT_GID, SUBMISSION_KINDS
from .invocation_audit import record_invocation_audit
from .errors import BackendFailure, DishRuleError
from .models import (
    ResolvedRelease,
    SectionRegistry,
    agent_family,
    is_protocol_managed,
    validate_change_reason,
    validate_create_title,
    validate_independence_attestation,
    validate_rejection_reason,
)
from .results import error_envelope, result_envelope
from .validation_scope import scope_for_command


_AGENT_EXPOSED_ACTIONS = {
    "approve", "create", "inspect", "prepare", "read", "reject",
    "section-tasks", "sections", "start", "submit",
}
_ADMIN_ONLY_ACTIONS = {
    "record-human-decision", "reconcile-abandonment", "reopen",
    "repair-destination", "supply-evidence",
}


def _exposed_action_contract(
    actions: list[str] | tuple[str, ...],
) -> tuple[list[str], str | None, str | None]:
    """Translate internal workflow actions to commands exposed to agents.

    Internal policy may require a Marco-admin continuation. Those commands must
    never appear in an agent response's ``allowed_actions`` because the Action
    and agent CLI cannot execute them. The exact private continuation remains
    visible as a diagnostic.
    """
    required_start_kind = "verification" if "verify" in actions else None
    translated = ["start" if action == "verify" else action for action in actions]
    required_admin_action = next(
        (action for action in translated if action in _ADMIN_ONLY_ACTIONS),
        None,
    )
    exposed = [
        action for action in translated
        if action in _AGENT_EXPOSED_ACTIONS
    ]
    return exposed, required_start_kind, required_admin_action


def expose_authoritative_view(view: Mapping[str, Any]) -> dict[str, Any]:
    """Return an agent-facing copy of an authoritative internal view."""
    exposed = dict(view)
    actions, required_start_kind, required_admin_action = _exposed_action_contract(
        list(view.get("legal_actions") or [])
    )
    exposed["legal_actions"] = actions
    if required_start_kind is not None:
        exposed["required_start_kind"] = required_start_kind
    if required_admin_action is not None:
        exposed["required_admin_action"] = required_admin_action
    return exposed


def _exposed_view(view: Mapping[str, Any]) -> dict[str, Any]:
    """Backward-compatible internal alias for the shared exposure contract."""

    return expose_authoritative_view(view)


def _exposed_result_contract(
    view: Mapping[str, Any], data: Mapping[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    actions, required_start_kind, required_admin_action = _exposed_action_contract(
        list(view.get("legal_actions") or [])
    )
    exposed_data = dict(data)
    if required_start_kind is not None:
        exposed_data["required_start_kind"] = required_start_kind
    required_admin_action = (
        required_admin_action or view.get("required_admin_action")
    )
    if required_admin_action is not None:
        exposed_data["required_admin_action"] = required_admin_action
    if view.get("recovery_required"):
        for key in (
            "recovery_required",
            "recovery_reasons",
            "resolver",
            "continuation_surface",
            "connected_action_available",
            "admin_command",
            "directive",
            "required_action",
            "historical_evidence",
        ):
            if key in view:
                exposed_data[key] = view[key]
    return actions, exposed_data


def _copy_recovery_guidance(
    view: Mapping[str, Any], data: dict[str, Any]
) -> None:
    if "required_admin_action" in view:
        data["required_admin_action"] = view["required_admin_action"]
    if view.get("recovery_required"):
        for key in (
            "recovery_required",
            "recovery_reasons",
            "resolver",
            "continuation_surface",
            "connected_action_available",
            "admin_command",
            "directive",
            "required_action",
            "historical_evidence",
        ):
            if key in view:
                data[key] = view[key]


def _admin_resolver(action: str | None) -> str | None:
    if action is None:
        return None
    return f"Marco/admin {action}"


_HOLD_ADMIN_ACTIONS = {
    "supply-evidence": {
        "cycle_route": "evidence",
        "preconstruction_route": "evidence",
        "detail_placeholder": "<summarize the supplied evidence>",
    },
    "record-human-decision": {
        "cycle_route": "human_review",
        "preconstruction_route": "human-review",
        "detail_placeholder": "<summarize the human's decision and reasoning>",
    },
}


def _reopen_two_pass_continuation(operation_id: str, view: Mapping[str, Any]) -> dict[str, Any]:
    """Describe the reachable private continuation for a two-pass Verification hold."""

    command = f"dish-admin reopen {operation_id} --category ... --before ... --after ... --editor ... --model ... --run-id ... --file ... --date ..."
    directive = (
        "Two independent Verification passes ended without a signable task (see this task's "
        "Status detail for why). Reopening requires a genuinely new corrected candidate, not a "
        "filled-in template: tell the human what concretely must change, then have an editor "
        "construct that candidate and run `dish-admin reopen --help` for the exact "
        "--category/--before/--after/--editor/--model/--run-id/--file/--date flags this needs. "
        f"The operation ID is {operation_id}. Then wait for confirmation it succeeded before "
        "continuing — do not start a new operation; resume this same submission."
    )
    return {
        "phase": str(view.get("phase") or ""),
        "submission_id": operation_id,
        "existing_submission_id": operation_id,
        "required_admin_action": "reopen",
        "resolver": "Marco/admin reopen",
        "continuation_surface": "private-admin",
        "connected_action_available": False,
        "admin_command": command,
        "directive": directive,
        "after_resolution": {
            "legal_actions": ["start"],
            "required_start_kind": "verification",
            "phase": "await_verification",
        },
    }


def _repair_destination_continuation(operation_id: str, view: Mapping[str, Any]) -> dict[str, Any]:
    """Describe the reachable private continuation for a stuck destination move."""

    command = (
        f"dish-admin repair-destination {operation_id} "
        '--destination-section-gid <SECTION_GID> --reason "<why this destination is correct>"'
    )
    directive = (
        "The final destination move failed and cannot be retried automatically (see this task's "
        "Status detail). Tell the human the correct destination section must be determined by "
        "inspecting live section state, then ask them to run the following command after "
        "replacing the section GID and reason placeholders:\n"
        f"{command}\n"
        "Then wait for confirmation it succeeded before continuing — do not start a new "
        "operation; resume this same submission with `submit`."
    )
    return {
        "phase": str(view.get("phase") or ""),
        "submission_id": operation_id,
        "existing_submission_id": operation_id,
        "required_admin_action": "repair-destination",
        "resolver": "Marco/admin repair-destination",
        "continuation_surface": "private-admin",
        "connected_action_available": False,
        "admin_command": command,
        "directive": directive,
        "after_resolution": {"legal_actions": ["submit"], "phase": "await_submission"},
    }


def _evidence_hold_continuation(
    conn: sqlite3.Connection, operation_id: str, view: Mapping[str, Any]
) -> dict[str, Any]:
    """Describe the reachable private continuation for an Evidence or Human Review hold."""

    admin_action = view.get("required_admin_action")
    if admin_action == "reopen":
        return _reopen_two_pass_continuation(operation_id, view)
    if admin_action == "repair-destination":
        return _repair_destination_continuation(operation_id, view)
    routes = _HOLD_ADMIN_ACTIONS.get(admin_action)
    if routes is None:
        return {}
    phase = str(view.get("phase") or "")
    resume_status = None
    preconstruction = conn.execute(
        """SELECT intended_json FROM operation_steps
             WHERE operation_id=? AND step_name='research_preconstruction_hold'
               AND completed_at IS NOT NULL""",
        (operation_id,),
    ).fetchone()
    if preconstruction is not None:
        try:
            intended = json.loads(preconstruction["intended_json"])
        except (TypeError, ValueError):
            intended = {}
        resume_status = intended.get("resume_status")
    if resume_status is None:
        cycle = conn.execute(
            """SELECT resume_state FROM verification_cycles
                 WHERE operation_id=? AND route=?
                 ORDER BY cycle_number DESC LIMIT 1""",
            (operation_id, routes["cycle_route"]),
        ).fetchone()
        if cycle is not None:
            resume_status = cycle["resume_state"]

    after_resolution: dict[str, Any] = {"legal_actions": []}
    if resume_status == "pending-research":
        after_resolution = {
            "legal_actions": ["prepare"],
            "phase": "prepare_required",
        }
    elif resume_status == "pending-verification":
        after_resolution = {
            "legal_actions": ["start"],
            "required_start_kind": "verification",
            "phase": "await_verification",
        }

    command = f'dish-admin {admin_action} {operation_id} --detail "{routes["detail_placeholder"]}"'
    if resume_status:
        command += f" --resume-status {resume_status}"
    next_action = after_resolution["legal_actions"][0] if after_resolution["legal_actions"] else None
    directive = (
        "Tell the human what fact or decision is missing (see this task's Status detail), then "
        "ask them to run the following command after replacing the angle-bracketed detail text "
        "with that answer:\n"
        f"{command}\n"
        "Then wait for confirmation it succeeded before continuing — do not start a new "
        "operation; resume this same submission"
        + (f" with `{next_action}`." if next_action else ".")
    )
    return {
        "phase": phase,
        "submission_id": operation_id,
        "existing_submission_id": operation_id,
        "required_admin_action": admin_action,
        "resolver": f"Marco/admin {admin_action}",
        "continuation_surface": "private-admin",
        "connected_action_available": False,
        "admin_command": command,
        "directive": directive,
        "after_resolution": after_resolution,
    }


def _apply_hold_continuation(
    conn: sqlite3.Connection, operation_id: str, view: Mapping[str, Any], data: dict[str, Any]
) -> dict[str, Any]:
    data.update(_evidence_hold_continuation(conn, operation_id, view))
    return data


class DishApplication:
    """Command dispatcher with one audit event per invocation."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        backend: CommandBackend,
        *,
        release_loader: Callable[..., ResolvedRelease],
        invocation_run_id: str | None = None,
        invocation_request_id: str | None = None,
    ) -> None:
        self.conn = conn
        self.backend = backend
        self.release_loader = release_loader
        self.invocation_run_id = str(invocation_run_id or "").strip() or None
        self.invocation_request_id = str(invocation_request_id or "").strip() or None
        self.operation_service = OperationApplicationService(
            conn, backend, request_id=self.invocation_request_id
        )
        parameters = inspect.signature(release_loader).parameters.values()
        self._release_loader_accepts_role = any(
            parameter.kind
            in {
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            }
            for parameter in parameters
        )

    def _load_release(self, protocol_role: str | None) -> ResolvedRelease:
        if self._release_loader_accepts_role:
            return self.release_loader(protocol_role)
        return self.release_loader()


    def execute(self, command: str, **arguments: Any) -> dict[str, Any]:
        repair_attempt = attempt_command_audit_repairs(
            self.conn, surface="dish"
        )
        trace = CommandTrace(
            task_gid=arguments.get("task_gid"),
            submission_id=arguments.get("submission_id"),
        )
        actor = arguments.get("agent")
        handler = CURRENT_COMMAND_HANDLERS.get(command)
        try:
            if handler is None:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    f"unknown dish command: {command}",
                    rule="invalid_command",
                )
            reject_undeclared_arguments(handler, arguments)
            result = handler(self, trace=trace, **arguments)
        except DishRuleError as exc:
            if trace.task_gid is None:
                trace.task_gid = _gid(exc.details.get("task_gid"))
            if trace.submission_id is None:
                trace.submission_id = _gid(exc.details.get("operation_id"))
            if exc.code == "WRONG_STATE" and exc.details.get("actual"):
                trace.state = str(exc.details["actual"])
            result = error_envelope(
                command,
                exc,
                task_gid=trace.task_gid,
                submission_id=trace.submission_id,
                state=trace.state,
                validation_scope=trace.validation_scope,
            )
            if exc.rule == "open_operation_exists":
                result.setdefault("data", {}).update({
                    key: exc.details[key]
                    for key in (
                        "existing_submission_id",
                        "phase",
                        "required_admin_action",
                        "resolver",
                    )
                    if key in exc.details
                })
            if exc.code == "BACKEND_UNCERTAIN" and (
                exc.details.get("execution_id")
                or exc.rule == "planning_reopen_reconciliation_required"
            ):
                result.setdefault("data", {}).update(exc.details)
            for key in (
                "required_admin_action",
                "required_start_kind",
                "resolver",
                "legal_next_step",
            ):
                if exc.details.get(key):
                    result.setdefault("data", {})[key] = exc.details[key]
            if exc.rule != "open_operation_exists":
                for key in ("admin_command", "directive"):
                    if exc.details.get(key):
                        result.setdefault("data", {})[key] = exc.details[key]
            if exc.rule == "planning_handoff_requires_initial":
                result["allowed_actions"] = ["start"]
            if trace.submission_id:
                try:
                    release = self._load_release(None)
                    view = _exposed_view(
                        self.operation_service.authoritative_view(
                            trace.submission_id, schema=release.schema
                        )
                    )
                    actions, exposed_data = _exposed_result_contract(
                        view, result.get("data", {})
                    )
                    result["state"] = view["status"]
                    result["allowed_actions"] = actions
                    result["data"] = exposed_data
                except Exception:
                    # Error reporting must not hide the original governed failure.
                    pass
        except Exception:
            exc = DishRuleError(
                "INTERNAL_ERROR",
                "unexpected internal failure",
                rule="unexpected_internal_failure",
            )
            result = error_envelope(
                command,
                exc,
                task_gid=trace.task_gid,
                submission_id=trace.submission_id,
                state=trace.state,
                validation_scope=trace.validation_scope,
            )
        attach_audit_repair_warning(result, repair_attempt, surface="dish")
        self._record_invocation(command, trace.actor_agent or actor, trace, result)
        return result

    def record_argument_failure(
        self,
        command: str,
        error: DishRuleError,
        *,
        agent: str | None = None,
        task_gid: str | None = None,
        submission_id: str | None = None,
    ) -> dict[str, Any]:
        repair_attempt = attempt_command_audit_repairs(
            self.conn, surface="dish"
        )
        trace = CommandTrace(task_gid=task_gid, submission_id=submission_id)
        if submission_id:
            row = self.conn.execute(
                "SELECT task_gid, status, editor_agent, operation_kind FROM operations WHERE operation_id=?",
                (submission_id,),
            ).fetchone()
            if row is not None:
                trace.task_gid = row["task_gid"]
                trace.state = row["status"]
                trace.actor_agent = row["editor_agent"]
                trace.validation_scope = scope_for_command(
                    command, operation_kind=row["operation_kind"]
                )
        result = error_envelope(
            command, error, task_gid=trace.task_gid,
            submission_id=submission_id, state=trace.state,
            validation_scope=trace.validation_scope,
        )
        attach_audit_repair_warning(result, repair_attempt, surface="dish")
        self._record_invocation(command, trace.actor_agent or agent, trace, result)
        return result

    def _read_live_task(self, task_gid: str) -> dict[str, Any]:
        try:
            return self.backend.read_task(task_gid)
        except BackendFailure as exc:
            if exc.status == 404:
                raise DishRuleError(
                    "NOT_FOUND",
                    f"task not found: {task_gid}",
                    rule="task_not_found",
                ) from exc
            raise

    def _validate_start_arguments(
        self,
        *,
        kind: str,
        change_level: str | None,
        change_reason: str | None,
    ) -> tuple[str | None, str | None]:
        if kind not in SUBMISSION_KINDS:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                f"invalid submission kind: {kind}",
                rule="invalid_submission_kind",
            )
        clean_level = str(change_level).strip() if change_level is not None else None
        if kind != "change":
            if clean_level is not None or change_reason is not None:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    "change arguments are only valid for change operations",
                    rule="change_arguments_forbidden",
                )
            return None, None
        clean_reason = (
            validate_change_reason(change_reason)
            if change_reason is not None
            else None
        )
        if clean_level is None:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "change level is required for change operations",
                rule="change_level_required",
            )
        if clean_level not in CHANGE_LEVELS:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                f"invalid change level: {clean_level}",
                rule="invalid_change_level",
            )
        if not clean_reason:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "change reason is required for change operations",
                rule="change_reason_required",
            )
        return clean_level, clean_reason

    def _record_invocation(
        self,
        command: str,
        actor: Any,
        trace: CommandTrace,
        result: Mapping[str, Any],
    ) -> None:
        record_invocation_audit(
            self.conn,
            surface="dish",
            command=command,
            result=result,
            task_gid=trace.task_gid,
            submission_id=trace.submission_id,
            actor=trace.actor_agent or actor,
            actor_run_id=self.invocation_run_id,
            audit_details=trace.audit_details,
        )




# Step 5 lifecycle command handlers.
def _step5_sections(self, *, trace: CommandTrace, agent: str) -> dict[str, Any]:
    agent_family(agent)
    sections = self.backend.list_sections(COOKING_PROJECT_GID)
    clean = [{"gid": _gid(item), "name": str(item.get("name") or "")} for item in sections]
    return result_envelope(command="sections", data={"project_gid": COOKING_PROJECT_GID, "sections": clean})


def _step5_section_tasks(
    self, *, trace: CommandTrace, agent: str, section_gid: str, cursor: str | None = None
) -> dict[str, Any]:
    agent_family(agent)
    section_gid = _clean_required(section_gid, rule="section_gid_required", label="section GID")
    clean_cursor = cursor.strip() if isinstance(cursor, str) and cursor.strip() else None
    tasks, next_cursor = self.backend.list_tasks_for_section(section_gid, cursor=clean_cursor)
    clean = [
        {"gid": _gid(item), "name": str(item.get("name") or ""), "completed": bool(item.get("completed"))}
        for item in tasks
    ]
    return result_envelope(
        command="section-tasks",
        data={"section_gid": section_gid, "tasks": clean, "next_cursor": next_cursor},
    )


def _step5_create(self, *, trace: CommandTrace, agent: str, title: str) -> dict[str, Any]:
    agent_family(agent)
    clean_title = validate_create_title(title)
    release = self._load_release(None)
    registry = SectionRegistry.from_sections(self.backend.list_sections(COOKING_PROJECT_GID))
    task = self.operation_service.current.create_task(
        lambda: self.backend.create_bare_task(title=clean_title, project_gid=COOKING_PROJECT_GID, section_gid=registry.research_queue_gid)
    )
    task_gid = _clean_required(task.get("gid"), rule="created_task_gid_missing", label="created task GID")
    trace.task_gid = task_gid
    return result_envelope(command="create", task_gid=task_gid, allowed_actions=["start"], data={"task_gid": task_gid, "schema_version": release.schema_version, "bare_task": True, "required_start_kind": "planning"})


def _step5_read(self, *, trace: CommandTrace, agent: str, task_gid: str) -> dict[str, Any]:
    from .step5 import diagnostics_for
    from .task_store import read_complete_task
    agent_family(agent)
    task_gid = _clean_required(task_gid, rule="task_gid_required", label="task GID")
    trace.task_gid = task_gid
    release = self._load_release(None)
    _require_cooking_task(self._read_live_task(task_gid), task_gid)
    live = read_complete_task(self.backend, task_gid=task_gid, project_gid=COOKING_PROJECT_GID)
    diag = diagnostics_for(live, release)
    stored = self.conn.execute("SELECT * FROM task_content_state WHERE task_gid = ?", (task_gid,)).fetchone()
    drift = None if stored is None else stored["last_confirmed_identity"] != live.identity
    data = {
        "task": {"gid": live.gid, "title": live.title, "notes": live.notes, "section_gid": live.section_gid, "completed": live.completed, "modified_at": live.modified_at},
        "parsed": diag["parsed"], "task_schema_version": diag["schema_version"],
        "content_identity": live.identity, "stored_identity": None if stored is None else stored["last_confirmed_identity"],
        "drift": drift, "migration_required": diag["migration_required"],
        "placement": {"project_gid": COOKING_PROJECT_GID, "section_gid": live.section_gid},
        "compatibility": {"protocol_version": release.protocol_version, "schema_version": release.schema_version},
        "validation": diag["validation"],
    }
    operation = self.conn.execute(
        """SELECT operation_id FROM operations
             WHERE task_gid=? AND status IN ('open','uncertain')
             ORDER BY created_at DESC LIMIT 1""",
        (task_gid,),
    ).fetchone()
    if operation is None:
        return result_envelope(command="read", task_gid=task_gid, data=data)
    operation_id = operation["operation_id"]
    view = _exposed_view(_current_operation_view(self, operation_id, schema=release.schema))
    data["active_operation"] = {
        "submission_id": operation_id,
        "authoritative_view": view,
    }
    if view.get("required_start_kind") is not None:
        data["required_start_kind"] = view["required_start_kind"]
    _copy_recovery_guidance(view, data)
    _apply_hold_continuation(self.conn, operation_id, view, data)
    trace.submission_id = operation_id
    trace.state = view["status"]
    return result_envelope(
        command="read", task_gid=task_gid, submission_id=operation_id,
        state=trace.state, allowed_actions=view["legal_actions"], data=data,
    )


def _current_operation_view(self, operation_id: str, *, schema=None) -> dict[str, Any]:
    return self.operation_service.authoritative_view(operation_id, schema=schema)


def _step5_inspect(self, *, trace: CommandTrace, agent: str, submission_id: str) -> dict[str, Any]:
    from .step5 import inspect_operation, verification_lineage
    from .step7 import record_current_dish_inspect
    agent_family(agent)
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    exists = self.conn.execute("SELECT 1 FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if exists is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    release = self._load_release(None)
    inspect_fact = record_current_dish_inspect(
        self.conn, self.backend, operation_id=operation_id, agent=agent,
        invocation_run_id=self.invocation_run_id, schema=release.schema,
    )
    data = inspect_operation(self.conn, operation_id)
    data["dish_inspect_fact"] = inspect_fact
    data["verification_lineage"] = verification_lineage(
        self.conn, operation_id, current_run_id=self.invocation_run_id
    )
    internal_view = _current_operation_view(self, operation_id, schema=release.schema)
    view = _exposed_view(internal_view)
    data["legal_next_actions"] = view["legal_actions"]
    data["authoritative_view"] = view
    content = data.get("content")
    if isinstance(content, dict):
        content["live_identity"] = view.get("live_identity")
        content["required_identity"] = view.get("required_identity")
        content["identity_matches"] = view.get("identity_matches")
    if view.get("required_start_kind") is not None:
        data["required_start_kind"] = view["required_start_kind"]
    _copy_recovery_guidance(view, data)
    _apply_hold_continuation(self.conn, operation_id, view, data)
    trace.submission_id = operation_id
    trace.task_gid = data["operation"]["task_gid"]
    trace.state = view["status"]
    return result_envelope(command="inspect", task_gid=trace.task_gid, submission_id=operation_id, state=trace.state, allowed_actions=view["legal_actions"], data=data)


def _step5_start(self, *, trace: CommandTrace, agent: str, task_gid: str, kind: str, change_level: str | None = None, change_reason: str | None = None, prepared_operation_id: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    from .step5 import (
        claim_operation,
        claim_prepared_stage_successor,
        diagnostics_for,
        start_result_data,
    )
    from .task_store import read_complete_task
    agent_family(agent)
    task_gid = _clean_required(task_gid, rule="task_gid_required", label="task GID")
    trace.task_gid = task_gid
    change_level, change_reason = self._validate_start_arguments(kind=kind, change_level=change_level, change_reason=change_reason)
    role = "planning" if kind == "planning" else "research"
    release = self._load_release(role)
    if kind == "planning":
        from .database import planning_reopen_blocker_for_task
        from .task_store import planning_reopen_recovery_details
        blocker = planning_reopen_blocker_for_task(self.conn, task_gid=task_gid)
        if blocker is not None:
            details = planning_reopen_recovery_details(blocker)
            details["request_status"] = blocker["request_status"]
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "Planning cannot start until the interrupted task reopen is reconciled",
                rule="planning_reopen_reconciliation_required",
                retryable=False,
                details=details,
            )
    _require_cooking_task(self._read_live_task(task_gid), task_gid)
    live = read_complete_task(self.backend, task_gid=task_gid, project_gid=COOKING_PROJECT_GID)
    registry = SectionRegistry.from_sections(self.backend.list_sections(COOKING_PROJECT_GID))
    if not is_protocol_managed(live.section_gid, registry):
        raise DishRuleError("UNMANAGED_TASK", f"task {task_gid} is in an excluded Cooking section", rule="task_in_excluded_section")
    diag = diagnostics_for(live, release)
    if kind == "planning":
        if live.completed:
            reopen_command = (
                f'dish-admin reopen-planning {task_gid} '
                '--reason "<summarize why the task must be reopened>"'
            )
            raise DishRuleError(
                "WRONG_STATE",
                "completed tasks require Marco to reopen them before Planning",
                rule="planning_completed_task_reopen_required",
                details={
                    "required_admin_action": "reopen-planning",
                    "resolver": _admin_resolver("reopen-planning"),
                    "admin_command": reopen_command,
                    "legal_next_step": (
                        "Marco/admin runs reopen-planning with a reason; after it succeeds, "
                        "retry start with kind=planning using a fresh client.request_id"
                    ),
                    "directive": (
                        f"Tell the human to run: {reopen_command}\n"
                        "Then wait for confirmation it succeeded before continuing — retry "
                        "start with kind=planning using a fresh client.request_id; do not "
                        "create a replacement operation."
                    ),
                },
            )
        if live.notes:
            from .task_document import (
                DocumentParseError,
                parse_planning_brief,
                validate_planning_brief,
            )
            try:
                planning_brief = parse_planning_brief(live.notes)
                planning_findings = validate_planning_brief(planning_brief).findings
            except DocumentParseError:
                planning_findings = (object(),)
            if not planning_findings:
                raise DishRuleError(
                    "VALIDATION_FAILED",
                    "Planning is complete; continue with initial Research",
                    rule="planning_handoff_requires_initial",
                    details={
                        "required_start_kind": "initial",
                        "legal_next_step": (
                            "start with kind=initial using a fresh client.request_id; "
                            "do not start Planning again"
                        ),
                    },
                )
            raise DishRuleError("VALIDATION_FAILED", "planning must start from a bare task", rule="planning_notes_not_empty")
    else:
        if diag["parsed"] is None:
            if kind == "initial":
                from .task_document import parse_planning_brief, validate_planning_brief, DocumentParseError
                try:
                    brief = parse_planning_brief(live.notes)
                    brief_findings = validate_planning_brief(brief).findings
                except DocumentParseError:
                    brief_findings = (object(),)
                if brief_findings:
                    raise DishRuleError("VALIDATION_FAILED", "task is neither canonical nor a valid Planning brief", rule="planning_brief_required", errors=diag["validation"])
            else:
                raise DishRuleError("VALIDATION_FAILED", "task is not canonical", rule="canonical_task_required", errors=diag["validation"])
        if diag["parsed"] is not None and diag["migration_required"]:
            raise DishRuleError("VALIDATION_FAILED", "task schema is older than the current schema; migration required", rule="migration_required", details={"task_schema_version": diag["schema_version"], "current_schema_version": release.schema_version})
        if diag["parsed"] is not None and diag["validation"]:
            raise DishRuleError("VALIDATION_FAILED", "task failed current structural validation", errors=diag["validation"])
    try:
        if prepared_operation_id is not None:
            op = self.operation_service.current.start_operation(
                lambda: claim_prepared_stage_successor(
                    self.conn,
                    live=live,
                    release=release,
                    kind=kind,
                    agent=agent,
                    run_id=run_id,
                    prepared_operation_id=prepared_operation_id,
                    change_level=change_level,
                    change_reason=change_reason,
                )
            )
        else:
            op = self.operation_service.current.start_operation(
                lambda: claim_operation(
                    self.conn,
                    live=live,
                    release=release,
                    kind=kind,
                    agent=agent,
                    run_id=run_id,
                    change_level=change_level,
                    change_reason=change_reason,
                )
            )
    except DishRuleError as exc:
        if exc.rule == "planning_reopen_reconciliation_required":
            from .database import planning_reopen_blocker_for_task
            from .task_store import planning_reopen_recovery_details
            blocker = planning_reopen_blocker_for_task(
                self.conn, task_gid=task_gid
            )
            if blocker is not None:
                details = planning_reopen_recovery_details(blocker)
                details["request_status"] = blocker["request_status"]
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "Planning cannot start until the interrupted task reopen is reconciled",
                    rule="planning_reopen_reconciliation_required",
                    retryable=False,
                    details=details,
                ) from exc
            raise
        if exc.rule == "abandonment_fence_active" and prepared_operation_id is None:
            successor_operation_id = exc.details.get("successor_operation_id")
            if (
                not successor_operation_id
                or exc.details.get("abandonment_status") != "awaiting_successor_claim"
            ):
                admin_command = exc.details.get("admin_command")
                raise DishRuleError(
                    exc.code,
                    str(exc),
                    rule=exc.rule,
                    retryable=exc.retryable,
                    details={
                        **exc.details,
                        "resolver": _admin_resolver(
                            exc.details.get("required_admin_action")
                        ),
                        "directive": (
                            f"Tell the human to run: {admin_command}\n"
                            "Then wait for confirmation it succeeded before "
                            "retrying start with a fresh client.request_id."
                        ),
                    },
                ) from exc
            op = self.operation_service.current.start_operation(
                lambda: claim_prepared_stage_successor(
                    self.conn,
                    live=live,
                    release=release,
                    kind=kind,
                    agent=agent,
                    run_id=run_id,
                    prepared_operation_id=successor_operation_id,
                    change_level=change_level,
                    change_reason=change_reason,
                )
            )
        elif exc.rule != "open_operation_exists" or prepared_operation_id is not None:
            raise
        else:
            existing = self.conn.execute(
                """SELECT operation_id FROM operations
                     WHERE task_gid=? AND status IN ('open','uncertain')
                     ORDER BY created_at DESC LIMIT 1""",
                (task_gid,),
            ).fetchone()
            if existing is None:
                raise
            existing_id = existing["operation_id"]
            view = _exposed_view(
                self.operation_service.authoritative_view(
                    existing_id, schema=release.schema
                )
            )
            required_admin_action = view.get("required_admin_action")
            trace.submission_id = existing_id
            trace.state = str(view.get("status") or "") or None
            raise DishRuleError(
                "CONFLICT",
                "task already has an open operation",
                rule="open_operation_exists",
                details={
                    "existing_submission_id": existing_id,
                    "phase": view.get("phase"),
                    "required_admin_action": required_admin_action,
                    "resolver": _admin_resolver(required_admin_action),
                    **_evidence_hold_continuation(self.conn, existing_id, view),
                },
            ) from exc
    trace.submission_id = op["operation_id"]
    trace.state = op["status"]
    return result_envelope(
        command="start",
        task_gid=task_gid,
        submission_id=op["operation_id"],
        state=op["status"],
        allowed_actions=["prepare"],
        data=start_result_data(
            live=live, release=release, registry=registry, kind=kind,
            operation=op, diagnostics=diag,
        ),
    )



def _step6_prepare(self, *, trace: CommandTrace, agent: str, model: str | None = None, submission_id: str, file_path: str | None = None, material_classification: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    from .step6 import prepare_live, preflight_planning_candidate_labels
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    route_release = self._load_release(None)
    routed = self.operation_service.route(operation_id, command="prepare", protocol_version=route_release.protocol_version)
    exists = routed.row
    if routed.generation == "legacy":
        raise DishRuleError(
            "PROTOCOL_INCOMPATIBLE",
            "legacy workflow records are read-only",
            rule="unsupported_legacy_workflow",
        )
    if routed.generation == "missing":
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    trace.submission_id = operation_id
    trace.task_gid = exists["task_gid"]
    operation_kind = self.conn.execute(
        "SELECT operation_kind FROM operations WHERE operation_id = ?",
        (operation_id,),
    ).fetchone()[0]
    trace.validation_scope = scope_for_command(
        "prepare", operation_kind=operation_kind
    )
    release = self._load_release("planning" if operation_kind == "planning" else "research")
    if operation_kind == "planning":
        preflight_planning_candidate_labels(file_path or "")
    data, view = self.operation_service.current.prepare(
        operation_id,
        lambda: prepare_live(self.conn, self.backend, operation_id=operation_id, agent=agent, model=model, file_path=file_path or "", release=release, material_classification=material_classification),
        schema=release.schema,
    )
    trace.state = view["status"]
    legal_actions, data = _exposed_result_contract(view, data)
    if not legal_actions and data.get("handoff") == "planning-to-research":
        # Planning's operation is finished, but the task's next legal command is
        # the Research `start`. Naming it keeps the "do not guess one" rule true.
        legal_actions = ["start"]
        data["required_start_kind"] = "initial"
    return result_envelope(
        command="prepare", task_gid=trace.task_gid, submission_id=operation_id,
        state=view["status"], allowed_actions=legal_actions, data=data,
        validation_scope=trace.validation_scope,
    )


# Step 7 exact-live Verification lifecycle.
_step6_command_start = _step5_start


def _step7_start(
    self,
    *,
    trace: CommandTrace,
    agent: str,
    task_gid: str,
    kind: str,
    change_level: str | None = None,
    change_reason: str | None = None,
    prepared_operation_id: str | None = None,
    target_operation_id: str | None = None,
    target_cycle_id: str | None = None,
    run_id: str | None = None,
    independence_attestation: str | None = None,
) -> dict[str, Any]:
    if kind != "verification":
        return _step6_command_start(
            self, trace=trace, agent=agent, task_gid=task_gid, kind=kind,
            change_level=change_level, change_reason=change_reason,
            prepared_operation_id=prepared_operation_id, run_id=run_id,
        )
    if prepared_operation_id is not None:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "prepared_operation_id is accepted only for Planning or Research successors",
            rule="prepared_operation_id_forbidden",
        )
    from .step7 import resolve_verification_start_target, verification_read
    clean_attestation = validate_independence_attestation(
        independence_attestation
    )
    agent_family(agent)
    task_gid = _clean_required(task_gid, rule="task_gid_required", label="task GID")
    trace.task_gid = task_gid
    operation, cycle, _authority = resolve_verification_start_target(
        self.conn,
        task_gid=task_gid,
        target_operation_id=target_operation_id,
        target_cycle_id=target_cycle_id,
    )
    operation_id = operation["operation_id"]
    cycle_id = cycle["cycle_id"]
    release = self._load_release("verification")
    data, view = self.operation_service.current.start_verification(
        operation_id,
        lambda: verification_read(
            self.conn, self.backend, operation_id=operation_id, agent=agent,
            honest_root=release.root, run_id=run_id,
            independence_attestation=clean_attestation, schema=release.schema,
            target_operation_id=operation_id,
            target_cycle_id=cycle_id,
        ),
        schema=release.schema,
    )
    trace.submission_id = operation_id
    trace.state = view["status"]
    legal_actions, data = _exposed_result_contract(view, data)
    # The reviewed binding is not decision-ready until the verifier performs an
    # exact current inspect, which records the durable fact exposed by policy.
    return result_envelope(
        command="start", task_gid=task_gid, submission_id=operation_id,
        state=view["status"], allowed_actions=legal_actions, data=data,
    )


def _step7_approve(
    self,
    *,
    trace: CommandTrace,
    agent: str,
    model: str | None = None,
    submission_id: str,
    file_path: str | None = None,
    correction: str = "none",
    reviewed_identity: str | None = None,
    semantic_review_complete: bool = False,
    provenance_complete: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    from .step7 import approve_live
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    route_release = self._load_release(None)
    routed = self.operation_service.route(operation_id, command="approve", protocol_version=route_release.protocol_version)
    exists = routed.row
    if routed.generation == "legacy":
        raise DishRuleError(
            "PROTOCOL_INCOMPATIBLE",
            "legacy workflow records are read-only",
            rule="unsupported_legacy_workflow",
        )
    if routed.generation == "missing":
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    trace.validation_scope = scope_for_command("approve")
    trace.submission_id = operation_id
    trace.task_gid = exists["task_gid"]
    clean_identity = _clean_required(reviewed_identity, rule="reviewed_identity_required", label="reviewed content identity")
    release = self._load_release("verification")
    data, view = self.operation_service.current.approve(
        operation_id,
        lambda: approve_live(
            self.conn, self.backend, operation_id=operation_id, agent=agent, model=model,
            reviewed_identity=clean_identity,
            semantic_review_complete=semantic_review_complete,
            provenance_complete=provenance_complete,
            correction_class=correction,
            run_id=run_id,
            schema=release.schema,
        ),
        schema=release.schema,
    )
    trace.state = view["status"]
    legal_actions, data = _exposed_result_contract(view, data)
    return result_envelope(
        command="approve", task_gid=trace.task_gid, submission_id=operation_id,
        state=view["status"], allowed_actions=legal_actions, data=data,
        validation_scope=trace.validation_scope,
    )




# Step 8 protocol-native rejection routes and Small same-pass correction.
_step7_command_approve = _step7_approve

def _step8_approve(self, *, trace: CommandTrace, agent: str, model: str | None = None, submission_id: str, file_path: str | None = None, correction: str = "none", reviewed_identity: str | None = None, semantic_review_complete: bool = False, provenance_complete: bool = False, run_id: str | None = None) -> dict[str, Any]:
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    exists = self.conn.execute("SELECT task_gid FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if exists is not None and correction == "small" and not file_path:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "Small correction approval requires a complete corrected candidate",
            rule="small_correction_file_required",
        )
    if exists is not None and correction != "small" and file_path:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "approval candidate file is accepted only for a Small correction",
            rule="approval_file_unexpected",
        )
    if exists is None or correction != "small":
        return _step7_command_approve(self, trace=trace, agent=agent, model=model, submission_id=submission_id, file_path=file_path, correction=correction, reviewed_identity=reviewed_identity, semantic_review_complete=semantic_review_complete, provenance_complete=provenance_complete, run_id=run_id)
    trace.validation_scope = scope_for_command("approve")
    from .step8 import approve_small
    release = self._load_release("verification")
    trace.submission_id = operation_id; trace.task_gid = exists["task_gid"]; trace.state = "open"
    data, view = self.operation_service.current.approve(
        operation_id,
        lambda: approve_small(self.conn, self.backend, operation_id=operation_id, agent=agent, model=model, file_path=file_path, reviewed_identity=_clean_required(reviewed_identity, rule="reviewed_identity_required", label="reviewed content identity"), semantic_review_complete=semantic_review_complete, provenance_complete=provenance_complete, run_id=run_id, schema=release.schema),
        schema=release.schema,
    )
    trace.state = view["status"]
    legal_actions, data = _exposed_result_contract(view, data)
    return result_envelope(
        command="approve", task_gid=trace.task_gid, submission_id=operation_id,
        state=view["status"], allowed_actions=legal_actions, data=data,
        validation_scope=trace.validation_scope,
    )

def _step8_reject(self, *, trace: CommandTrace, agent: str, model: str | None = None, submission_id: str, reason: str, route: str | None = None, file_path: str | None = None, resume_status: str | None = None, run_id: str | None = None, independence_attestation: str | None = None) -> dict[str, Any]:
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    reason = validate_rejection_reason(reason)
    route_release = self._load_release(None)
    routed = self.operation_service.route(operation_id, command="reject", protocol_version=route_release.protocol_version)
    exists = routed.row
    if routed.generation == "legacy":
        raise DishRuleError(
            "PROTOCOL_INCOMPATIBLE",
            "legacy workflow records are read-only",
            rule="unsupported_legacy_workflow",
        )
    if route is None:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "rejection route is required",
            rule="rejection_route_required",
        )
    if routed.generation == "missing":
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    preconstruction_hold = bool(
        exists["status"] == "open"
        and exists["phase"] == "prepare_required"
        and exists["operation_kind"] == "initial"
        and exists["content_write_completed_at"] is None
    )
    clean_attestation = None if preconstruction_hold else independence_attestation
    trace.validation_scope = scope_for_command("reject")
    from .step8 import reject_route
    release = self._load_release("verification")
    trace.submission_id = operation_id; trace.task_gid = exists["task_gid"]; trace.state = "open"
    data, view = self.operation_service.current.reject(
        operation_id,
        lambda: reject_route(self.conn, self.backend, operation_id=operation_id, agent=agent, model=model, route=route, reason=reason, file_path=file_path, resume_status=resume_status, run_id=run_id, independence_attestation=clean_attestation, request_id=self.invocation_request_id, schema=release.schema, honest_root=release.root),
        schema=release.schema,
    )
    trace.state = view["status"]
    legal_actions, data = _exposed_result_contract(view, data)
    _apply_hold_continuation(
        self.conn, operation_id,
        {**view, "required_admin_action": data.get("required_admin_action")},
        data,
    )
    return result_envelope(
        command="reject", task_gid=trace.task_gid, submission_id=operation_id,
        state=view["status"], allowed_actions=legal_actions, data=data,
        validation_scope=trace.validation_scope,
    )


# Step 9 movement-only submit.

def _step9_submit(self, *, trace: CommandTrace, submission_id: str) -> dict[str, Any]:
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    route_release = self._load_release(None)
    routed = self.operation_service.route(operation_id, command="submit", protocol_version=route_release.protocol_version)
    exists = routed.row
    if routed.generation == "legacy":
        raise DishRuleError(
            "PROTOCOL_INCOMPATIBLE",
            "legacy workflow records are read-only",
            rule="unsupported_legacy_workflow",
        )
    if routed.generation == "missing":
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    trace.validation_scope = scope_for_command("submit")
    from .step9 import submit_live
    release = self._load_release("verification")
    trace.submission_id = operation_id
    trace.task_gid = exists["task_gid"]
    data, view = self.operation_service.current.submit(
        operation_id,
        lambda: submit_live(self.conn, self.backend, operation_id=operation_id, schema=release.schema),
        schema=release.schema,
    )
    trace.state = view["status"]
    legal_actions, data = _exposed_result_contract(view, data)
    return result_envelope(
        command="submit", task_gid=trace.task_gid, submission_id=operation_id,
        state=view["status"], allowed_actions=legal_actions, data=data,
        validation_scope=trace.validation_scope,
    )

CURRENT_COMMAND_HANDLERS = {
    "sections": _step5_sections,
    "section-tasks": _step5_section_tasks,
    "create": _step5_create,
    "read": _step5_read,
    "inspect": _step5_inspect,
    "start": _step7_start,
    "prepare": _step6_prepare,
    "approve": _step8_approve,
    "reject": _step8_reject,
    "submit": _step9_submit,
}
