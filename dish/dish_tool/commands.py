"""Agent-facing command behavior for the guarded ``dish`` CLI."""

from __future__ import annotations

from .audit_repair import attempt_command_audit_repairs, attach_audit_repair_warning

import asyncio
import inspect
import json
import sqlite3
from typing import Any, Callable, Mapping

from .application_service import OperationApplicationService
from .command_identity import CONNECTED_AGENT_COMMANDS
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
from .human_actions import PromptField, exact_action, relay_text, template_action
from .review_queue import human_review_consequence_metadata
from .database import resolve_signoff_cycle_for_identity
from .task_document import document_shape
from .workflow_policy import RestingTaskSnapshot, required_resting_start_kind
from .validation_scope import scope_for_command


def _exposed_action_contract(
    actions: list[str] | tuple[str, ...],
) -> tuple[list[str], str | None, str | None]:
    """Project internal workflow actions onto accepted connected command metadata."""
    from .admin_command_spec import ADMIN_COMMANDS

    required_start_kind = "verification" if "verify" in actions else None
    translated = ["start" if action == "verify" else action for action in actions]
    exposed = [action for action in translated if action in CONNECTED_AGENT_COMMANDS]
    required_admin_action = next(
        (action for action in translated if action in ADMIN_COMMANDS),
        None,
    )
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
        exposed_data.get("required_admin_action")
        or required_admin_action
        or view.get("required_admin_action")
    )
    if required_admin_action is not None:
        exposed_data["required_admin_action"] = required_admin_action
    if "semantic_proposal" in view:
        exposed_data["semantic_proposal"] = view["semantic_proposal"]
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


def _resting_task_required_start_kind(
    conn: sqlite3.Connection, *, live, diagnostics: Mapping[str, Any]
) -> str | None:
    """Derive ordinary start authority for a managed task with no active operation."""
    shape = document_shape(live.notes)
    parsed = diagnostics.get("parsed")
    canonical_status = None
    signed_baseline_bound = False
    if isinstance(parsed, dict):
        state = parsed.get("state")
        if isinstance(state, dict):
            canonical_status = state.get("Status")
        if (
            canonical_status == "ready"
            and not diagnostics.get("validation")
            and not diagnostics.get("migration_required")
        ):
            signed_baseline_bound = (
                resolve_signoff_cycle_for_identity(
                    conn, task_gid=live.gid, identity=live.identity
                )
                is not None
            )
    return required_resting_start_kind(
        RestingTaskSnapshot(
            document_shape=shape,
            structurally_valid=not bool(diagnostics.get("validation")),
            migration_required=bool(diagnostics.get("migration_required")),
            completed=bool(live.completed),
            canonical_status=None if canonical_status is None else str(canonical_status),
            signed_baseline_bound=signed_baseline_bound,
        )
    )


def _active_operation_id(conn: sqlite3.Connection, *, task_gid: str) -> str | None:
    row = conn.execute(
        """SELECT operation_id FROM operations
             WHERE task_gid=? AND status IN ('open','uncertain')
             ORDER BY created_at DESC LIMIT 1""",
        (task_gid,),
    ).fetchone()
    return None if row is None else str(row["operation_id"])


def _admin_resolver(action: str | None) -> str | None:
    if action is None:
        return None
    return f"Marco/admin {action}"


_AGENT_CORRECTABLE_FINDING_KINDS = {
    "syntax", "agent-correctable", "illegal-combination",
}


def _attach_validation_retry_guidance(
    result: dict[str, Any], *, command: str
) -> None:
    """Describe a safe corrected retry only when live policy still permits it."""
    if result.get("code") != "VALIDATION_FAILED":
        return
    findings = [
        item for item in result.get("errors", [])
        if isinstance(item, Mapping) and item.get("kind") in _AGENT_CORRECTABLE_FINDING_KINDS
    ]
    if not findings or command not in result.get("allowed_actions", []):
        return
    result.setdefault("data", {})["retry"] = {
        "mode": "correct_then_retry",
        "action": command,
        "same_operation": True,
        "same_cycle": command in {"approve", "reject"},
        "fresh_request_id": True,
        "mutation_occurred": False,
        "instruction": (
            f"Correct the submitted candidate using the validation findings, then retry `{command}` "
            "on this same open operation"
            + (" and Verification cycle." if command in {"approve", "reject"} else ".")
        ),
    }


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


def _verification_hold_continuation(operation_id: str, view: Mapping[str, Any]) -> dict[str, Any]:
    """Describe the private release continuation for a Verification hold."""

    spec = exact_action(
        kind="release-verification-hold",
        command="resolved",
        positional=(operation_id,),
        summary="Release the unchanged Verification hold.",
        effect="Open a fresh Verification round without approving or editing the candidate.",
        after_success={"agent_action": "start", "required_start_kind": "verification"},
    )
    command = spec.shell_command()
    return {
        "phase": str(view.get("phase") or ""),
        "submission_id": operation_id,
        "existing_submission_id": operation_id,
        "required_admin_action": "resolved",
        "resolver": "Marco/admin resolved",
        "continuation_surface": "private-admin",
        "connected_action_available": False,
        **spec.payload(),
        "directive": relay_text(
            spec,
            instruction=(
                "Wait for confirmation it succeeded, then start a fresh Verification round "
                "on this same submission."
            ),
        ),
        "after_resolution": {
            "legal_actions": ["verify"],
            "phase": "await_verification",
        },
    }


def _repair_destination_continuation(operation_id: str, view: Mapping[str, Any]) -> dict[str, Any]:
    """Describe the reachable private continuation for a stuck destination move."""

    spec = template_action(
        kind="repair-destination",
        command="repair-destination",
        positional=(operation_id,),
        options=(
            ("--destination-section-gid", "<SECTION_GID>"),
            ("--reason", "<why this destination is correct>"),
        ),
        prompt_fields=(
            PromptField("destination_section_gid", "Destination section GID", "<SECTION_GID>"),
            PromptField("reason", "Why this destination is correct", "<why this destination is correct>"),
        ),
        summary="Repair the recorded destination after a failed final move.",
        effect="Change only the approved destination binding, then allow submit to resume.",
        after_success={"agent_action": "submit"},
    )
    command = spec.shell_command()
    directive = relay_text(
        spec,
        instruction=(
            "Wait for confirmation it succeeded; do not start a new operation. Resume this "
            "same submission with `submit`."
        ),
    )
    return {
        "phase": str(view.get("phase") or ""),
        "submission_id": operation_id,
        "existing_submission_id": operation_id,
        "required_admin_action": "repair-destination",
        "resolver": "Marco/admin repair-destination",
        "continuation_surface": "private-admin",
        "connected_action_available": False,
        **spec.payload(),
        "directive": directive,
        "after_resolution": {"legal_actions": ["submit"], "phase": "await_submission"},
    }


def _evidence_hold_continuation(
    conn: sqlite3.Connection, operation_id: str, view: Mapping[str, Any]
) -> dict[str, Any]:
    """Describe the reachable private continuation for an Evidence or Human Review hold."""

    admin_action = view.get("required_admin_action")
    if admin_action == "resolved":
        return _verification_hold_continuation(operation_id, view)
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
    cycle = None
    if resume_status is None:
        cycle = conn.execute(
            """SELECT cycle_id, resume_state, hold_identity FROM verification_cycles
                 WHERE operation_id=? AND route=?
                 ORDER BY cycle_number DESC LIMIT 1""",
            (operation_id, routes["cycle_route"]),
        ).fetchone()
        if cycle is not None:
            resume_status = cycle["resume_state"]
    elif preconstruction is None:
        cycle = conn.execute(
            """SELECT cycle_id, resume_state, hold_identity FROM verification_cycles
                 WHERE operation_id=? AND route=?
                 ORDER BY cycle_number DESC LIMIT 1""",
            (operation_id, routes["cycle_route"]),
        ).fetchone()

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

    op = conn.execute("SELECT task_gid FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if admin_action == "record-human-decision" and cycle is not None:
        consequences = human_review_consequence_metadata(resume_status)
        approval_after_resolution = consequences["approval"]
        dismissal_after_resolution = consequences["dismissal"]
        review_spec = exact_action(
            kind="review-human-decision",
            command="review-inspect",
            positional=(cycle["cycle_id"],),
            summary="Review the one decision that is blocking this task.",
            effect="Show the compact issue and let Marco approve the decision or dismiss an invalid escalation.",
            after_success={"instruction": "Use the review action Dish returns; do not reconstruct hold-resolution commands."},
        )
        dismiss_spec = template_action(
            kind="dismiss-human-review",
            command="review-reject",
            positional=(cycle["cycle_id"],),
            options=(("--reason", "<why this escalation is invalid>"),),
            prompt_fields=(PromptField("reason", "Why this escalation is invalid", "<why this escalation is invalid>"),),
            summary="Dismiss this Human Review escalation as invalid.",
            effect=(
                "Preserve the escalation and dismissal reason, record no Marco decision or governed authorization, "
                "and return the unchanged candidate to fresh Verification."
            ),
            after_success=dismissal_after_resolution,
        )
        return {
            "phase": phase,
            "submission_id": operation_id,
            "existing_submission_id": operation_id,
            "required_admin_action": "review-inspect",
            "resolver": "Marco/admin review workflow",
            "continuation_surface": "private-admin",
            "connected_action_available": False,
            **review_spec.payload(),
            "human_actions": [
                review_spec.payload()["human_action"] | {"shell_command": review_spec.shell_command()},
                dismiss_spec.payload()["human_action"] | {"shell_command": dismiss_spec.shell_command()},
            ],
            "resolution_effect": {
                "review_only": True,
                "records_human_decision": False,
                "modifies_canonical_fields": False,
                "authorizes_governed_field_changes": False,
            },
            "directive": (
                "Keep the Marco-facing result short: state the decision needed, quantify any threshold blocker, "
                "and say that the item is available in Dish review. Do not dump hold IDs, resume state, evidence "
                "detail, or a raw record-human-decision command unless Marco explicitly asks for protocol detail."
            ),
            "after_resolution": {
                "approval": approval_after_resolution,
                "dismissal": dismissal_after_resolution,
            },
        }
    options: list[tuple[str, object | None]] = [
        ("--detail", routes["detail_placeholder"]),
    ]
    if resume_status:
        options.append(("--resume-status", resume_status))
    if op is not None:
        options.append(("--expected-task-gid", op["task_gid"]))
    if cycle is not None:
        options.extend((
            ("--expected-cycle-id", cycle["cycle_id"]),
            ("--expected-hold-identity", cycle["hold_identity"]),
        ))
    spec = template_action(
        kind=admin_action,
        command=admin_action,
        positional=(operation_id,),
        options=tuple(options),
        prompt_fields=(
            PromptField("detail", "Decision or evidence detail", routes["detail_placeholder"]),
        ),
        summary=(
            "Record Marco's binding decision and release the hold."
            if admin_action == "record-human-decision"
            else "Record Marco-supplied evidence and release the hold."
        ),
        effect=(
            "Record the authenticated decision and release the hold; this does not edit "
            "or authorize governed fields."
            if admin_action == "record-human-decision"
            else "Record the supplied evidence, release the hold, and resume the operation."
        ),
        after_success=after_resolution,
    )
    command = spec.shell_command()
    alternative_human_actions = [spec.payload()["human_action"]]
    if admin_action == "record-human-decision" and cycle is not None:
        dismiss_spec = template_action(
            kind="dismiss-human-review",
            command="review-reject",
            positional=(cycle["cycle_id"],),
            options=(("--reason", "<why this escalation is invalid>"),),
            prompt_fields=(PromptField("reason", "Why this escalation is invalid", "<why this escalation is invalid>"),),
            summary="Dismiss this Human Review escalation as invalid.",
            effect=(
                "Preserve the rejected escalation in the audit trail, record no Marco decision or governed authorization, "
                "and resume the unchanged task so a fresh verifier can reassess it."
            ),
            after_success=human_review_consequence_metadata(resume_status)["dismissal"],
        )
        alternative_human_actions.append(
            dismiss_spec.payload()["human_action"] | {"shell_command": dismiss_spec.shell_command()}
        )
    next_action = after_resolution["legal_actions"][0] if after_resolution["legal_actions"] else None
    resume_clause = f" with `{next_action}`." if next_action else "."
    if admin_action == "record-human-decision":
        resolution_effect: dict[str, Any] = {
            "records_human_decision": True,
            "releases_hold": True,
            "resumes_workflow": True,
            "modifies_canonical_fields": False,
            "authorizes_governed_field_changes": False,
        }
        directive = (
            "Tell the human the exact decision being asked for and the consequence of each "
            "option (see this task's Status detail). `record-human-decision` records that "
            "authenticated decision, releases this hold, and resumes the workflow — it does not "
            "edit or authorize any change to canonical governed fields such as Exemptions or "
            "Locks. Before relaying it, replace the angle-bracketed detail text with the "
            "complete decision and reasoning:\n"
            f"{command}\n"
            "If carrying out the decision requires a governed-field change, say so explicitly "
            "and supply the separate exact `dish-admin authorize-governed-change` command using "
            "the authoritative current value and exact proposed replacement; do not describe the "
            "field change as approved or complete until that authorization succeeds and an agent "
            "installs the authorized candidate. Then wait for confirmation the decision command "
            "succeeded before continuing — do not start a new operation; resume this same "
            "submission" + resume_clause
        )
    else:
        resolution_effect = {
            "records_supplied_evidence": True,
            "releases_hold": True,
            "resumes_workflow": True,
            "modifies_canonical_fields": False,
        }
        directive = (
            "Ask Marco the actual missing fact in plain English using this task's Status detail. "
            "Do not list hold IDs, resume state, protocol field names, or the supply-evidence "
            "command unless Marco asks how to record the answer. After he answers, use the "
            "returned admin continuation and wait for confirmation before continuing — do not "
            "start a new operation; resume this same submission" + resume_clause
        )
    return {
        "phase": phase,
        "submission_id": operation_id,
        "existing_submission_id": operation_id,
        "required_admin_action": admin_action,
        "resolver": f"Marco/admin {admin_action}",
        "continuation_surface": "private-admin",
        "connected_action_available": False,
        **spec.payload(),
        "human_actions": alternative_human_actions,
        "resolution_effect": resolution_effect,
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
                for key in (
                    "admin_command",
                    "admin_command_is_template",
                    "admin_command_template",
                    "human_action",
                    "directive",
                    "continuation_surface",
                    "connected_action_available",
                    "after_resolution",
                    "proposal_id",
                    "proposal_status",
                    "proposal_queued",
                    "batch_may_continue",
                ):
                    if key in exc.details and exc.details.get(key) is not None:
                        result.setdefault("data", {})[key] = exc.details[key]
            if exc.rule == "planning_handoff_requires_initial":
                result["allowed_actions"] = ["start"]
            if trace.submission_id:
                try:
                    release = self._load_release(None)
                    view = expose_authoritative_view(
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
                    _attach_validation_retry_guidance(result, command=command)
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
    operation_id = _active_operation_id(self.conn, task_gid=task_gid)
    if operation_id is None:
        required_start_kind = _resting_task_required_start_kind(
            self.conn, live=live, diagnostics=diag
        )
        if required_start_kind is not None:
            data["required_start_kind"] = required_start_kind
        return result_envelope(
            command="read",
            task_gid=task_gid,
            allowed_actions=["start"] if required_start_kind is not None else [],
            data=data,
        )
    view = expose_authoritative_view(_current_operation_view(self, operation_id, schema=release.schema))
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
    view = expose_authoritative_view(internal_view)
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
            spec = template_action(
                kind="reopen-planning",
                command="reopen-planning",
                positional=(task_gid,),
                options=(("--reason", "<why this completed task must be reopened>"),),
                prompt_fields=(
                    PromptField(
                        "reason",
                        "Why this completed task must be reopened",
                        "<why this completed task must be reopened>",
                    ),
                ),
                summary="Reopen the completed task for a new Planning operation.",
                effect="Make the task eligible for Planning without creating a replacement task.",
                after_success={
                    "agent_actions": ["retry start with kind=planning using a fresh request ID"]
                },
            )
            reopen_command = spec.shell_command()
            raise DishRuleError(
                "WRONG_STATE",
                "completed tasks require Marco to reopen them before Planning",
                rule="planning_completed_task_reopen_required",
                details={
                    "required_admin_action": "reopen-planning",
                    "resolver": _admin_resolver("reopen-planning"),
                    **spec.payload(),
                    "legal_next_step": (
                        "Marco/admin runs reopen-planning with a reason; after it succeeds, "
                        "retry start with kind=planning using a fresh client.request_id"
                    ),
                    "directive": relay_text(
                        spec,
                        instruction=(
                            "Wait for confirmation it succeeded, then retry start with "
                            "kind=planning using a fresh client.request_id. Do not create a "
                            "replacement operation."
                        ),
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

    if kind != "verification" and _active_operation_id(self.conn, task_gid=task_gid) is None:
        required_start_kind = _resting_task_required_start_kind(
            self.conn, live=live, diagnostics=diag
        )
        if required_start_kind != kind:
            details = {}
            if required_start_kind is not None:
                details = {
                    "required_start_kind": required_start_kind,
                    "legal_next_step": (
                        f"start with kind={required_start_kind} using a fresh client.request_id"
                    ),
                }
            if kind == "change" and required_start_kind is None:
                raise DishRuleError(
                    "WRONG_STATE",
                    "post-signoff Change requires the current ready identity to have exact durable signoff lineage",
                    rule="post_signoff_change_signed_baseline_required",
                    details={"baseline_identity": live.identity},
                )
            raise DishRuleError(
                "WRONG_STATE",
                "requested start kind is not legal for this resting task",
                rule="resting_task_start_kind_mismatch",
                details=details,
            )
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
            view = expose_authoritative_view(
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



def _step6_prepare(self, *, trace: CommandTrace, agent: str, model: str | None = None, submission_id: str, file_path: str | None = None, material_classification: str | None = None, run_id: str | None = None, governed_change_fields=None) -> dict[str, Any]:
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
        lambda: prepare_live(
            self.conn, self.backend, operation_id=operation_id, agent=agent, model=model,
            file_path=file_path or "", release=release,
            material_classification=material_classification,
            governed_change_fields=governed_change_fields,
        ),
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

def _step8_reject(self, *, trace: CommandTrace, agent: str, model: str | None = None, submission_id: str, reason: str, route: str | None = None, file_path: str | None = None, resume_status: str | None = None, run_id: str | None = None, independence_attestation: str | None = None, blocker_metric: str | None = None, blocker_actual: float | None = None, blocker_limit: float | None = None, blocker_delta: float | None = None, blocker_unit: str | None = None, blocker_basis: str | None = None, human_review_confirmed: bool = False, human_review_basis: str | None = None, repairs_considered: str | None = None, governed_change_fields=None) -> dict[str, Any]:
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
        lambda: reject_route(self.conn, self.backend, operation_id=operation_id, agent=agent, model=model, route=route, reason=reason, file_path=file_path, resume_status=resume_status, run_id=run_id, independence_attestation=clean_attestation, request_id=self.invocation_request_id, schema=release.schema, honest_root=release.root, blocker_metric=blocker_metric, blocker_actual=blocker_actual, blocker_limit=blocker_limit, blocker_delta=blocker_delta, blocker_unit=blocker_unit, blocker_basis=blocker_basis, human_review_confirmed=human_review_confirmed, human_review_basis=human_review_basis, repairs_considered=repairs_considered, governed_change_fields=governed_change_fields),
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


def _step_semantic_proposals(
    self, *, trace: CommandTrace, agent: str
) -> dict[str, Any]:
    from .semantic_proposals import list_semantic_proposals

    release = self._load_release("verification")
    rows = list_semantic_proposals(self.conn, statuses=("approved",))
    proposals = []
    for item in rows:
        row = dict(item)
        view = self.operation_service.current.authoritative_view(
            str(row["operation_id"]), schema=release.schema
        )
        proposal_facts = view.get("semantic_proposal")
        if (
            "apply-proposal" not in view.get("legal_actions", ())
            or not isinstance(proposal_facts, Mapping)
            or proposal_facts.get("proposal_id") != row["proposal_id"]
        ):
            continue
        row.pop("candidate_notes", None)
        row["agent_action"] = {
            "command": "apply-proposal",
            "arguments": {"proposal_id": row["proposal_id"]},
        }
        proposals.append(row)
    return result_envelope(
        command="proposals", state="ok", allowed_actions=["apply-proposal"] if proposals else [],
        data={
            "count": len(proposals),
            "proposals": proposals,
            "instruction": (
                "Claim and apply an approved proposal exactly as stored. Do not reconstruct "
                "or edit its candidate."
            ),
        },
    )


def _step_apply_semantic_proposal(
    self, *, trace: CommandTrace, proposal_id: str, agent: str,
    model: str, run_id: str | None = None,
) -> dict[str, Any]:
    clean_id = _clean_required(
        proposal_id, rule="semantic_proposal_id_required", label="proposal ID"
    )
    effective_run_id = str(run_id or self.invocation_run_id or "").strip()
    if not effective_run_id:
        raise DishRuleError(
            "INVALID_ARGUMENT", "run ID is required to claim a proposal",
            rule="run_id_required",
        )
    from .semantic_proposals import get_semantic_proposal
    proposal = get_semantic_proposal(self.conn, clean_id)
    operation_id = str(proposal["operation_id"])
    trace.submission_id = operation_id
    trace.task_gid = str(proposal["task_gid"])
    trace.validation_scope = scope_for_command("reject")
    from .step8 import apply_semantic_proposal
    release = self._load_release("verification")
    data, view = self.operation_service.current.apply_proposal(
        operation_id,
        lambda: apply_semantic_proposal(
            self.conn, self.backend, proposal_id=clean_id, agent=agent,
            model=model, run_id=effective_run_id,
            request_id=self.invocation_request_id, schema=release.schema,
        ),
        schema=release.schema,
    )
    trace.state = str(view["status"])
    legal_actions, data = _exposed_result_contract(view, data)
    return result_envelope(
        command="apply-proposal", task_gid=trace.task_gid,
        submission_id=operation_id, state=view["status"],
        allowed_actions=legal_actions, data=data,
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
    "proposals": _step_semantic_proposals,
    "apply-proposal": _step_apply_semantic_proposal,
}
