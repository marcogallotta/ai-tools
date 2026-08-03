"""Marco-only lifecycle commands for the separate ``dish-admin`` surface."""

from __future__ import annotations

from .audit_repair import attempt_command_audit_repairs, attach_audit_repair_warning

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .application_service import OperationApplicationService
from .command_support import reject_undeclared_arguments
from .database import (
    bind_abandonment_execution_in_transaction,
    create_abandonment_attempt_in_transaction,
    get_abandonment_attempt,
    complete_operation_step,
    declare_operation_step,
    record_audit,
    resolve_admin_abandonment_target,
    resolve_admin_operation_target,
)
from .invocation_audit import record_invocation_audit
from .transactions import immediate_transaction, savepoint_transaction
from .errors import DishRuleError
from .results import error_envelope, result_envelope
from .human_actions import PromptField, exact_action, relay_text, template_action


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
        repair_attempt = attempt_command_audit_repairs(
            self.conn, surface="dish-admin"
        )
        trace = AdminTrace(submission_id=arguments.get("submission_id"))
        handler = CURRENT_ADMIN_COMMAND_HANDLERS.get(command)
        try:
            if command in _OPERATION_TARGET_COMMANDS and arguments.get("submission_id"):
                arguments = dict(arguments)
                arguments["submission_id"] = resolve_admin_operation_target(
                    self.conn, arguments["submission_id"]
                )
                trace.submission_id = arguments["submission_id"]
            if command == "reconcile-abandonment" and arguments.get("abandonment_id"):
                arguments = dict(arguments)
                arguments["abandonment_id"] = resolve_admin_abandonment_target(
                    self.conn, arguments["abandonment_id"]
                )
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
            if exc.code == "BACKEND_UNCERTAIN" and (
                exc.details.get("execution_id")
                or exc.rule in {
                    "planning_reopen_outcome_uncertain",
                    "planning_reopen_reconciliation_required",
                }
            ):
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
        attach_audit_repair_warning(
            result, repair_attempt, surface="dish-admin"
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
        repair_attempt = attempt_command_audit_repairs(
            self.conn, surface="dish-admin"
        )
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
        attach_audit_repair_warning(
            result, repair_attempt, surface="dish-admin"
        )
        self._record_invocation(command, trace, result)
        return result

    def _record_invocation(
        self,
        command: str,
        trace: AdminTrace,
        result: Mapping[str, Any],
    ) -> None:
        request_id = (
            str(trace.audit_details.get("request_id") or "").strip() or None
        )
        if command == "reopen-planning" and request_id is not None:
            with immediate_transaction(self.conn, "planning_reopen_invocation"):
                existing = self.conn.execute(
                    """SELECT 1 FROM audit_events
                         WHERE event_type='dish-admin.reopen-planning'
                           AND json_extract(details, '$.request_id')=?
                         LIMIT 1""",
                    (request_id,),
                ).fetchone()
                if existing is not None:
                    return
                self._write_invocation(command, trace, result)
            return
        self._write_invocation(command, trace, result)

    def _write_invocation(
        self, command: str, trace: AdminTrace, result: Mapping[str, Any]
    ) -> None:
        record_invocation_audit(
            self.conn,
            surface="dish-admin",
            command=command,
            result=result,
            task_gid=trace.task_gid,
            submission_id=trace.submission_id,
            actor_role="marco",
            actor_run_id=self.invocation_run_id,
            audit_details=trace.audit_details,
        )






def _inspect_expired_or_released_cycle_lease(
    conn: sqlite3.Connection, *, operation_id: str, cycle_id: str, run_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT * FROM service_leases
             WHERE operation_id=? AND lease_kind='actor'
               AND context_cycle_id=? AND run_id=?
               AND (released_at IS NOT NULL OR julianday(expires_at)<=julianday('now'))
             ORDER BY actor_attempt_seq DESC LIMIT 1""",
        (operation_id, cycle_id, run_id),
    ).fetchone()


def _command_inspect(
    self, *, trace: AdminTrace, submission_id: str
) -> dict[str, Any]:
    """Return a compact, human-oriented diagnostic over authoritative state."""

    if self.backend is None or self.operation_service is None:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "admin inspection requires backend access",
            rule="admin_inspect_unavailable",
        )
    operation_id = _clean_required(
        submission_id, rule="operation_id_required", label="operation ID or task"
    )
    operation = self.conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if operation is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")

    from .constants import COOKING_PROJECT_GID
    from .task_store import read_complete_task
    from .commands import (
        _evidence_hold_continuation,
        _repair_destination_continuation,
        expose_authoritative_view,
    )

    release = None if self.release_loader is None else self.release_loader()
    schema = None if release is None else release.schema
    live = read_complete_task(
        self.backend, task_gid=operation["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    view = expose_authoritative_view(
        self.operation_service.authoritative_view(operation_id, schema=schema)
    )
    cycle = self.conn.execute(
        """SELECT * FROM verification_cycles WHERE operation_id=?
             ORDER BY cycle_number DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    active_lease = self.conn.execute(
        """SELECT * FROM service_leases
             WHERE operation_id=? AND lease_kind='actor' AND released_at IS NULL
               AND julianday(expires_at)>julianday('now')
             ORDER BY actor_attempt_seq DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    latest_lease = self.conn.execute(
        """SELECT * FROM service_leases
             WHERE operation_id=? AND lease_kind='actor'
             ORDER BY actor_attempt_seq DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    abandonment = self.conn.execute(
        """SELECT * FROM abandonment_attempts
             WHERE task_gid=? AND status!='completed'
             ORDER BY created_at DESC LIMIT 1""",
        (operation["task_gid"],),
    ).fetchone()

    actions: list[dict[str, Any]] = []
    administrative_blocker = False
    operator_instruction: str | None = None
    problem = "No administrative blocker is currently recorded."
    waiting_for = str(view.get("phase") or operation["phase"])

    if abandonment is not None:
        administrative_blocker = True
        decorated = _decorate_abandonment_result(
            self.conn, {"abandonment_id": abandonment["abandonment_id"]}
        )
        required = decorated.get("required_action")
        if isinstance(required, Mapping):
            human_action = required.get("human_action")
            if isinstance(human_action, Mapping):
                actions.append(dict(human_action))
        if abandonment["status"] == "awaiting_successor_claim":
            problem = "Abandonment is complete and a prepared successor is waiting for an agent claim."
        elif abandonment["status"] == "awaiting_hold_resolution":
            problem = "Abandonment preserved a governed hold that Marco must resolve."
        else:
            problem = "A permanent-run abandonment is active and must be reconciled."
    elif operation["status"] == "uncertain" or view.get("unresolved_attempts"):
        administrative_blocker = True
        spec = template_action(
            kind="reconcile-uncertain-effect",
            command="recover",
            positional=(operation_id,),
            options=(
                ("--outcome", "<inspect|not-applied|applied>"),
                ("--reason", "<what the live reread proved>"),
            ),
            prompt_fields=(
                PromptField("outcome", "Observed outcome", "<inspect|not-applied|applied>"),
                PromptField("reason", "What the live reread proved", "<what the live reread proved>"),
            ),
            summary="Reconcile an interrupted external effect.",
            effect="Record only what a fresh live reread proves.",
            after_success={"instruction": "Rerun dish-admin inspect."},
        )
        actions.append(spec.payload()["human_action"] | {"shell_command": spec.shell_command()})
        problem = "An external write or movement has an unresolved outcome."
    elif active_lease is not None:
        administrative_blocker = True
        spec = template_action(
            kind="expire-active-lease",
            command="expire-lease",
            positional=(active_lease["lease_id"],),
            options=(("--reason", "<why the active run is no longer available>"),),
            prompt_fields=(
                PromptField("reason", "Why the active run is unavailable", "<why the active run is no longer available>"),
            ),
            summary="Release the active lease only if its agent run is gone.",
            effect="This does not transfer cycle ownership; rerun inspect afterward to abandon the dead attempt if needed.",
            after_success={"instruction": "Rerun dish-admin inspect on this task."},
        )
        actions.append(spec.payload()["human_action"] | {"shell_command": spec.shell_command()})
        problem = "An agent run currently holds the operation lease."
    elif operation["phase"] in {"held_evidence", "held_human"}:
        administrative_blocker = True
        continuation = _evidence_hold_continuation(self.conn, operation_id, view)
        human_action = continuation.get("human_action")
        if isinstance(human_action, dict):
            actions.append(dict(human_action) | {
                "shell_command": continuation.get("admin_command")
                or continuation.get("admin_command_template")
            })
        problem = "The operation is waiting for Marco-supplied evidence or a binding decision."
    elif view.get("destination_repair_required"):
        administrative_blocker = True
        continuation = _repair_destination_continuation(operation_id, view)
        human_action = continuation.get("human_action")
        if isinstance(human_action, dict):
            actions.append(dict(human_action) | {
                "shell_command": continuation.get("admin_command")
                or continuation.get("admin_command_template")
            })
        problem = "The final destination move needs an explicit repair."
    elif (
        operation["phase"] == "await_verification"
        and cycle is not None
        and cycle["completed_at"] is None
        and str(cycle["run_id"] or "").strip()
    ):
        administrative_blocker = True
        lease = _inspect_expired_or_released_cycle_lease(
            self.conn,
            operation_id=operation_id,
            cycle_id=cycle["cycle_id"],
            run_id=cycle["run_id"],
        )
        if lease is not None:
            spec = template_action(
                kind="abandon-dead-verifier",
                command="abandon-operation",
                positional=(operation_id,),
                options=(
                    ("--lease-id", lease["lease_id"]),
                    ("--reason", "<why the verifier run is permanently unavailable>"),
                ),
                prompt_fields=(
                    PromptField("reason", "Why the verifier run is permanently unavailable", "<why the verifier run is permanently unavailable>"),
                ),
                summary="Abandon the dead verifier attempt.",
                effect="Preserve the candidate and prepare a fresh Verification continuation.",
                after_success={"instruction": "Follow the exact continuation returned by Dish."},
            )
            actions.append(spec.payload()["human_action"] | {"shell_command": spec.shell_command()})
            problem = "The open Verification cycle belongs to a prior run with no active lease."
        else:
            problem = (
                "The open Verification cycle is bound to another run, but Dish cannot identify "
                "one safe abandonment lease from current records."
            )
            operator_instruction = (
                "Dish cannot safely generate an abandonment command for this state. Do not guess "
                "a lease ID; run this inspect command with --verbose and report the result for a "
                "code or data repair."
            )
    elif latest_lease is not None:
        administrative_blocker = True
        try:
            lease = _select_abandonment_lease(
                self.conn, operation_id=operation_id, lease_id=None
            )
        except DishRuleError:
            problem = (
                "The operation has no active lease, but its historical attempts do not "
                "identify one safe automatic abandonment authority."
            )
            operator_instruction = (
                "Dish cannot safely generate an abandonment command. Do not choose a lease from "
                "raw records; rerun inspect with --verbose and report the result for repair."
            )
        else:
            spec = template_action(
                kind="abandon-dead-agent",
                command="abandon-operation",
                positional=(operation_id,),
                options=(
                    ("--lease-id", lease["lease_id"]),
                    ("--reason", "<why the agent run is permanently unavailable>"),
                ),
                prompt_fields=(
                    PromptField(
                        "reason",
                        "Why the agent run is permanently unavailable",
                        "<why the agent run is permanently unavailable>",
                    ),
                ),
                summary="Abandon the dead agent attempt.",
                effect="Preserve confirmed work and prepare the stage's safe successor.",
                after_success={
                    "instruction": "Follow the exact continuation returned by Dish."
                },
            )
            actions.append(spec.payload()["human_action"])
            problem = "The operation belongs to a prior agent run with no active lease."
    trace.submission_id = operation_id
    trace.task_gid = operation["task_gid"]
    trace.state = operation["status"]
    data = {
        "task_title": live.title,
        "task_gid": operation["task_gid"],
        "asana_url": f"https://app.asana.com/0/0/{operation['task_gid']}",
        "operation_id": operation_id,
        "operation_kind": operation["operation_kind"],
        "status": operation["status"],
        "phase": operation["phase"],
        "waiting_for": waiting_for,
        "problem": problem,
        "operator_instruction": operator_instruction,
        "administrative_blocker": administrative_blocker,
        "human_actions": actions,
        "agent_actions_now": (
            [] if administrative_blocker else list(view.get("legal_actions") or [])
        ),
        "service_lease": None if active_lease is None else {
            "lease_id": active_lease["lease_id"],
            "owner_id": active_lease["owner_id"],
            "run_id": active_lease["run_id"],
            "expires_at": active_lease["expires_at"],
        },
        "latest_actor_attempt": None if latest_lease is None else {
            "lease_id": latest_lease["lease_id"],
            "run_id": latest_lease["run_id"],
            "released_at": latest_lease["released_at"],
            "expires_at": latest_lease["expires_at"],
            "cycle_id": latest_lease["context_cycle_id"],
        },
        "verification_cycle": None if cycle is None else {
            "cycle_id": cycle["cycle_id"],
            "run_id": cycle["run_id"],
            "verifier_agent": cycle["verifier_agent"],
            "completed_at": cycle["completed_at"],
            "outcome": cycle["outcome"],
            "route": cycle["route"],
        },
        "authoritative_view": view,
    }
    return result_envelope(
        command="inspect",
        task_gid=operation["task_gid"],
        submission_id=operation_id,
        state=operation["status"],
        data=data,
    )

def _command_holds(self, *, trace: AdminTrace) -> dict[str, Any]:
    if self.backend is None or self.operation_service is None:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "hold listing requires backend access",
            rule="hold_listing_unavailable",
        )
    from .commands import _evidence_hold_continuation, expose_authoritative_view
    from .constants import COOKING_PROJECT_GID
    from .task_gateway import read_complete_task

    release = None if self.release_loader is None else self.release_loader()
    schema = None if release is None else release.schema
    rows = self.conn.execute(
        """SELECT * FROM operations
             WHERE status='open' AND phase IN ('held_evidence','held_human')
             ORDER BY created_at, operation_id"""
    ).fetchall()
    holds = []
    for op in rows:
        view = expose_authoritative_view(
            self.operation_service.authoritative_view(op["operation_id"], schema=schema)
        )
        continuation = _evidence_hold_continuation(
            self.conn, op["operation_id"], view
        )
        pre = self.conn.execute(
            """SELECT intended_json FROM operation_steps
                 WHERE operation_id=? AND step_name='research_preconstruction_hold'
                   AND completed_at IS NOT NULL""",
            (op["operation_id"],),
        ).fetchone()
        cycle = self.conn.execute(
            """SELECT * FROM verification_cycles WHERE operation_id=?
                 ORDER BY cycle_number DESC LIMIT 1""",
            (op["operation_id"],),
        ).fetchone()
        if pre is not None:
            payload = json.loads(pre["intended_json"])
            route = payload.get("route")
            hold_class = (
                "research_preconstruction_evidence"
                if route == "evidence"
                else "research_preconstruction_human"
            )
            question = payload.get("reason")
            cycle_id = None
            hold_identity = op["expected_identity"]
        elif cycle is not None and cycle["outcome"] == "verification-hold":
            hold_class = "verification_two_pass"
            question = None
            cycle_id = cycle["cycle_id"]
            hold_identity = cycle["hold_identity"]
        else:
            route = None if cycle is None else cycle["route"]
            hold_class = (
                "verification_evidence"
                if route == "evidence"
                else "verification_human"
            )
            cycle_id = None if cycle is None else cycle["cycle_id"]
            hold_identity = None if cycle is None else cycle["hold_identity"]
            question = None
        live = read_complete_task(
            self.backend,
            task_gid=op["task_gid"],
            project_gid=COOKING_PROJECT_GID,
        )
        if question is None:
            try:
                from .task_document import parse_task_document

                doc = parse_task_document(f"{live.title}\n{live.notes}")
                question = doc.state.values.get("Status detail")
            except Exception:
                question = None
        holds.append(
            {
                "hold_class": hold_class,
                "required_admin_action": continuation.get("required_admin_action"),
                "task_title": live.title,
                "task_gid": op["task_gid"],
                "asana_url": f"https://app.asana.com/0/0/{op['task_gid']}",
                "operation_id": op["operation_id"],
                "cycle_id": cycle_id,
                "hold_identity": hold_identity,
                "question": question,
                "phase": op["phase"],
                "created_at": op["created_at"],
                "human_action": continuation.get("human_action"),
                "admin_command": continuation.get("admin_command"),
                "admin_command_is_template": continuation.get(
                    "admin_command_is_template"
                ),
                "admin_command_template": continuation.get(
                    "admin_command_template"
                ),
                "after_resolution": continuation.get("after_resolution"),
            }
        )
    return result_envelope(
        command="holds", state="ok", data={"holds": holds, "count": len(holds)}
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
    if clean_reason.startswith("<") and clean_reason.endswith(">"):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "reopen reason still contains the unfilled command placeholder",
            rule="planning_reopen_reason_placeholder",
        )
    trace.task_gid = clean
    if self.backend is None or self.release_loader is None:
        raise DishRuleError(
            "INTERNAL_ERROR", "planning reopen backend is unavailable",
            rule="planning_reopen_backend_unavailable",
        )
    from .database import planning_reopen_blocker_for_task
    from .task_store import planning_reopen_recovery_details
    blocker = planning_reopen_blocker_for_task(self.conn, task_gid=clean)
    if blocker is not None:
        details = planning_reopen_recovery_details(blocker)
        details["request_status"] = blocker["request_status"]
        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "an interrupted Planning reopen must be reconciled before another reopen",
            rule="planning_reopen_reconciliation_required",
            retryable=False,
            details=details,
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
    trace.audit_details.update({
        "request_id": self.invocation_request_id,
        "reason": clean_reason,
    })
    try:
        reopened, attempt_id = reopen_completed_task_for_planning(
            self.conn,
            self.backend,
            task_gid=clean,
            project_gid=COOKING_PROJECT_GID,
            reason=clean_reason,
            actor_run_id=self.invocation_run_id,
            request_id=self.invocation_request_id,
        )
    except sqlite3.IntegrityError:
        blocker = planning_reopen_blocker_for_task(self.conn, task_gid=clean)
        if blocker is None:
            raise
        details = planning_reopen_recovery_details(blocker)
        details["request_status"] = blocker["request_status"]
        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "an interrupted Planning reopen must be reconciled before another reopen",
            rule="planning_reopen_reconciliation_required",
            retryable=False,
            details=details,
        )
    except DishRuleError:
        if self.invocation_request_id:
            from .database import planning_reopen_attempt_by_request
            attempt = planning_reopen_attempt_by_request(
                self.conn, request_id=self.invocation_request_id
            )
            if attempt is not None:
                trace.audit_details.update({
                    "attempt_id": attempt["attempt_id"],
                    "expected_identity": attempt["expected_identity"],
                    "section_gid": attempt["expected_section_gid"],
                })
        raise
    trace.audit_details.update({
        "attempt_id": attempt_id,
        "request_id": self.invocation_request_id,
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


# Step 8 Marco-only Verification hold reopen.
def _step8_admin_reopen(self, *, trace: AdminTrace, submission_id: str, category: str, before: str, after: str, editor: str, model: str, date: str, run_id: str | None = None, file_path: str | None = None) -> dict[str, Any]:
    if self.backend is None:
        raise DishRuleError("INTERNAL_ERROR", "admin backend is required", rule="backend_required")
    from .step8 import reopen_verification_hold
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    row = self.conn.execute("SELECT task_gid FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if row is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    trace.submission_id = operation_id; trace.task_gid = row["task_gid"]; trace.state = "open"
    release = None if self.release_loader is None else self.release_loader()
    schema = None if release is None else release.schema
    data, view = self.operation_service.current.reopen_verification_hold(
        operation_id,
        lambda: reopen_verification_hold(
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
def _step9_admin_recover(
    self,
    *,
    trace: AdminTrace,
    submission_id: str,
    outcome: str,
    reason: str,
) -> dict[str, Any]:
    operation_id = _clean_required(
        submission_id, rule="operation_id_required", label="operation ID"
    )
    clean_outcome = str(outcome or "").strip()
    if not clean_outcome:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "recovery outcome is required",
            rule="recovery_outcome_required",
            details={"field": "outcome"},
        )
    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "recovery reason is required",
            rule="recovery_reason_required",
            details={"field": "reason"},
        )
    if clean_reason.startswith("<") and clean_reason.endswith(">"):
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "recovery reason still contains the unfilled command placeholder",
            rule="recovery_reason_placeholder",
            details={"field": "reason"},
        )
    if clean_outcome not in {"inspect", "not-applied", "applied"}:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "recovery outcome must be inspect, not-applied, or applied",
            rule="recovery_outcome_invalid",
            details={"field": "outcome"},
        )
    exists = self.conn.execute(
        "SELECT task_gid FROM operations WHERE operation_id = ?", (operation_id,)
    ).fetchone()
    if exists is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    if self.backend is None:
        raise DishRuleError("INTERNAL_ERROR", "admin backend is required", rule="backend_required")
    from .step9 import recover_operation
    trace.submission_id = operation_id
    trace.task_gid = exists["task_gid"]
    data, view = self.operation_service.current.recover(
        operation_id,
        lambda: recover_operation(
            self.conn,
            self.backend,
            operation_id=operation_id,
            requested_outcome=clean_outcome,
            reason=clean_reason,
        ),
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
    expected_task_gid: str | None = None,
    expected_cycle_id: str | None = None,
    expected_hold_identity: str | None = None,
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
            expected_task_gid=expected_task_gid, expected_cycle_id=expected_cycle_id,
            expected_hold_identity=expected_hold_identity,
        ),
        schema=release.schema,
    )
    trace.state = view["status"]
    return result_envelope(
        command=action, task_gid=trace.task_gid, submission_id=operation_id,
        state=view["status"], allowed_actions=view["legal_actions"], data=data,
    )


def _command_supply_evidence(self, *, trace: AdminTrace, submission_id: str, detail: str, resume_status: str, file_path: str | None = None, editor: str | None = None, model: str | None = None, run_id: str | None = None, expected_task_gid: str | None = None, expected_cycle_id: str | None = None, expected_hold_identity: str | None = None) -> dict[str, Any]:
    return _resolve_protocol_hold(
        self, trace=trace, submission_id=submission_id, resolution_kind="evidence", expected_task_gid=expected_task_gid, expected_cycle_id=expected_cycle_id, expected_hold_identity=expected_hold_identity,
        detail=detail, resume_status=resume_status, file_path=file_path, editor=editor, model=model, run_id=run_id,
    )


def _command_record_human_decision(self, *, trace: AdminTrace, submission_id: str, detail: str, resume_status: str, file_path: str | None = None, editor: str | None = None, model: str | None = None, run_id: str | None = None, expected_task_gid: str | None = None, expected_cycle_id: str | None = None, expected_hold_identity: str | None = None) -> dict[str, Any]:
    return _resolve_protocol_hold(
        self, trace=trace, submission_id=submission_id, resolution_kind="human_review", expected_task_gid=expected_task_gid, expected_cycle_id=expected_cycle_id, expected_hold_identity=expected_hold_identity,
        detail=detail, resume_status=resume_status, file_path=file_path, editor=editor, model=model, run_id=run_id,
    )




def _command_resolved(self, *, trace: AdminTrace, submission_id: str) -> dict[str, Any]:
    if self.backend is None or self.release_loader is None:
        raise DishRuleError("INTERNAL_ERROR", "Verification hold release requires backend and Honest release", rule="hold_resolution_unavailable")
    from .step8 import resolve_verification_hold
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    row = self.conn.execute("SELECT task_gid FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if row is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    release = self.release_loader()
    trace.submission_id = operation_id
    trace.task_gid = row["task_gid"]
    data, view = self.operation_service.current.resolve_hold(
        operation_id, "resolved",
        lambda: resolve_verification_hold(self.conn, self.backend, operation_id=operation_id, schema=release.schema),
        schema=release.schema,
    )
    trace.state = view["status"]
    return result_envelope(command="resolved", task_gid=trace.task_gid, submission_id=operation_id, state=view["status"], allowed_actions=view["legal_actions"], data=data)


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


def _abandonment_reconcile_action(abandonment_id: str) -> dict[str, Any]:
    spec = exact_action(
        kind="reconcile-abandonment",
        command="reconcile-abandonment",
        positional=(abandonment_id,),
        summary="Continue a previously interrupted or blocked abandonment.",
        effect="Reclassify the persisted abandonment and prepare its safe continuation.",
        after_success={
            "start_new_operation": False,
            "instruction": "Refresh Dish and follow the exact continuation returned.",
        },
    )
    payload = spec.payload()
    return {
        "surface": "private-admin",
        "command": "reconcile-abandonment",
        "arguments": {"abandonment_id": abandonment_id},
        **payload,
        "relay_text": relay_text(
            spec,
            instruction="Wait for confirmation it succeeded, then refresh the authoritative Dish action.",
        ),
        "after_success": dict(spec.after_success or {}),
    }


def _abandonment_hold_action(conn: sqlite3.Connection, operation: sqlite3.Row) -> dict[str, Any]:
    if operation["phase"] == "held_evidence":
        command = "supply-evidence"
        detail = "<summarize the supplied evidence>"
        summary = "Record Marco-supplied evidence and release the preserved hold."
    else:
        command = "record-human-decision"
        detail = "<summarize the human decision and reasoning>"
        summary = "Record Marco's binding decision and release the preserved hold."
    cycle = conn.execute(
        """SELECT cycle_id,hold_identity FROM verification_cycles
             WHERE operation_id=? AND completed_at IS NOT NULL
             ORDER BY cycle_number DESC LIMIT 1""",
        (operation["operation_id"],),
    ).fetchone()
    options: list[tuple[str, object | None]] = [
        ("--detail", detail),
        ("--resume-status", "pending-research"),
        ("--expected-task-gid", operation["task_gid"]),
    ]
    if cycle is not None and cycle["hold_identity"]:
        options.extend((
            ("--expected-cycle-id", cycle["cycle_id"]),
            ("--expected-hold-identity", cycle["hold_identity"]),
        ))
    spec = template_action(
        kind=command,
        command=command,
        positional=(operation["operation_id"],),
        options=tuple(options),
        prompt_fields=(PromptField("detail", "Decision or evidence detail", detail),),
        summary=summary,
        effect="Resume the preserved Research operation without creating a replacement operation.",
        after_success={
            "start_new_operation": False,
            "instruction": "Refresh Dish and follow the exact continuation returned.",
        },
    )
    return {
        "surface": "private-admin",
        "command": command,
        "arguments": {
            "submission_id": operation["operation_id"],
            "resume_status": "pending-research",
            "expected_task_gid": operation["task_gid"],
            **({
                "expected_cycle_id": cycle["cycle_id"],
                "expected_hold_identity": cycle["hold_identity"],
            } if cycle is not None and cycle["hold_identity"] else {}),
        },
        **spec.payload(),
        "relay_text": relay_text(
            spec,
            instruction="Wait for confirmation it succeeded, then refresh the authoritative Dish action.",
        ),
        "after_success": dict(spec.after_success or {}),
    }


def _decorate_abandonment_result(
    conn: sqlite3.Connection, result: Mapping[str, Any]
) -> dict[str, Any]:
    data = dict(result)
    abandonment_id = str(data.get("abandonment_id") or "").strip()
    if not abandonment_id:
        return data
    abandonment = get_abandonment_attempt(conn, abandonment_id)
    data["abandonment"] = {key: abandonment[key] for key in abandonment.keys()}
    if data.get("required_action"):
        return data
    if abandonment["status"] == "blocked_manual_reconciliation":
        data["required_action"] = _abandonment_reconcile_action(abandonment_id)
    elif abandonment["status"] == "awaiting_hold_resolution":
        operation = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?",
            (abandonment["source_operation_id"],),
        ).fetchone()
        if operation is not None:
            data["required_action"] = _abandonment_hold_action(conn, operation)
    return data


def _select_abandonment_lease(
    conn: sqlite3.Connection, *, operation_id: str, lease_id: str | None
) -> sqlite3.Row:
    operation = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if operation is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    if operation["status"] not in {"open", "uncertain"} or operation["phase"] == "terminal":
        raise DishRuleError(
            "WRONG_STATE",
            "only an active operation can be abandoned",
            rule="abandonment_source_not_active",
            details={"status": operation["status"], "phase": operation["phase"]},
        )
    clean_lease_id = str(lease_id or "").strip() or None
    if clean_lease_id is not None:
        rows = conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=?", (clean_lease_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT * FROM service_leases
                 WHERE operation_id=? AND lease_kind='actor'
                   AND (released_at IS NOT NULL OR julianday(expires_at)<=julianday('now'))
                 ORDER BY actor_attempt_seq DESC""",
            (operation_id,),
        ).fetchall()
    eligible = []
    for row in rows:
        expired_or_released = bool(
            row["released_at"] is not None
            or conn.execute(
                "SELECT julianday(?)<=julianday('now')", (row["expires_at"],)
            ).fetchone()[0]
        )
        superseded = conn.execute(
            """SELECT 1
                 FROM service_leases AS later
                WHERE later.task_gid=?
                  AND later.lease_kind='actor'
                  AND later.actor_attempt_seq > ?
                  AND EXISTS (
                      SELECT 1 FROM operation_actor_facts AS fact
                       WHERE fact.operation_id=?
                         AND fact.run_id=later.run_id
                  )""",
            (operation["task_gid"], row["actor_attempt_seq"], operation_id),
        ).fetchone()
        if (
            row["operation_id"] == operation_id
            and row["task_gid"] == operation["task_gid"]
            and row["lease_kind"] == "actor"
            and row["actor_attempt_seq"] is not None
            and superseded is None
            and expired_or_released
        ):
            eligible.append(row)
    if len(eligible) != 1:
        candidates = [
            {
                "lease_id": row["lease_id"],
                "run_id": row["run_id"],
                "cycle_id": row["context_cycle_id"],
                "actor_attempt_seq": row["actor_attempt_seq"],
                "released_at": row["released_at"],
                "expires_at": row["expires_at"],
            }
            for row in rows
            if row["lease_kind"] == "actor"
        ]
        spec = exact_action(
            kind="inspect-abandonment-authority",
            command="inspect",
            positional=(operation_id,),
            summary="Inspect which dead attempt can safely authorize abandonment.",
            effect=(
                "Show the exact cycle owner and one safe abandonment command; do not guess "
                "between lease IDs."
            ),
            after_success={"instruction": "Run the recommended action returned by inspect."},
        )
        raise DishRuleError(
            "CONFLICT",
            (
                "the supplied lease cannot authorize abandonment"
                if clean_lease_id is not None
                else "Dish cannot safely choose one abandonment lease without inspection"
            ),
            rule=(
                "abandonment_lease_not_eligible"
                if clean_lease_id is not None
                else "abandonment_lease_selection_required"
            ),
            details={
                "candidate_leases": candidates,
                "required_admin_action": "inspect",
                **spec.payload(),
                "directive": relay_text(
                    spec,
                    instruction=(
                        "Wait for Marco to run the one recommended abandonment action. "
                        "Do not ask him to choose a lease ID from raw records."
                    ),
                ),
            },
        )
    return eligible[0]


def _claimed_admin_execution(
    conn: sqlite3.Connection, *, operation_id: str
) -> sqlite3.Row:
    row = conn.execute(
        """SELECT execution.*
             FROM operation_execution_claims AS claim
             JOIN operation_executions AS execution
               ON execution.execution_id=claim.execution_id
            WHERE claim.operation_id=?""",
        (operation_id,),
    ).fetchone()
    if (
        row is None
        or row["command"] not in {"abandon-operation", "reconcile-abandonment"}
        or row["status"] not in {"started", "uncertain"}
    ):
        raise DishRuleError(
            "CONFLICT",
            "abandonment command lacks exact operation execution authority",
            rule="abandonment_execution_binding_missing",
        )
    return row


def _unsettled_abandonment_execution(
    conn: sqlite3.Connection, *, operation_id: str, current_execution_id: str | None
) -> sqlite3.Row | None:
    """Return the one unresolved abandonment execution, including post-settlement crashes."""

    if current_execution_id is not None:
        row = conn.execute(
            "SELECT * FROM operation_executions WHERE execution_id=?",
            (current_execution_id,),
        ).fetchone()
        if row is not None and row["status"] in {"started", "uncertain"}:
            return row
    rows = conn.execute(
        """SELECT execution.*
             FROM operation_executions AS execution
             JOIN operation_execution_claims AS claim
               ON claim.execution_id=execution.execution_id
            WHERE execution.operation_id=?
              AND execution.command IN ('abandon-operation','reconcile-abandonment')
              AND execution.status IN ('started','uncertain')
            ORDER BY execution.created_at DESC""",
        (operation_id,),
    ).fetchall()
    if len(rows) > 1:
        raise DishRuleError(
            "CONFLICT",
            "multiple unresolved abandonment executions require manual repair",
            rule="abandonment_execution_ambiguous",
            details={"execution_ids": [row["execution_id"] for row in rows]},
        )
    return None if not rows else rows[0]


def _stored_abandonment_result(row: sqlite3.Row) -> dict[str, Any]:
    try:
        stored = json.loads(row["latest_result_json"] or "{}")
    except (TypeError, ValueError):
        stored = {}
    result = dict(stored) if isinstance(stored, dict) else {}
    result.setdefault("abandonment_id", row["abandonment_id"])
    return result


def _command_abandon_operation(
    self,
    *,
    trace: AdminTrace,
    submission_id: str,
    reason: str,
    lease_id: str | None = None,
) -> dict[str, Any]:
    if self.operation_service is None or self.operation_service.current is None:
        raise DishRuleError(
            "PROTOCOL_INCOMPATIBLE",
            "permanent attempt abandonment requires the current shared workflow service",
            rule="shared_service_required",
        )
    operation_id = _clean_required(
        submission_id, rule="operation_id_required", label="operation ID"
    )
    clean_reason = _clean_required(
        reason, rule="abandonment_reason_required", label="abandonment reason"
    )
    operation = self.conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if operation is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    trace.submission_id = operation_id
    trace.task_gid = operation["task_gid"]
    trace.state = operation["status"]

    def execute() -> dict[str, Any]:
        lease = _select_abandonment_lease(
            self.conn, operation_id=operation_id, lease_id=lease_id
        )
        existing = self.conn.execute(
            """SELECT * FROM abandonment_attempts
                 WHERE source_operation_id=? AND source_lease_id=?
                   AND abandoned_owner_id=? AND abandoned_run_id=?
                   AND attempt_cycle_id IS ?""",
            (
                operation_id,
                lease["lease_id"],
                lease["owner_id"],
                lease["run_id"],
                lease["context_cycle_id"],
            ),
        ).fetchone()
        if existing is not None:
            if existing["status"] == "completed":
                return _stored_abandonment_result(existing)
            raise DishRuleError(
                "CONFLICT",
                "this actor attempt already has an active abandonment",
                rule="abandonment_attempt_exists",
                details={
                    "abandonment_id": existing["abandonment_id"],
                    "required_admin_action": "reconcile-abandonment",
                    **_abandonment_reconcile_action(existing["abandonment_id"]),
                },
            )
        abandonment_id = str(uuid.uuid4())
        execution = _claimed_admin_execution(
            self.conn, operation_id=operation_id
        )
        with immediate_transaction(self.conn, "create_abandonment_attempt"):
            create_abandonment_attempt_in_transaction(
                self.conn,
                abandonment_id=abandonment_id,
                task_gid=operation["task_gid"],
                source_operation_id=operation_id,
                source_lease_id=lease["lease_id"],
                abandoned_owner_id=lease["owner_id"],
                abandoned_run_id=lease["run_id"],
                attempt_cycle_id=lease["context_cycle_id"],
                current_execution_id=execution["execution_id"],
                reason=clean_reason,
            )
        return self.operation_service.current.settle_abandonment_frontier(
            abandonment_id, reason=clean_reason
        )

    data, view = self.operation_service.current.abandon_operation(
        operation_id, execute
    )
    data = _decorate_abandonment_result(self.conn, data)
    required = data.get("required_action")
    actions = [required["command"]] if isinstance(required, dict) and required.get("surface") == "connected-agent" else []
    trace.state = view.get("status") or trace.state
    return result_envelope(
        command="abandon-operation",
        task_gid=operation["task_gid"],
        submission_id=operation_id,
        state=trace.state,
        allowed_actions=actions,
        data=data,
    )


def _command_reconcile_abandonment(
    self, *, trace: AdminTrace, abandonment_id: str
) -> dict[str, Any]:
    if self.operation_service is None or self.operation_service.current is None:
        raise DishRuleError(
            "PROTOCOL_INCOMPATIBLE",
            "abandonment reconciliation requires the current shared workflow service",
            rule="shared_service_required",
        )
    clean_id = _clean_required(
        abandonment_id, rule="abandonment_id_required", label="abandonment ID"
    )
    abandonment = get_abandonment_attempt(self.conn, clean_id)
    operation_id = abandonment["source_operation_id"]
    operation = self.conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    trace.submission_id = operation_id
    trace.task_gid = abandonment["task_gid"]
    trace.state = None if operation is None else operation["status"]
    prior_execution = _unsettled_abandonment_execution(
        self.conn,
        operation_id=operation_id,
        current_execution_id=abandonment["current_execution_id"],
    )
    settled = abandonment["status"] in {
        "completed",
        "awaiting_successor_claim",
        "awaiting_hold_resolution",
    }
    if settled and prior_execution is None:
        data = _decorate_abandonment_result(
            self.conn, _stored_abandonment_result(abandonment)
        )
        required = data.get("required_action")
        actions = [required["command"]] if isinstance(required, dict) and required.get("surface") == "connected-agent" else []
        return result_envelope(
            command="reconcile-abandonment",
            task_gid=abandonment["task_gid"],
            submission_id=operation_id,
            state=trace.state,
            allowed_actions=actions,
            data=data,
        )

    def execute() -> dict[str, Any]:
        if settled:
            return _stored_abandonment_result(
                get_abandonment_attempt(self.conn, clean_id)
            )
        execution = _claimed_admin_execution(
            self.conn, operation_id=operation_id
        )
        with immediate_transaction(self.conn, "bind_abandonment_execution"):
            bind_abandonment_execution_in_transaction(
                self.conn,
                abandonment_id=clean_id,
                execution_id=execution["execution_id"],
            )
        return self.operation_service.current.settle_abandonment_frontier(
            clean_id, reason=abandonment["reason"]
        )

    if (
        prior_execution is not None
        and prior_execution["status"] in {"started", "uncertain"}
    ):
        data, view = self.operation_service.current.resume_abandonment_execution(
            operation_id, clean_id, prior_execution["execution_id"], execute
        )
        data = dict(data)
        data["resumed_admin_execution"] = {
            "execution_id": prior_execution["execution_id"],
            "command": prior_execution["command"],
            "request_id": prior_execution["request_id"],
        }
    else:
        data, view = self.operation_service.current.reconcile_abandonment(
            operation_id, execute
        )
    data = _decorate_abandonment_result(self.conn, data)
    required = data.get("required_action")
    actions = [required["command"]] if isinstance(required, dict) and required.get("surface") == "connected-agent" else []
    trace.state = view.get("status") or trace.state
    return result_envelope(
        command="reconcile-abandonment",
        task_gid=abandonment["task_gid"],
        submission_id=operation_id,
        state=trace.state,
        allowed_actions=actions,
        data=data,
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
        with savepoint_transaction(self.conn, "operation_cancel"):
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

_OPERATION_TARGET_COMMANDS = {
    "holds", "inspect", "reopen", "recover", "repair-destination", "supply-evidence",
    "record-human-decision", "resolved", "authorize-governed-change", "discard",
    "abandon-operation",
}

CURRENT_ADMIN_COMMAND_HANDLERS = {
    "inspect": _command_inspect,
    "migrate": _step5_admin_migrate,
    "reopen-planning": _step5_admin_reopen_planning,
    "reopen": _step8_admin_reopen,
    "recover": _step9_admin_recover,
    "repair-destination": _step9_admin_repair_destination,
    "holds": _command_holds,
    "supply-evidence": _command_supply_evidence,
    "record-human-decision": _command_record_human_decision,
    "resolved": _command_resolved,
    "authorize-governed-change": _command_authorize_governed_change,
    "discard": _current_operation_discard,
    "abandon-operation": _command_abandon_operation,
    "reconcile-abandonment": _command_reconcile_abandonment,
}
