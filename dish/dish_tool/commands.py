"""Agent-facing command behavior for the guarded ``dish`` CLI."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from typing import Any, Callable, Mapping

from .application_service import OperationApplicationService
from .command_support import (
    CommandBackend,
    CommandTrace,
    _clean_required,
    _gid,
    _require_cooking_task,
)
from .constants import AGENT_FAMILIES, CHANGE_LEVELS, COOKING_PROJECT_GID, SUBMISSION_KINDS
from .database import process_command_audit_repairs
from .invocation_audit import record_invocation_audit
from .errors import BackendFailure, DishRuleError
from .models import ResolvedRelease, SectionRegistry, agent_family, is_protocol_managed
from .results import error_envelope, result_envelope
from .validation_scope import scope_for_command


_AGENT_EXPOSED_ACTIONS = {
    "approve", "create", "inspect", "prepare", "read", "reject",
    "sections", "start", "submit",
}
_ADMIN_ONLY_ACTIONS = {
    "record-human-decision", "reopen", "supply-evidence",
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


def _exposed_view(view: Mapping[str, Any]) -> dict[str, Any]:
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


def _exposed_result_contract(
    view: Mapping[str, Any], data: Mapping[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    actions, required_start_kind, required_admin_action = _exposed_action_contract(
        list(view.get("legal_actions") or [])
    )
    exposed_data = dict(data)
    if required_start_kind is not None:
        exposed_data["required_start_kind"] = required_start_kind
    if required_admin_action is not None:
        exposed_data["required_admin_action"] = required_admin_action
    return actions, exposed_data


class DishApplication:
    """Command dispatcher with one audit event per invocation."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        backend: CommandBackend,
        *,
        release_loader: Callable[..., ResolvedRelease],
        invocation_run_id: str | None = None,
    ) -> None:
        self.conn = conn
        self.backend = backend
        self.release_loader = release_loader
        self.invocation_run_id = str(invocation_run_id or "").strip() or None
        self.operation_service = OperationApplicationService(conn, backend)
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
        try:
            process_command_audit_repairs(self.conn)
        except Exception:
            pass
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
            result = handler(self, trace=trace, **arguments)
        except DishRuleError as exc:
            if trace.task_gid is None:
                trace.task_gid = _gid(exc.details.get("task_gid"))
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
        clean_reason = str(change_reason).strip() if change_reason is not None else None
        if kind != "change":
            if clean_level is not None or clean_reason is not None:
                raise DishRuleError(
                    "INVALID_ARGUMENT",
                    "change arguments are only valid for change operations",
                    rule="change_arguments_forbidden",
                )
            return None, None
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


def _step5_create(self, *, trace: CommandTrace, agent: str, title: str) -> dict[str, Any]:
    agent_family(agent)
    clean_title = _clean_required(title, rule="title_required", label="title")
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
    if view.get("required_admin_action") is not None:
        data["required_admin_action"] = view["required_admin_action"]
    trace.submission_id = operation_id
    trace.state = view["status"]
    return result_envelope(
        command="read", task_gid=task_gid, submission_id=operation_id,
        state=trace.state, allowed_actions=view["legal_actions"], data=data,
    )


def _current_operation_view(self, operation_id: str, *, schema=None) -> dict[str, Any]:
    return self.operation_service.authoritative_view(operation_id, schema=schema)


def _step5_inspect(self, *, trace: CommandTrace, agent: str, submission_id: str) -> dict[str, Any]:
    from .step5 import inspect_operation
    agent_family(agent)
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    exists = self.conn.execute("SELECT 1 FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if exists is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    data = inspect_operation(self.conn, operation_id)
    release = self._load_release(None)
    internal_view = _current_operation_view(self, operation_id, schema=release.schema)
    view = _exposed_view(internal_view)
    data["legal_next_actions"] = view["legal_actions"]
    data["authoritative_view"] = view
    if view.get("required_start_kind") is not None:
        data["required_start_kind"] = view["required_start_kind"]
    if view.get("required_admin_action") is not None:
        data["required_admin_action"] = view["required_admin_action"]
    trace.submission_id = operation_id
    trace.task_gid = data["operation"]["task_gid"]
    trace.state = view["status"]
    return result_envelope(command="inspect", task_gid=trace.task_gid, submission_id=operation_id, state=trace.state, allowed_actions=view["legal_actions"], data=data)


def _step5_start(self, *, trace: CommandTrace, agent: str, task_gid: str, kind: str, change_level: str | None = None, change_reason: str | None = None, run_id: str | None = None) -> dict[str, Any]:
    from .step5 import claim_operation, diagnostics_for, start_result_data
    from .task_store import read_complete_task
    agent_family(agent)
    task_gid = _clean_required(task_gid, rule="task_gid_required", label="task GID")
    trace.task_gid = task_gid
    change_level, change_reason = self._validate_start_arguments(kind=kind, change_level=change_level, change_reason=change_reason)
    role = "planning" if kind == "planning" else "research"
    release = self._load_release(role)
    _require_cooking_task(self._read_live_task(task_gid), task_gid)
    live = read_complete_task(self.backend, task_gid=task_gid, project_gid=COOKING_PROJECT_GID)
    registry = SectionRegistry.from_sections(self.backend.list_sections(COOKING_PROJECT_GID))
    if not is_protocol_managed(live.section_gid, registry):
        raise DishRuleError("UNMANAGED_TASK", f"task {task_gid} is in an excluded Cooking section", rule="task_in_excluded_section")
    diag = diagnostics_for(live, release)
    if kind == "planning":
        if live.notes:
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
    from .step6 import prepare_live
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
    run_id: str | None = None,
    independence_attestation: str | None = None,
) -> dict[str, Any]:
    if kind != "verification":
        return _step6_command_start(
            self, trace=trace, agent=agent, task_gid=task_gid, kind=kind,
            change_level=change_level, change_reason=change_reason, run_id=run_id,
        )
    from .step7 import verification_read
    agent_family(agent)
    task_gid = _clean_required(task_gid, rule="task_gid_required", label="task GID")
    trace.task_gid = task_gid
    row = self.conn.execute(
        "SELECT operation_id FROM operations WHERE task_gid = ? AND status = 'open' ORDER BY created_at DESC LIMIT 1",
        (task_gid,),
    ).fetchone()
    if row is None:
        raise DishRuleError("NOT_FOUND", "task has no open operation", rule="open_operation_missing")
    operation_id = row["operation_id"]
    release = self._load_release("verification")
    data, view = self.operation_service.current.start_verification(
        operation_id,
        lambda: verification_read(
            self.conn, self.backend, operation_id=operation_id, agent=agent,
            honest_root=release.root, run_id=run_id,
            independence_attestation=independence_attestation, schema=release.schema,
        ),
        schema=release.schema,
    )
    trace.submission_id = operation_id
    trace.state = view["status"]
    legal_actions, data = _exposed_result_contract(view, data)
    # Verification start binds the reviewed identity, but agents still need the
    # explicit inspect step to see lineage, provenance, and the authoritative
    # snapshot before deciding. Keep the decision actions visible as follow-ons.
    legal_actions = ["inspect", *(action for action in legal_actions if action != "inspect")]
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
    independence_attestation: str | None = None,
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
            independence_attestation=independence_attestation, schema=release.schema,
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

def _step8_approve(self, *, trace: CommandTrace, agent: str, model: str | None = None, submission_id: str, file_path: str | None = None, correction: str = "none", reviewed_identity: str | None = None, semantic_review_complete: bool = False, provenance_complete: bool = False, run_id: str | None = None, independence_attestation: str | None = None) -> dict[str, Any]:
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
        return _step7_command_approve(self, trace=trace, agent=agent, model=model, submission_id=submission_id, file_path=file_path, correction=correction, reviewed_identity=reviewed_identity, semantic_review_complete=semantic_review_complete, provenance_complete=provenance_complete, run_id=run_id, independence_attestation=independence_attestation)
    trace.validation_scope = scope_for_command("approve")
    from .step8 import approve_small
    release = self._load_release("verification")
    trace.submission_id = operation_id; trace.task_gid = exists["task_gid"]; trace.state = "open"
    data, view = self.operation_service.current.approve(
        operation_id,
        lambda: approve_small(self.conn, self.backend, operation_id=operation_id, agent=agent, model=model, file_path=file_path, reviewed_identity=_clean_required(reviewed_identity, rule="reviewed_identity_required", label="reviewed content identity"), semantic_review_complete=semantic_review_complete, provenance_complete=provenance_complete, run_id=run_id, independence_attestation=independence_attestation, schema=release.schema),
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
    trace.validation_scope = scope_for_command("reject")
    from .step8 import reject_route
    release = self._load_release("verification")
    trace.submission_id = operation_id; trace.task_gid = exists["task_gid"]; trace.state = "open"
    data, view = self.operation_service.current.reject(
        operation_id,
        lambda: reject_route(self.conn, self.backend, operation_id=operation_id, agent=agent, model=model, route=route, reason=reason, file_path=file_path, resume_status=resume_status, run_id=run_id, independence_attestation=independence_attestation, schema=release.schema, honest_root=release.root),
        schema=release.schema,
    )
    trace.state = view["status"]
    legal_actions, data = _exposed_result_contract(view, data)
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
    "create": _step5_create,
    "read": _step5_read,
    "inspect": _step5_inspect,
    "start": _step7_start,
    "prepare": _step6_prepare,
    "approve": _step8_approve,
    "reject": _step8_reject,
    "submit": _step9_submit,
}
