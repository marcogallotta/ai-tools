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
    retire_operation_run_in_transaction,
    resolve_admin_abandonment_target,
    resolve_admin_dish_target,
    resolve_admin_operation_target,
    utc_now,
)
from .invocation_audit import record_invocation_audit
from .transactions import immediate_transaction, savepoint_transaction
from .errors import DishRuleError
from .admin_command_spec import RESOLVED_OPERATION_TARGET_COMMANDS
from .results import error_envelope, result_envelope
from .human_actions import PromptField, exact_action, relay_text, template_action
from .semantic_proposals import (
    active_proposal_for_operation,
    approve_semantic_proposal,
    get_semantic_proposal,
    proposal_payload,
    reject_semantic_proposal,
    release_semantic_proposal_claim,
)
from .step8 import apply_semantic_proposal
from .constants import MECHANICAL_PROPOSAL_AGENT
from .review_queue import (
    human_review_consequence_metadata,
    list_review_items,
    resolve_review_item,
)


def _mechanical_proposal_run_id(proposal_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"dish:semantic-proposal:{proposal_id}"))


def _is_recoverable_mechanical_proposal_claim(proposal: Mapping[str, Any]) -> bool:
    proposal_id = str(proposal["proposal_id"] or "")
    return bool(proposal_id) and (
        proposal["status"] == "claimed"
        and proposal["claimed_agent"] == MECHANICAL_PROPOSAL_AGENT
        and proposal["claimed_run_id"] == _mechanical_proposal_run_id(proposal_id)
    )


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


def _assert_no_active_semantic_proposal(
    conn: sqlite3.Connection,
    operation_id: str,
    *,
    requested_command: str,
    authoritative_view: Mapping[str, Any] | None = None,
) -> None:
    proposal = active_proposal_for_operation(conn, operation_id)
    if proposal is None:
        return
    proposal_status = str(proposal["status"])
    view_proposal = (
        authoritative_view.get("semantic_proposal")
        if isinstance(authoritative_view, Mapping)
        and isinstance(authoritative_view.get("semantic_proposal"), Mapping)
        else {}
    )
    legal_actions = (
        tuple(authoritative_view.get("legal_actions") or ())
        if isinstance(authoritative_view, Mapping)
        else ()
    )
    if proposal_status == "pending":
        next_command = "review-inspect"
        instruction = f"Review proposal {proposal['proposal_id']} before other admin recovery."
    elif proposal_status == "approved" and isinstance(view_proposal.get("block"), Mapping):
        next_command = "inspect"
        instruction = (
            f"Proposal {proposal['proposal_id']} is approved but currently blocked; inspect "
            "the authoritative reason before retrying application."
        )
    elif proposal_status == "approved":
        next_command = "review-approve"
        instruction = (
            f"Proposal {proposal['proposal_id']} is already approved; retry its mechanical "
            "application through the admin review action."
        )
    else:
        next_command = "inspect"
        instruction = (
            f"Inspect proposal {proposal['proposal_id']} and its authoritative block before "
            "attempting recovery or cancellation."
        )
    details: dict[str, Any] = {
        "proposal_id": proposal["proposal_id"],
        "proposal_status": proposal_status,
        "requested_command": requested_command,
        "required_action": next_command,
        "instruction": instruction,
    }
    if isinstance(view_proposal.get("block"), Mapping):
        details["proposal_block"] = dict(view_proposal["block"])
    raise DishRuleError(
        "WRONG_STATE",
        "the operation is parked on a durable semantic proposal",
        rule="semantic_proposal_application_required",
        details=details,
    )


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
        recovery_request_settler: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.conn = conn
        self.backend = backend
        self.release_loader = release_loader
        self.invocation_request_id = invocation_request_id
        self.invocation_run_id = invocation_run_id
        self.recovery_request_settler = recovery_request_settler
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


def _replace_run_action(*, dish_reference: str, summary: str, effect: str) -> dict[str, Any]:
    """Return the one high-level Marco action for retiring/replacing a Dish run."""
    spec = template_action(
        kind="replace-outstanding-run",
        command="kill",
        positional=(dish_reference,),
        options=(("--reason", "<why this Dish run should be replaced>"),),
        prompt_fields=(
            PromptField(
                "reason",
                "Why this Dish run should be replaced",
                "<why this Dish run should be replaced>",
            ),
        ),
        summary=summary,
        effect=effect,
        after_success={"instruction": "Follow the safe continuation returned by Dish."},
    )
    return spec.payload()["human_action"] | {"shell_command": spec.shell_command()}


def _command_inspect(
    self,
    *,
    trace: AdminTrace,
    dish: str | None = None,
    submission_id: str | None = None,
) -> dict[str, Any]:
    """Return a compact, human-oriented diagnostic for any known Dish."""

    if self.backend is None or self.operation_service is None:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "admin inspection requires backend access",
            rule="admin_inspect_unavailable",
        )
    target_reference = dish if dish is not None else submission_id
    target = resolve_admin_dish_target(self.conn, target_reference)
    trace.task_gid = str(target["task_gid"])

    from .constants import COOKING_PROJECT_GID
    from .task_store import read_complete_task
    from .commands import (
        _evidence_hold_continuation,
        _repair_destination_continuation,
        expose_authoritative_view,
    )

    operation_id = target.get("operation_id")
    if operation_id is None:
        live = read_complete_task(
            self.backend, task_gid=target["task_gid"], project_gid=COOKING_PROJECT_GID
        )
        trace.submission_id = None
        trace.state = "resting"
        data = {
            "dish_id": target["dish_id"],
            "task_title": live.title,
            "task_gid": target["task_gid"],
            "asana_url": f"https://app.asana.com/0/0/{target['task_gid']}",
            "operation_id": None,
            "operation_kind": None,
            "status": "resting",
            "phase": None,
            "waiting_for": "the next requested Dish workflow",
            "problem": "This Dish has no open workflow operation.",
            "operator_instruction": None,
            "administrative_blocker": False,
            "human_actions": [],
            "agent_actions_now": [],
            "semantic_proposal": None,
            "service_lease": None,
            "latest_actor_attempt": None,
            "outstanding_invocation": None,
            "verification_cycle": None,
            "abandonment": None,
            "authoritative_view": None,
        }
        return result_envelope(
            command="inspect",
            task_gid=target["task_gid"],
            submission_id=None,
            state="resting",
            data=data,
        )

    operation = self.conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if operation is None:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "resolved Dish operation is missing",
            rule="admin_dish_operation_missing",
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
    proposal = active_proposal_for_operation(self.conn, operation_id)
    safe_reclaim = None
    if active_lease is None and latest_lease is not None:
        from .safe_reclaim import safe_reclaim_eligibility
        safe_reclaim = safe_reclaim_eligibility(
            self.conn, self.backend, operation_id=operation_id,
            lease_id=latest_lease["lease_id"],
        )

    actions: list[dict[str, Any]] = []
    agent_actions_override: list[dict[str, Any] | str] | None = None
    administrative_blocker = False
    operator_instruction: str | None = None
    problem = "No administrative blocker is currently recorded."
    waiting_for = str(view.get("phase") or operation["phase"])

    if abandonment is not None and proposal is not None:
        administrative_blocker = True
        problem = (
            "The operation has both an active semantic proposal and an active abandonment. "
            "Dish cannot safely choose a continuation."
        )
        operator_instruction = (
            "Do not abandon, approve, or apply anything further. Report this conflicting "
            "state for reconciliation."
        )
    elif abandonment is not None:
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
            problem = "The prior run is retired; confirmed work is preserved and a fresh agent can continue safely."
        elif abandonment["status"] == "awaiting_hold_resolution":
            problem = "The prior run is retired; the real Evidence/Human Review checkpoint is preserved and still needs Marco."
        else:
            problem = "The prior run is retired, but Dish still needs deterministic reconciliation before a safe continuation is known."
    elif operation["status"] == "uncertain" or view.get("unresolved_attempts"):
        administrative_blocker = True
        actions.append(
            _replace_run_action(
                dish_reference=str(target["dish_id"] or target["task_gid"]),
                summary="Replace the interrupted run.",
                effect=(
                    "Dish will first reconcile only what fresh evidence proves, then retire the "
                    "old run's authority and prepare the safe continuation."
                ),
            )
        )
        problem = "An interrupted external write or movement belongs to the current run and must be reconciled before replacement."
    elif proposal is not None and proposal["status"] == "pending":
        administrative_blocker = True
        waiting_for = "Marco review of the queued semantic proposal"
        spec = exact_action(
            kind="inspect-semantic-proposal",
            command="review-inspect",
            positional=(proposal["proposal_id"],),
            summary="Review the exact linked semantic change bundle.",
            effect="Show Marco the rationale and every linked edit before approval or rejection.",
            after_success={"instruction": "Approve, reject, or defer the proposal."},
        )
        actions.append(spec.payload()["human_action"] | {"shell_command": spec.shell_command()})
        problem = "This Verification attempt is safely parked while Marco reviews a semantic proposal."
    elif proposal is not None and proposal["status"] == "approved":
        administrative_blocker = True
        if "apply-proposal" in view.get("legal_actions", ()):
            waiting_for = "Dish to retry mechanical application of Marco's approved proposal"
            problem = (
                "Marco's exact proposal approval is durable, but mechanical application has not completed."
            )
            spec = exact_action(
                kind="retry-approved-proposal",
                command="review-approve",
                positional=(proposal["proposal_id"],),
                summary="Retry mechanical application of the already-approved exact bundle.",
                effect=(
                    "Reuse the durable approval; revalidate and apply only the same immutable candidate."
                ),
                after_success={"instruction": "Give the resulting task to an independent verifier."},
            )
            actions.append(spec.payload()["human_action"] | {"shell_command": spec.shell_command()})
        else:
            waiting_for = "authoritative proposal reconciliation"
            problem = (
                "Marco approved the proposal, but its authoritative state no longer permits "
                "application."
            )
            operator_instruction = (
                "Inspect the authoritative proposal block; the durable approval remains recorded."
            )
    elif proposal is not None and proposal["status"] == "claimed":
        administrative_blocker = True
        if _is_recoverable_mechanical_proposal_claim(proposal):
            waiting_for = "Dish to resume finalization of Marco's already-approved proposal"
            problem = (
                "Dish's deterministic mechanical application claim is still durable; retry can "
                "reconcile an exact previously confirmed write without asking Marco again."
            )
            spec = exact_action(
                kind="resume-approved-proposal",
                command="review-approve",
                positional=(proposal["proposal_id"],),
                summary="Resume the already-approved mechanical proposal application.",
                effect=(
                    "Reuse only the exact deterministic Dish claim and durable confirmed-write binding; "
                    "do not perform another governed write when that binding proves the candidate is live."
                ),
                after_success={"instruction": "Give the resulting task to an independent verifier."},
            )
            actions.append(spec.payload()["human_action"] | {"shell_command": spec.shell_command()})
        else:
            waiting_for = "the agent currently applying Marco's approved proposal"
            problem = "An agent run has claimed the approved proposal for exact application."
            if active_lease is not None:
                actions.append(
                    _replace_run_action(
                        dish_reference=str(target["dish_id"] or target["task_gid"]),
                        summary="Replace the applying run.",
                        effect=(
                            "Retire that run's Dish authority while preserving Marco's exact approved "
                            "proposal for deterministic re-application."
                        ),
                    )
                )
            else:
                operator_instruction = (
                    "The proposal claim has no active lease. Do not abandon the operation or guess a "
                    "replacement run; report this claim for deterministic recovery."
                )
    elif active_lease is not None:
        administrative_blocker = True
        actions.append(
            _replace_run_action(
                dish_reference=str(target["dish_id"] or target["task_gid"]),
                summary="Replace the current run.",
                effect=(
                    "Retire only this run's Dish authority, preserve confirmed work and real "
                    "checkpoints, and prepare the safe continuation mechanically."
                ),
            )
        )
        problem = "A Dish run currently owns this workflow operation."
    elif safe_reclaim is not None and safe_reclaim.eligible:
        waiting_for = "a fresh agent to reclaim the inactive clean attempt"
        problem = (
            "The prior agent run is inactive and committed state is mechanically safe "
            "for a different run to reclaim."
        )
        agent_actions_override = [
            {
                "command": "safe-reclaim",
                "arguments": {
                    "submission_id": operation_id,
                    "lease_id": safe_reclaim.lease_id,
                },
            }
        ]
    elif operation["phase"] in {"held_evidence", "held_human"}:
        administrative_blocker = True
        continuation = _evidence_hold_continuation(self.conn, operation_id, view)
        continuation_actions = continuation.get("human_actions")
        if isinstance(continuation_actions, list):
            for item in continuation_actions:
                if isinstance(item, dict):
                    actions.append(dict(item))
        else:
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
        recovery_rules = {
            "safe_reclaim_unresolved_external_effect",
            "safe_reclaim_execution_claim_live",
            "safe_reclaim_execution_unsettled",
            "safe_reclaim_request_unsettled",
        }
        failed_rules = (
            {item.get("rule") for item in safe_reclaim.failed_clauses}
            if safe_reclaim is not None else set()
        )
        if failed_rules & recovery_rules:
            actions.append(
                _replace_run_action(
                    dish_reference=str(target["dish_id"] or target["task_gid"]),
                    summary="Replace the interrupted verifier run.",
                    effect=(
                        "Dish will reconcile the interrupted execution first, then preserve the "
                        "candidate and prepare a fresh Verification continuation."
                    ),
                )
            )
            problem = "The prior verifier is inactive, but its interrupted execution must be reconciled before another run can take over."
        elif lease is not None:
            actions.append(
                _replace_run_action(
                    dish_reference=str(target["dish_id"] or target["task_gid"]),
                    summary="Replace the inactive verifier run.",
                    effect="Preserve the candidate and prepare a fresh Verification continuation.",
                )
            )
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
        recovery_rules = {
            "safe_reclaim_unresolved_external_effect",
            "safe_reclaim_execution_claim_live",
            "safe_reclaim_execution_unsettled",
            "safe_reclaim_request_unsettled",
        }
        failed_rules = (
            {item.get("rule") for item in safe_reclaim.failed_clauses}
            if safe_reclaim is not None else set()
        )
        if failed_rules & recovery_rules:
            actions.append(
                _replace_run_action(
                    dish_reference=str(target["dish_id"] or target["task_gid"]),
                    summary="Replace the interrupted run.",
                    effect=(
                        "Dish will reconcile the interrupted execution first, then preserve "
                        "confirmed work and prepare the safe continuation."
                    ),
                )
            )
            problem = (
                "The prior run is inactive, but its interrupted execution must be reconciled "
                "before replacement."
            )
        else:
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
                actions.append(
                    _replace_run_action(
                        dish_reference=str(target["dish_id"] or target["task_gid"]),
                        summary="Replace the inactive run.",
                        effect="Preserve confirmed work and prepare the stage's safe successor.",
                    )
                )
                problem = "The operation belongs to a prior agent run with no active lease."
    trace.submission_id = operation_id
    trace.task_gid = operation["task_gid"]
    trace.state = operation["status"]
    outstanding_invocation = None
    replacement_action_present = any(
        isinstance(item, Mapping) and item.get("command") == "kill" for item in actions
    )
    outstanding_lease = self.conn.execute(
        """SELECT * FROM service_leases
             WHERE operation_id=? AND lease_kind='actor' AND released_at IS NULL
             ORDER BY actor_attempt_seq DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if outstanding_lease is None and replacement_action_present:
        outstanding_lease = latest_lease
    if outstanding_lease is not None:
        if outstanding_lease["released_at"] is not None:
            authority_state = "retired"
        else:
            authority_state = (
                "active"
                if self.conn.execute(
                    "SELECT julianday(?) > julianday('now')",
                    (outstanding_lease["expires_at"],),
                ).fetchone()[0]
                else "expired"
            )
        outstanding_invocation = {
            "dish_id": target["dish_id"],
            "operation_id": operation_id,
            "stage": operation["phase"],
            "owner_id": outstanding_lease["owner_id"],
            "run_id": outstanding_lease["run_id"],
            "lease_id": outstanding_lease["lease_id"],
            "first_observed_at": outstanding_lease["acquired_at"],
            "last_activity_at": outstanding_lease["renewed_at"],
            "authority_state": authority_state,
            "expires_at": outstanding_lease["expires_at"],
            "replacement_required": replacement_action_present,
        }

    data = {
        "dish_id": target["dish_id"],
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
            []
            if administrative_blocker
            else (
                agent_actions_override
                if agent_actions_override is not None
                else list(view.get("legal_actions") or [])
            )
        ),
        "semantic_proposal": None if proposal is None else {
            "proposal_id": proposal["proposal_id"],
            "status": proposal["status"],
            "candidate_identity": proposal["candidate_identity"],
            "claimed_agent": proposal["claimed_agent"],
            "claimed_run_id": proposal["claimed_run_id"],
        },
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
        "outstanding_invocation": outstanding_invocation,
        "verification_cycle": None if cycle is None else {
            "cycle_id": cycle["cycle_id"],
            "run_id": cycle["run_id"],
            "verifier_agent": cycle["verifier_agent"],
            "completed_at": cycle["completed_at"],
            "outcome": cycle["outcome"],
            "route": cycle["route"],
        },
        "abandonment": None if abandonment is None else {
            "abandonment_id": abandonment["abandonment_id"],
            "status": abandonment["status"],
            "outcome": abandonment["outcome"],
            "source_operation_id": abandonment["source_operation_id"],
            "successor_operation_id": abandonment["successor_operation_id"],
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



def _command_kill(
    self,
    *,
    trace: AdminTrace,
    dish: str,
    reason: str,
) -> dict[str, Any]:
    """Fence one outstanding Dish run and drive its safe replacement mechanically."""
    if self.backend is None or self.operation_service is None:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "Dish replacement requires backend access",
            rule="admin_kill_unavailable",
        )
    clean_reason = _clean_required(
        reason, rule="kill_reason_required", label="replacement reason"
    )
    target = resolve_admin_dish_target(self.conn, dish)
    trace.task_gid = str(target["task_gid"])
    operation_id = target.get("operation_id")
    if operation_id is None:
        inspected = _command_inspect(self, trace=AdminTrace(), dish=dish)
        data = inspected.get("data") if isinstance(inspected.get("data"), Mapping) else {}
        return result_envelope(
            command="kill",
            task_gid=target["task_gid"],
            submission_id=None,
            state="resting",
            data={
                "dish_id": target["dish_id"],
                "outcome": "no_outstanding_invocation",
                "human_consequence": "Nothing was changed; this Dish has no workflow run to replace.",
                "fenced_invocation": None,
                "continuation": dict(data),
            },
        )

    from dish_service.leases import LeaseManager
    from .operation_execution import (
        live_operation_execution_claim,
        operation_recovery_pending,
        unresolved_operation_executions,
    )

    operation = self.conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if operation is None:
        raise DishRuleError(
            "INTERNAL_ERROR", "resolved Dish operation is missing",
            rule="admin_dish_operation_missing",
        )
    trace.submission_id = str(operation_id)
    trace.state = str(operation["status"])

    abandonment = self.conn.execute(
        """SELECT * FROM abandonment_attempts
             WHERE task_gid=? AND status!='completed'
             ORDER BY created_at DESC LIMIT 1""",
        (target["task_gid"],),
    ).fetchone()
    lease = self.conn.execute(
        """SELECT * FROM service_leases
             WHERE operation_id=? AND lease_kind='actor' AND released_at IS NULL
             ORDER BY actor_attempt_seq DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    proposal_before_kill = active_proposal_for_operation(self.conn, operation_id)
    lease_was_active_at_resolution = lease is not None

    # Resolve the exact authority Marco is replacing before taking the writer
    # lock, but establish retirement only inside that lock.  A claimed proposal
    # can outlive its active actor lease, so recover its service owner from the
    # durable lease history for the exact claimed run.
    retirement_source = lease
    if retirement_source is None and proposal_before_kill is not None and proposal_before_kill["status"] == "claimed":
        claimed_run = str(proposal_before_kill["claimed_run_id"] or "").strip()
        if claimed_run:
            retirement_source = self.conn.execute(
                """SELECT * FROM service_leases
                     WHERE operation_id=? AND lease_kind='actor' AND run_id=?
                     ORDER BY actor_attempt_seq DESC LIMIT 1""",
                (operation_id, claimed_run),
            ).fetchone()
            if retirement_source is None:
                raise DishRuleError(
                    "CONFLICT",
                    "Dish cannot prove the service owner for the claimed proposal run",
                    rule="kill_retirement_identity_missing",
                    details={
                        "operation_id": operation_id,
                        "proposal_id": proposal_before_kill["proposal_id"],
                        "run_id": claimed_run,
                    },
                )
    if retirement_source is None:
        retirement_source = self.conn.execute(
            """SELECT * FROM service_leases
                 WHERE operation_id=? AND lease_kind='actor'
                 ORDER BY actor_attempt_seq DESC LIMIT 1""",
            (operation_id,),
        ).fetchone()

    claim = live_operation_execution_claim(self.conn, operation_id=operation_id)
    if claim is not None:
        raise DishRuleError(
            "CONFLICT",
            "Dish is currently committing a workflow mutation; no authority was changed",
            rule="kill_mutation_in_progress",
            retryable=True,
            details={
                "dish_id": target["dish_id"],
                "operation_id": operation_id,
                "run_id": None if retirement_source is None else retirement_source["run_id"],
                "command": claim["command"],
                "acquired_at": claim["acquired_at"],
                "human_consequence": (
                    "The current write was left untouched. Retry kill after that mutation settles."
                ),
            },
        )

    fenced = None
    if retirement_source is not None:
        target_owner = str(retirement_source["owner_id"])
        target_run = str(retirement_source["run_id"])
        source_lease_id = str(retirement_source["lease_id"])
        with immediate_transaction(self.conn, "kill_retire_operation_run"):
            claim = live_operation_execution_claim(self.conn, operation_id=operation_id)
            if claim is not None:
                raise DishRuleError(
                    "CONFLICT",
                    "Dish began committing a workflow mutation before the run could be fenced",
                    rule="kill_mutation_in_progress",
                    retryable=True,
                    details={"dish_id": target["dish_id"], "operation_id": operation_id},
                )
            retirement = retire_operation_run_in_transaction(
                self.conn,
                operation_id=operation_id,
                owner_id=target_owner,
                run_id=target_run,
                source_lease_id=source_lease_id,
                reason=f"Marco kill/replace: {clean_reason}",
            )
            # Lease cleanup is secondary to retirement.  If the selected lease
            # was released/replaced before this transaction, touch only a current
            # lease still owned by the exact retired principal; a successor lease
            # is never expired.
            current = LeaseManager(self.conn).active_for_operation(operation_id)
            if (
                current is not None
                and current["owner_id"] == target_owner
                and current["run_id"] == target_run
            ):
                LeaseManager(self.conn).admin_expire_selected(
                    str(current["lease_id"]),
                    reason=f"Marco kill/replace: {clean_reason}",
                    manage_transaction=False,
                )
        fenced = {
            "owner_id": target_owner,
            "run_id": target_run,
            "lease_id": source_lease_id,
            "first_observed_at": retirement_source["acquired_at"],
            "last_activity_at": retirement_source["renewed_at"],
            "authority_state": "retired",
            "retired_at": retirement["retired_at"],
            "retirement_id": retirement["retirement_id"],
        }

    # Reconcile interrupted effects before moving ownership.  This is plumbing,
    # not a second Marco decision.
    operation = self.conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if (
        operation is not None
        and (
            operation["status"] == "uncertain"
            or operation_recovery_pending(self.conn, operation_id)
            or unresolved_operation_executions(self.conn, operation_id)
        )
    ):
        _step9_admin_recover(
            self,
            trace=AdminTrace(submission_id=operation_id),
            submission_id=operation_id,
            outcome="inspect",
            reason=f"kill/replace recovery: {clean_reason}",
        )

    proposal = active_proposal_for_operation(self.conn, operation_id)
    if proposal is not None and proposal["status"] == "claimed":
        claimed_run = str(proposal["claimed_run_id"] or "")
        if fenced is not None and claimed_run == fenced["run_id"]:
            release_semantic_proposal_claim(
                self.conn,
                proposal_id=str(proposal["proposal_id"]),
                run_id=claimed_run,
                reason=f"applying run replaced by Marco: {clean_reason}",
            )
            proposal = active_proposal_for_operation(self.conn, operation_id)

    # A semantic proposal is a deliberate durable checkpoint.  Killing its agent
    # run never answers the proposal for Marco.
    if proposal is not None:
        inspected = _command_inspect(self, trace=AdminTrace(), dish=dish)
        continuation = (
            dict(inspected["data"])
            if isinstance(inspected.get("data"), Mapping)
            else {}
        )
        return result_envelope(
            command="kill",
            task_gid=target["task_gid"],
            submission_id=operation_id,
            state=continuation.get("status") or trace.state,
            data={
                "dish_id": target["dish_id"],
                "outcome": "checkpoint_preserved",
                "human_consequence": (
                    "The prior run can no longer act. The existing semantic proposal remains exactly as Marco's review checkpoint."
                ),
                "fenced_invocation": fenced,
                "preserved_checkpoint": {
                    "kind": "semantic_proposal",
                    "proposal_id": proposal["proposal_id"],
                    "status": proposal["status"],
                },
                "continuation": continuation,
            },
        )

    # If another kill already began abandonment, continue it rather than asking
    # Marco to operate the reconciliation internals.
    if abandonment is None and fenced is not None and lease_was_active_at_resolution:
        abandoned = _command_abandon_operation(
            self,
            trace=AdminTrace(submission_id=operation_id),
            submission_id=operation_id,
            reason=clean_reason,
            lease_id=str(fenced["lease_id"]),
        )
        abandonment_data = (
            dict(abandoned["data"])
            if isinstance(abandoned.get("data"), Mapping)
            else {}
        )
        abandonment_id = str(abandonment_data.get("abandonment_id") or "")
    elif abandonment is not None:
        abandonment_id = str(abandonment["abandonment_id"])
        abandonment_data = _decorate_abandonment_result(
            self.conn, {"abandonment_id": abandonment_id}
        )
    else:
        # No authority is currently live.  Inspect the authoritative workflow
        # disposition: a clean handoff/safe-reclaim is already replacement-ready,
        # while a dead bound attempt still requires the existing durable
        # abandonment path.  This avoids treating every historical lease as dead.
        inspected = _command_inspect(self, trace=AdminTrace(), dish=dish)
        continuation = (
            dict(inspected["data"])
            if isinstance(inspected.get("data"), Mapping)
            else {}
        )
        continuation_actions = continuation.get("human_actions")
        replacement_required = (
            isinstance(continuation_actions, list)
            and any(
                isinstance(item, Mapping) and item.get("command") == "kill"
                for item in continuation_actions
            )
        )
        if not replacement_required:
            agent_actions = continuation.get("agent_actions_now")
            safe_reclaim_ready = (
                isinstance(agent_actions, list)
                and any(
                    isinstance(item, Mapping) and item.get("command") == "safe-reclaim"
                    for item in agent_actions
                )
            )
            held = continuation.get("phase") in {"held_evidence", "held_human"}
            outcome = (
                "replacement_ready" if safe_reclaim_ready
                else "checkpoint_preserved" if held
                else "no_outstanding_invocation"
            )
            consequence = (
                "The prior run is durably retired. The workflow is mechanically ready for a fresh agent to reclaim."
                if safe_reclaim_ready and fenced is not None
                else "The prior run already has no Dish authority. The workflow is mechanically ready for a fresh agent to reclaim."
                if safe_reclaim_ready
                else "The prior run is durably retired. The real Evidence/Human Review checkpoint remains intact."
                if held and fenced is not None
                else "The prior run already has no Dish authority. The real Evidence/Human Review checkpoint remains intact."
                if held
                else "The exact prior run is durably retired; no additional recovery work is required."
                if fenced is not None
                else "Nothing was changed; Dish has no currently authorized or dead-bound run that needs replacement."
            )
            return result_envelope(
                command="kill",
                task_gid=target["task_gid"],
                submission_id=operation_id,
                state=continuation.get("status") or trace.state,
                data={
                    "dish_id": target["dish_id"],
                    "outcome": outcome,
                    "human_consequence": consequence,
                    "fenced_invocation": fenced,
                    "continuation": continuation,
                },
            )

        try:
            dead_lease = _select_abandonment_lease(
                self.conn, operation_id=operation_id, lease_id=None
            )
        except DishRuleError as exc:
            return result_envelope(
                command="kill",
                task_gid=target["task_gid"],
                submission_id=operation_id,
                state=continuation.get("status") or trace.state,
                data={
                    "dish_id": target["dish_id"],
                    "outcome": "manual_reconciliation_required",
                    "human_consequence": (
                        "The prior run has no live Dish authority, but current durable records do not identify one safe attempt to replace. Nothing further was changed."
                    ),
                    "fenced_invocation": None,
                    "continuation": continuation,
                    "replacement_block": {"rule": exc.rule, "message": str(exc)},
                },
            )
        abandoned = _command_abandon_operation(
            self,
            trace=AdminTrace(submission_id=operation_id),
            submission_id=operation_id,
            reason=clean_reason,
            lease_id=str(dead_lease["lease_id"]),
        )
        abandonment_data = (
            dict(abandoned["data"])
            if isinstance(abandoned.get("data"), Mapping)
            else {}
        )
        abandonment_id = str(abandonment_data.get("abandonment_id") or "")

    # One automatic reconciliation retry covers the supported fresh-reread
    # frontier.  If it remains blocked, that is a real repair condition rather
    # than another hidden operator step.
    abandonment = get_abandonment_attempt(self.conn, abandonment_id)
    if abandonment["status"] in {"started", "blocked_manual_reconciliation"}:
        reconciled = _command_reconcile_abandonment(
            self,
            trace=AdminTrace(submission_id=operation_id),
            abandonment_id=abandonment_id,
        )
        abandonment_data = (
            dict(reconciled["data"])
            if isinstance(reconciled.get("data"), Mapping)
            else abandonment_data
        )
        abandonment = get_abandonment_attempt(self.conn, abandonment_id)

    inspected = _command_inspect(self, trace=AdminTrace(), dish=dish)
    continuation = (
        dict(inspected["data"])
        if isinstance(inspected.get("data"), Mapping)
        else {}
    )
    status = str(abandonment["status"])
    if status == "awaiting_successor_claim":
        outcome = "replacement_ready"
        consequence = (
            "The prior run can no longer act. Confirmed work was preserved and the safe continuation is ready for a fresh agent."
        )
    elif status == "awaiting_hold_resolution":
        outcome = "checkpoint_preserved"
        consequence = (
            "The prior run can no longer act. The real Evidence/Human Review checkpoint was preserved and still needs Marco's decision."
        )
    elif status == "completed":
        outcome = "replacement_complete"
        consequence = (
            "The prior run can no longer act and Dish completed the safe replacement path."
        )
    else:
        outcome = "manual_reconciliation_required"
        consequence = (
            "The prior run can no longer act, but fresh evidence still does not prove one safe continuation. No further workflow authority was guessed."
        )
    return result_envelope(
        command="kill",
        task_gid=target["task_gid"],
        submission_id=operation_id,
        state=continuation.get("status") or trace.state,
        data={
            "dish_id": target["dish_id"],
            "outcome": outcome,
            "human_consequence": consequence,
            "fenced_invocation": fenced,
            "abandonment": abandonment_data.get("abandonment"),
            "continuation": continuation,
        },
    )

def _attention_category(data: Mapping[str, Any]) -> tuple[str, str]:
    """Classify one inspect result conservatively for the read-only attention view."""

    status = str(data.get("status") or "")
    phase = str(data.get("phase") or "")
    problem = str(data.get("problem") or "")
    view = data.get("authoritative_view")
    if not isinstance(view, Mapping):
        view = {}
    abandonment = data.get("abandonment")
    proposal = data.get("semantic_proposal")
    if isinstance(proposal, Mapping):
        proposal_status = str(proposal.get("status") or "")
        proposal_view = view.get("semantic_proposal")
        proposal_block = (
            proposal_view.get("block")
            if isinstance(proposal_view, Mapping)
            and isinstance(proposal_view.get("block"), Mapping)
            else None
        )
        if proposal_status == "pending":
            return "needs_marco", "a semantic proposal is waiting for Marco's review"
        if proposal_status == "approved":
            if "apply-proposal" in view.get("legal_actions", ()):
                return "healthy", "an approved proposal is ready for a fresh agent to apply"
            rule = None if proposal_block is None else proposal_block.get("rule")
            return "unsafe", f"an approved proposal is blocked by {rule or 'authoritative state'}"
        if proposal_status == "claimed":
            if proposal_block is not None:
                return "unsafe", (
                    "a claimed proposal is blocked by "
                    f"{proposal_block.get('rule') or 'authoritative state'}"
                )
            if data.get("service_lease") is not None:
                return "healthy", "an agent is applying an approved proposal"
            return "unsafe", "an approved proposal claim exists without an active applying lease"
    if isinstance(abandonment, Mapping):
        abandonment_status = str(abandonment.get("status") or "")
        if abandonment_status == "awaiting_successor_claim":
            return "healthy", "prepared successor is waiting for an agent"
        if abandonment_status == "awaiting_hold_resolution":
            return "needs_marco", "abandonment preserved a real Evidence or Human Review hold"
        if abandonment_status == "blocked_manual_reconciliation":
            return "unsafe", "abandonment reached an unsupported or contradictory frontier"
        return "multi_step_safe", "abandonment has a deterministic continuation"

    if status == "uncertain" or view.get("unresolved_attempts") or view.get("unresolved_execution_ids"):
        return "unsafe", "an external effect or execution outcome is unresolved"
    if status not in {"open", "uncertain"} and data.get("service_lease") is not None:
        return "unsafe", "a terminal operation still has an active actor lease"
    if phase in {"held_evidence", "held_human"}:
        return "needs_marco", "the workflow is waiting for Marco's evidence or decision"
    if view.get("destination_repair_required"):
        return "needs_marco", "the approved destination requires an explicit Marco repair"

    agent_actions = data.get("agent_actions_now")
    if not isinstance(agent_actions, list):
        agent_actions = []
    agent_commands = {
        str(action.get("command") or "")
        for action in agent_actions
        if isinstance(action, Mapping)
    }
    if "safe-reclaim" in agent_commands:
        return "multi_step_safe", "a fresh agent can safely reclaim the inactive clean attempt"

    actions = data.get("human_actions")
    if not isinstance(actions, list):
        actions = []
    kinds = {
        str(action.get("kind") or "")
        for action in actions
        if isinstance(action, Mapping)
    }
    if "reconcile-uncertain-effect" in kinds:
        return "unsafe", "the live outcome must be reconciled before any cleanup"
    if kinds & {"abandon-dead-verifier", "abandon-dead-agent", "reconcile-abandonment"}:
        return "multi_step_safe", "the dead run can be retired through supported recovery"
    if "expire-active-lease" in kinds:
        return "healthy", "an unexpired lease currently owns the operation"
    if kinds:
        return "needs_marco", "the next step requires a Marco-admin decision or input"
    if (
        "cannot identify one safe" in problem.lower()
        or "cannot safely choose" in problem.lower()
        or "historical attempts do not identify" in problem.lower()
    ):
        return "needs_marco", "Dish cannot safely choose between multiple historical attempts"
    return "healthy", "no administrative blocker is recorded"


def _attention_candidate_operation_ids(conn: sqlite3.Connection) -> list[str]:
    """Return only database-evidenced abnormal candidates, one per task."""

    selected: list[str] = []
    seen_tasks: set[str] = set()

    for row in conn.execute(
        """SELECT task_gid, source_operation_id
             FROM abandonment_attempts
            WHERE status!='completed'
            ORDER BY created_at, abandonment_id"""
    ):
        task_gid = str(row["task_gid"])
        if task_gid in seen_tasks:
            continue
        seen_tasks.add(task_gid)
        selected.append(str(row["source_operation_id"]))

    for row in conn.execute(
        """SELECT operation_id, task_gid
             FROM operations
            WHERE status='uncertain'
               OR (status='open' AND phase IN ('held_evidence','held_human'))
               OR (status='open' AND destination_movement_attempt_id IS NOT NULL
                   AND movement_completed_at IS NULL)
            ORDER BY created_at, operation_id"""
    ):
        task_gid = str(row["task_gid"])
        if task_gid in seen_tasks:
            continue
        seen_tasks.add(task_gid)
        selected.append(str(row["operation_id"]))

    for row in conn.execute(
        """SELECT operation.operation_id, operation.task_gid
             FROM semantic_proposals AS proposal
             JOIN operations AS operation ON operation.operation_id=proposal.operation_id
            WHERE proposal.status IN ('pending','approved','claimed')
            ORDER BY proposal.created_at, proposal.proposal_id"""
    ):
        task_gid = str(row["task_gid"])
        if task_gid in seen_tasks:
            continue
        seen_tasks.add(task_gid)
        selected.append(str(row["operation_id"]))

    for row in conn.execute(
        """SELECT operation.operation_id, operation.task_gid
             FROM service_leases AS lease
             JOIN operations AS operation ON operation.operation_id=lease.operation_id
            WHERE lease.lease_kind='actor' AND lease.released_at IS NULL
              AND (lease.expires_at<=? OR operation.status!='open')
            ORDER BY lease.acquired_at, lease.lease_id""",
        (utc_now(),),
    ):
        task_gid = str(row["task_gid"])
        if task_gid in seen_tasks:
            continue
        seen_tasks.add(task_gid)
        selected.append(str(row["operation_id"]))

    for row in conn.execute(
        """SELECT operation.operation_id, operation.task_gid
             FROM operations AS operation
            WHERE operation.status='open'
              AND operation.phase IN ('prepare_required','await_verification','await_submission')
              AND operation.run_id IS NOT NULL
              AND NOT EXISTS (
                    SELECT 1 FROM service_leases AS lease
                     WHERE lease.operation_id=operation.operation_id
                       AND lease.lease_kind='actor' AND lease.released_at IS NULL
                  )
            ORDER BY operation.created_at, operation.operation_id"""
    ):
        task_gid = str(row["task_gid"])
        if task_gid in seen_tasks:
            continue
        seen_tasks.add(task_gid)
        selected.append(str(row["operation_id"]))

    for row in conn.execute(
        """SELECT operation.operation_id, operation.task_gid
             FROM operation_executions AS execution
             JOIN operations AS operation ON operation.operation_id=execution.operation_id
            WHERE execution.status IN ('started','uncertain')
            ORDER BY execution.created_at, execution.execution_id"""
    ):
        task_gid = str(row["task_gid"])
        if task_gid in seen_tasks:
            continue
        seen_tasks.add(task_gid)
        selected.append(str(row["operation_id"]))
    return selected


def _command_attention(self, *, trace: AdminTrace) -> dict[str, Any]:
    """List abnormal workflow state across Dish without mutating any item."""

    if self.backend is None or self.operation_service is None:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "attention scan requires backend access",
            rule="attention_scan_unavailable",
        )

    items: list[dict[str, Any]] = []
    category_counts = {
        "safe_cleanup": 0,
        "multi_step_safe": 0,
        "needs_marco": 0,
        "unsafe": 0,
    }
    healthy_count = 0
    workflow_record_count = int(
        self.conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    )
    operation_ids = _attention_candidate_operation_ids(self.conn)
    for operation_id in operation_ids:
        try:
            inspected = _command_inspect(
                self, trace=AdminTrace(), submission_id=operation_id
            )
            data = inspected.get("data")
            if not inspected.get("ok") or not isinstance(data, Mapping):
                raise DishRuleError(
                    str(inspected.get("code") or "INTERNAL_ERROR"),
                    str(
                        (data or {}).get("message")
                        if isinstance(data, Mapping)
                        else "inspection failed"
                    ),
                    rule="attention_item_inspection_failed",
                )
            category, category_reason = _attention_category(data)
            if category == "healthy":
                healthy_count += 1
                continue
            category_counts[category] += 1
            items.append(
                {
                    "category": category,
                    "category_reason": category_reason,
                    "task_title": data.get("task_title"),
                    "task_gid": data.get("task_gid"),
                    "asana_url": data.get("asana_url"),
                    "operation_id": data.get("operation_id"),
                    "status": data.get("status"),
                    "phase": data.get("phase"),
                    "problem": data.get("problem"),
                    "human_actions": data.get("human_actions") or [],
                    "agent_actions_now": data.get("agent_actions_now") or [],
                    "service_lease": data.get("service_lease"),
                    "latest_actor_attempt": data.get("latest_actor_attempt"),
                    "verification_cycle": data.get("verification_cycle"),
                    "abandonment": data.get("abandonment"),
                }
            )
        except DishRuleError as exc:
            operation = self.conn.execute(
                "SELECT task_gid,status,phase FROM operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if (
                exc.rule == "task_not_in_cooking"
                and operation is not None
                and operation["status"] not in {"open", "uncertain"}
            ):
                healthy_count += 1
                continue
            category_counts["unsafe"] += 1
            items.append(
                {
                    "category": "unsafe",
                    "category_reason": "Dish could not establish a trustworthy inspection result",
                    "task_title": None,
                    "task_gid": None if operation is None else operation["task_gid"],
                    "operation_id": operation_id,
                    "status": None if operation is None else operation["status"],
                    "phase": None if operation is None else operation["phase"],
                    "problem": str(exc),
                    "errors": [{"rule": exc.rule, **dict(exc.details)}],
                    "human_actions": [],
                    "agent_actions_now": [],
                }
            )
        except Exception as exc:
            category_counts["unsafe"] += 1
            items.append(
                {
                    "category": "unsafe",
                    "category_reason": "Dish could not complete inspection for this operation",
                    "task_title": None,
                    "task_gid": None,
                    "operation_id": operation_id,
                    "status": None,
                    "phase": None,
                    "problem": "unexpected inspection failure",
                    "errors": [{"rule": "attention_item_unexpected_failure", "error_type": type(exc).__name__}],
                    "human_actions": [],
                    "agent_actions_now": [],
                }
            )

    trace.state = "ok"
    trace.audit_details.update(
        {
            "checked_count": workflow_record_count,
            "live_inspection_count": len(operation_ids),
            "attention_count": len(items),
            "category_counts": dict(category_counts),
        }
    )
    return result_envelope(
        command="attention",
        state="ok",
        data={
            "checked_count": workflow_record_count,
            "live_inspection_count": len(operation_ids),
            "attention_count": len(items),
            "healthy_count": healthy_count,
            "category_counts": category_counts,
            "attention_items": items,
            "read_only": True,
            "message": "Attention scan completed without changing workflow state.",
        },
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
    from .task_store import read_complete_task

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
    if self.recovery_request_settler is not None:
        settled_requests = self.recovery_request_settler(operation_id)
        if settled_requests:
            data = dict(data)
            data["settled_service_request_ids"] = settled_requests

    continuation = _command_inspect(
        self,
        trace=AdminTrace(submission_id=operation_id),
        submission_id=operation_id,
    )
    continuation_data = (
        continuation.get("data")
        if isinstance(continuation.get("data"), Mapping)
        else {}
    )
    human_actions = continuation_data.get("human_actions")
    recovery_still_required = bool(
        isinstance(human_actions, list)
        and any(
            isinstance(action, Mapping) and action.get("command") == "recover"
            for action in human_actions
        )
    )
    data = dict(data)
    data["post_recovery"] = {
        "problem": continuation_data.get("problem"),
        "waiting_for": continuation_data.get("waiting_for"),
        "operator_instruction": continuation_data.get("operator_instruction"),
        "task_title": continuation_data.get("task_title"),
        "phase": continuation_data.get("phase"),
        "administrative_blocker": bool(
            continuation_data.get("administrative_blocker")
        ),
        "human_actions": human_actions if isinstance(human_actions, list) else [],
        "agent_actions_now": continuation_data.get("agent_actions_now")
        if isinstance(continuation_data.get("agent_actions_now"), list)
        else [],
    }
    data["recovery_still_required"] = recovery_still_required
    return result_envelope(
        command="recover",
        task_gid=trace.task_gid,
        submission_id=operation_id,
        state=str(continuation.get("state") or view["status"]),
        allowed_actions=list(continuation.get("allowed_actions") or []),
        data=data,
    )



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
    record_human_decision: bool = True,
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
    action = (
        "supply-evidence" if resolution_kind == "evidence"
        else "record-human-decision" if record_human_decision
        else "review-reject"
    )
    data, view = self.operation_service.current.resolve_hold(
        operation_id, action,
        lambda: resolve_hold(
            self.conn, self.backend, operation_id=operation_id, resolution_kind=resolution_kind,
            detail=detail, resume_status=resume_status, honest_root=release.root,
            schema=release.schema, file_path=file_path, editor=editor, model=model, run_id=run_id,
            expected_task_gid=expected_task_gid, expected_cycle_id=expected_cycle_id,
            expected_hold_identity=expected_hold_identity,
            record_human_decision=record_human_decision,
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



def _command_review_queue(
    self, *, trace: AdminTrace, status: str = "active"
) -> dict[str, Any]:
    status_map = {
        "active": ("pending", "approved", "claimed"),
        "pending": ("pending",),
        "approved": ("approved", "claimed"),
        "all": ("pending", "approved", "claimed", "applied", "rejected", "stale"),
    }
    statuses = status_map.get(str(status or "active").strip())
    if statuses is None:
        raise DishRuleError(
            "INVALID_ARGUMENT", "unsupported review queue status",
            rule="semantic_proposal_status_invalid",
            details={"allowed": sorted(status_map)},
        )
    items = list_review_items(
        self.conn,
        proposal_statuses=statuses,
        include_human_holds="pending" in statuses,
    )
    return result_envelope(
        command="review-queue", state="ok",
        data={
            "count": len(items),
            "status_filter": status,
            "proposals": list(items),
            "review_items": list(items),
        },
    )


def _command_review_inspect(
    self, *, trace: AdminTrace, proposal_id: str
) -> dict[str, Any]:
    clean_id = _clean_required(proposal_id, rule="review_item_id_required", label="review item ID")
    item = resolve_review_item(self.conn, clean_id)
    trace.task_gid = item["task_gid"]
    trace.submission_id = item["operation_id"]
    trace.state = item["status"]
    data: dict[str, Any] = {"review_item": item}
    if item["item_type"] == "semantic_proposal":
        data["proposal"] = item
        if self.operation_service is not None and self.operation_service.current is not None:
            from .commands import expose_authoritative_view

            release = None if self.release_loader is None else self.release_loader()
            schema = None if release is None else release.schema
            view = expose_authoritative_view(
                self.operation_service.current.authoritative_view(
                    str(item["operation_id"]), schema=schema
                )
            )
            data["authoritative_view"] = view
            if item.get("status") == "approved" or _is_recoverable_mechanical_proposal_claim(item):
                data["admin_action"] = {
                    "command": "review-approve",
                    "arguments": {"proposal_id": item["proposal_id"]},
                    "effect": "Resume mechanical application of the already-approved exact bundle.",
                }
    else:
        if item["item_type"] == "verification_hold":
            spec = exact_action(
                kind="release-verification-hold",
                command="resolved",
                positional=(item["operation_id"],),
                summary="Release the three-round Verification hold.",
                effect="Return the unchanged candidate to a fresh Verification round.",
                after_success={"instruction": "A later fresh verifier may start Verification."},
            )
        else:
            consequences = human_review_consequence_metadata(item.get("resume_status"))
            approval_consequence = consequences["approval"]
            spec = template_action(
                kind="approve-human-review",
                command="review-approve",
                positional=(item["review_id"],),
                options=(("--reason", "<Marco's decision and brief reason>"),),
                prompt_fields=(
                    PromptField(
                        "reason",
                        "Marco's decision and brief reason",
                        "<Marco's decision and brief reason>",
                    ),
                ),
                summary="Approve this Human Review decision and release the hold.",
                effect=(
                    "Dish records Marco's substantive decision and follows the hold's stored resume route; "
                    "it does not silently edit governed fields."
                ),
                after_success=approval_consequence,
            )
        data.update(spec.payload())
        if item["item_type"] == "human_review":
            dismiss_spec = template_action(
                kind="dismiss-human-review",
                command="review-reject",
                positional=(item["review_id"],),
                options=(("--reason", "<why this escalation is invalid>"),),
                prompt_fields=(PromptField("reason", "Why this escalation is invalid", "<why this escalation is invalid>"),),
                summary="Dismiss this Human Review escalation as invalid.",
                effect=(
                    "Preserve the escalation and dismissal reason in the audit trail, record no Marco decision, "
                    "and release the unchanged task to fresh Verification."
                ),
                after_success=human_review_consequence_metadata(item.get("resume_status"))["dismissal"],
            )
            data["human_actions"] = [
                data["human_action"],
                dismiss_spec.payload()["human_action"] | {"shell_command": dismiss_spec.shell_command()},
            ]
    return result_envelope(
        command="review-inspect", task_gid=item["task_gid"],
        submission_id=item["operation_id"], state=item["status"],
        data=data,
    )


def _command_review_approve(
    self, *, trace: AdminTrace, proposal_id: str, reason: str | None = None, detail: str | None = None
) -> dict[str, Any]:
    if self.backend is None:
        raise DishRuleError(
            "INTERNAL_ERROR", "review approval requires the live task backend",
            rule="semantic_proposal_backend_required",
        )
    from .constants import COOKING_PROJECT_GID
    from .task_store import read_complete_task

    clean_id = _clean_required(proposal_id, rule="review_item_id_required", label="review item ID")
    item = resolve_review_item(self.conn, clean_id)
    if item["item_type"] == "verification_hold":
        result = _command_resolved(self, trace=trace, submission_id=item["operation_id"])
        result["command"] = "review-approve"
        result.setdefault("data", {}).update({
            "effect": "The Verification hold was released without editing or approving the candidate.",
            "next_step": "A later fresh verifier may start Verification.",
        })
        return result
    if item["item_type"] == "human_review":
        legacy_semantic_default = "Approved after reviewing the exact linked change bundle."
        clean_detail = str(detail or reason or "").strip()
        if clean_detail == legacy_semantic_default and not str(detail or "").strip():
            clean_detail = ""
        if not clean_detail:
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "Human Review approval requires Marco's decision",
                rule="human_review_detail_required",
                details={
                    "review_id": item["review_id"],
                    "required_input": "Pass --reason with Marco's decision and brief reasoning.",
                    "inspect_command": f"dish-admin review-inspect {item['review_id']}",
                },
            )
        result = _command_record_human_decision(
            self,
            trace=trace,
            submission_id=item["operation_id"],
            detail=clean_detail,
            resume_status=item["resume_status"] or "pending-verification",
            expected_task_gid=item["task_gid"],
            expected_cycle_id=item["cycle_id"],
            expected_hold_identity=item["hold_identity"],
        )
        # review-approve is the public wrapper Marco invoked. Keep the internal
        # hold-resolution event identity in audit/state, not in the outer result.
        result["command"] = "review-approve"
        actual_resume = result.get("data", {}).get("resume_status")
        consequence = human_review_consequence_metadata(actual_resume)["approval"]
        if consequence["next_stage"] == "research":
            effect = (
                "Marco's substantive decision was recorded. The task returned to Research and the held "
                "Verification operation completed; no governed field was edited or authorized."
            )
            next_step = "Continue the task through Research; do not treat this as a fresh Verification release."
        else:
            effect = (
                "Marco's substantive decision was recorded and the unchanged candidate was released to a fresh "
                "Verification cycle; no governed field was edited or authorized."
            )
            next_step = (
                "An eligible verifier may continue Verification. If the original verifier is "
                "still live and did not materially edit the candidate, that same run may continue."
            )
        result.setdefault("data", {}).update({
            "effect": effect,
            "next_step": next_step,
            "approval_consequence": consequence,
        })
        return result
    if self.release_loader is None:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "semantic proposal application requires the current protocol release",
            rule="semantic_proposal_release_required",
        )
    row = self.conn.execute(
        "SELECT * FROM semantic_proposals WHERE proposal_id=?", (item["proposal_id"],)
    ).fetchone()
    clean_id = item["proposal_id"]
    live = read_complete_task(
        self.backend, task_gid=row["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    clean_reason = str(reason or "").strip() or "Approved after reviewing the exact linked change bundle."
    mechanical_run_id = _mechanical_proposal_run_id(clean_id)
    if row["status"] == "claimed":
        claimed = dict(row)
        if not _is_recoverable_mechanical_proposal_claim(claimed):
            raise DishRuleError(
                "CONFLICT",
                "claimed proposal is not owned by Dish's deterministic mechanical application",
                rule="semantic_proposal_claimed",
                details={
                    "claimed_agent": row["claimed_agent"],
                    "claimed_run_id": row["claimed_run_id"],
                },
            )
        approved = row
    else:
        approved = approve_semantic_proposal(
            self.conn,
            proposal_id=clean_id,
            live_title=live.title,
            live_notes=live.notes,
            reason=clean_reason,
        )
    trace.task_gid = approved["task_gid"]
    trace.submission_id = approved["operation_id"]
    trace.state = approved["status"]

    # Approval is already durable before application begins.  The second action is
    # mechanical: revalidate the exact approved bundle against live authority, install
    # it if still applicable, and open independent Verification.  Keep apply-proposal
    # as a low-level recovery/testing command, but do not require another AI agent for
    # the ordinary approved path.
    release = self.release_loader()
    try:
        applied = apply_semantic_proposal(
            self.conn,
            self.backend,
            proposal_id=clean_id,
            agent=MECHANICAL_PROPOSAL_AGENT,
            model="dish-mechanical",
            run_id=mechanical_run_id,
            request_id=None,
            schema=release.schema,
        )
    except DishRuleError as exc:
        details = dict(exc.details)
        details.update({
            "proposal_id": clean_id,
            "approval_persisted": True,
            "proposal_status": get_semantic_proposal(self.conn, clean_id)["status"],
        })
        raise DishRuleError(
            exc.code,
            str(exc),
            rule=exc.rule,
            retryable=exc.retryable,
            details=details,
            errors=exc.errors,
        ) from exc

    trace.state = "applied"
    return result_envelope(
        command="review-approve", task_gid=approved["task_gid"],
        submission_id=approved["operation_id"], state="applied",
        data={
            "proposal": applied["proposal"],
            "recovered_confirmed_write": bool(applied.get("recovered_confirmed_write")),
            "effect": (
                "Marco's exact proposal approval was recorded, then Dish mechanically "
                "revalidated and applied that same immutable bundle."
            ),
            "new_cycle_id": applied["new_cycle_id"],
            "next_step": (
                "The material editor cannot sign this result. An independent eligible "
                "verifier must review the new Verification cycle."
            ),
        },
    )


def _command_review_reject(
    self, *, trace: AdminTrace, proposal_id: str, reason: str
) -> dict[str, Any]:
    if self.backend is None:
        raise DishRuleError(
            "INTERNAL_ERROR", "proposal rejection requires the live task backend",
            rule="semantic_proposal_backend_required",
        )
    from .constants import COOKING_PROJECT_GID
    from .task_store import read_complete_task

    clean_id = _clean_required(proposal_id, rule="review_item_id_required", label="review item ID")
    item = resolve_review_item(self.conn, clean_id)
    if item["item_type"] == "verification_hold":
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "the three-round Verification hold uses resolved/reopen, not review rejection",
            rule="verification_hold_reject_unsupported",
            details={"review_id": item["review_id"]},
        )
    if item["item_type"] == "human_review":
        result = _resolve_protocol_hold(
            self, trace=trace, submission_id=item["operation_id"],
            resolution_kind="human_review", detail=reason,
            # Dismissing the escalation is not resolving its claimed issue. Always
            # return the unchanged candidate to fresh Verification; the rejected
            # escalation's requested downstream route has no authority here.
            resume_status="pending-verification",
            expected_task_gid=item["task_gid"], expected_cycle_id=item["cycle_id"],
            expected_hold_identity=item["hold_identity"], record_human_decision=False,
        )
        result.setdefault("data", {}).update({
            "effect": (
                "The Human Review escalation was dismissed as invalid. No Marco decision or "
                "governed authorization was recorded; the unchanged candidate was released to fresh Verification."
            ),
            "next_step": (
                "A later fresh verifier should reassess the original issue and must not treat the dismissed finding as settled fact."
            ),
        })
        return result
    clean_id = item["proposal_id"]
    row = self.conn.execute(
        "SELECT * FROM semantic_proposals WHERE proposal_id=?", (clean_id,)
    ).fetchone()
    live = read_complete_task(
        self.backend, task_gid=row["task_gid"], project_gid=COOKING_PROJECT_GID
    )
    rejected, new_cycle = reject_semantic_proposal(
        self.conn, proposal_id=clean_id, reason=reason, live_identity=live.identity
    )
    trace.task_gid = rejected["task_gid"]
    trace.submission_id = rejected["operation_id"]
    trace.state = rejected["status"]
    return result_envelope(
        command="review-reject", task_gid=rejected["task_gid"],
        submission_id=rejected["operation_id"], state=rejected["status"],
        allowed_actions=["start"],
        data={
            "proposal": proposal_payload(self.conn, rejected),
            "completed_cycle_id": rejected["cycle_id"],
            "new_cycle_id": new_cycle["cycle_id"],
            "effect": (
                "The proposal was rejected. No governed authorization or task edit was made, "
                "and the unchanged live candidate was released into a fresh Verification round."
            ),
            "next_step": (
                "A later genuinely fresh agent may start Verification and propose a different "
                "correction that respects Marco's rejection."
            ),
            "agent_action": {
                "command": "start",
                "arguments": {"kind": "verification", "task_gid": rejected["task_gid"]},
            },
        },
    )

def _command_authorize_governed_change(self, *, trace: AdminTrace, submission_id: str, field: str, before: Any, after: Any, reason: str, run_id: str | None = None) -> dict[str, Any]:
    from .database import record_marco_authorization
    operation_id = _clean_required(submission_id, rule="operation_id_required", label="operation ID")
    op = self.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    if op is None:
        raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
    _assert_no_active_semantic_proposal(
        self.conn,
        operation_id,
        requested_command="authorize-governed-change",
        authoritative_view=(
            None
            if self.operation_service is None or self.operation_service.current is None
            else self.operation_service.current.authoritative_view(operation_id)
        ),
    )
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
    _assert_no_active_semantic_proposal(
        self.conn,
        operation_id,
        requested_command="abandon-operation",
        authoritative_view=self.operation_service.current.authoritative_view(operation_id),
    )
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
    _assert_no_active_semantic_proposal(
        self.conn,
        operation_id,
        requested_command="discard",
        authoritative_view=(
            None
            if self.operation_service is None or self.operation_service.current is None
            else self.operation_service.current.authoritative_view(operation_id)
        ),
    )
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

_OPERATION_TARGET_COMMANDS = set(RESOLVED_OPERATION_TARGET_COMMANDS)


CURRENT_ADMIN_COMMAND_HANDLERS = {
    "attention": _command_attention,
    "kill": _command_kill,
    "review-queue": _command_review_queue,
    "review-inspect": _command_review_inspect,
    "review-approve": _command_review_approve,
    "review-reject": _command_review_reject,
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
