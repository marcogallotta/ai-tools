"""Transport-neutral authority for current dish workflow operations."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Callable, TypeVar

from .database_schema import _validate_semantic_evidence
from .errors import DishRuleError
from .execution_provenance import operation_execution_provenance
from .legacy_adapter import LegacyReadOnlyAdapter
from .operation_execution import (
    claim_abandonment_execution,
    claim_operation_execution,
    execution_recovery_state,
    finish_operation_execution,
    partial_write_error,
)
from .abandonment_view import apply_abandonment_view
from .workflow_snapshot import build_workflow_snapshot

T = TypeVar("T")


def _failure_rule_for_exception(exc: BaseException) -> str:
    """Map an execution failure to a durable-evidence rule label.

    A raw ``sqlite3.OperationalError`` collapses distinct writer-contention
    conditions (SQLITE_BUSY/SQLITE_LOCKED) into one generic type name unless
    the category is extracted here before it is discarded.
    """
    if isinstance(exc, DishRuleError):
        return exc.rule
    if isinstance(exc, sqlite3.OperationalError):
        message = str(exc).lower()
        error_code = getattr(exc, "sqlite_errorcode", None)
        primary_code = None if error_code is None else error_code & 0xFF
        if primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or (
            "locked" in message or "busy" in message
        ):
            return "OperationalError:database_writer_lock"
    return type(exc).__name__


@dataclass(frozen=True)
class RoutedTarget:
    generation: str
    row: sqlite3.Row | None


class CurrentWorkflowService:
    """Single authorization and result-state boundary for current mutations."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        backend,
        *,
        request_id: str | None = None,
    ) -> None:
        self.conn = conn
        self.backend = backend
        self.request_id = str(request_id or "").strip() or None

    def operation(self, operation_id: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
        return row

    def authoritative_view(self, operation_id: str, *, schema=None) -> dict[str, object]:
        snapshot, facts = build_workflow_snapshot(
            self.conn,
            self.backend,
            operation_id,
            self.operation(operation_id),
            schema=schema,
        )
        return apply_abandonment_view(
            self.conn, operation_id, snapshot, facts
        )

    def assert_action(self, operation_id: str, action: str, *, schema=None) -> dict[str, object]:
        view = self.authoritative_view(operation_id, schema=schema)
        if action not in view["legal_actions"]:
            proposal = view.get("semantic_proposal")
            if isinstance(proposal, dict):
                proposal_status = str(proposal.get("status") or "")
                if action == "apply-proposal":
                    block = proposal.get("block")
                    if isinstance(block, dict):
                        block_rule = block.get("rule")
                        raise DishRuleError(
                            str(block.get("code") or "CONFLICT"),
                            str(block.get("message") or "semantic proposal is not currently applicable"),
                            rule=None if block_rule is None else str(block_rule),
                            retryable=block.get("retryable"),
                            details=(
                                block.get("details")
                                if isinstance(block.get("details"), dict)
                                else {}
                            ),
                            errors=(
                                block.get("errors")
                                if isinstance(block.get("errors"), list)
                                else ()
                            ),
                        )
                    if proposal_status == "pending":
                        raise DishRuleError(
                            "WRONG_STATE",
                            "proposal is not approved and claimable",
                            rule="semantic_proposal_not_claimable",
                            details={"status": proposal_status},
                        )
                    if proposal_status == "claimed":
                        raise DishRuleError(
                            "CONFLICT",
                            "approved proposal is already claimed by another run",
                            rule="semantic_proposal_claimed",
                            details={"claimed_run_id": proposal.get("claimed_run_id")},
                        )
                if proposal_status in {"pending", "approved", "claimed"}:
                    proposal_actionable = "apply-proposal" in view["legal_actions"]
                    if proposal_status == "pending":
                        next_action = "review-inspect"
                        instruction = (
                            f"Marco must review proposal {proposal.get('proposal_id')}."
                        )
                    elif proposal_actionable:
                        next_action = "apply-proposal"
                        instruction = (
                            "A fresh eligible run must apply proposal "
                            f"{proposal.get('proposal_id')} exactly as stored."
                        )
                    else:
                        next_action = "inspect"
                        instruction = (
                            "Inspect the proposal's authoritative block before attempting "
                            "another workflow action."
                        )
                    details = {
                        "proposal_id": proposal.get("proposal_id"),
                        "proposal_status": proposal_status,
                        "required_action": next_action,
                        "instruction": instruction,
                    }
                    if not proposal_actionable and isinstance(proposal.get("block"), dict):
                        details["proposal_block"] = proposal["block"]
                    raise DishRuleError(
                        "WRONG_STATE",
                        "this task is parked on a durable semantic proposal",
                        rule="semantic_proposal_application_required",
                        retryable=False,
                        details=details,
                    )
            code = "WRONG_STATE"
            rule = "operation_action_not_allowed"
            message = f"{action} is not legal for the current operation state"
            # Terminal operations remain terminal even if their live task later
            # drifts. Report the lifecycle error before active-operation drift
            # diagnostics so retries receive a stable WRONG_STATE result.
            if view.get("status") not in {"open", "uncertain"}:
                pass
            elif view.get("recovery_required"):
                rule = "workflow_recovery_required"
                message = "operation requires recovery or migration reconciliation before any ordinary action"
            elif not view.get("identity_matches", True):
                code = "CONFLICT"
                if view.get("phase") in {"await_submission", "ready_move_failed"}:
                    rule = "post_signoff_content_drift"
                    message = "live content no longer matches the exact signed candidate"
                else:
                    rule = "live_task_drift"
                    message = "live task content does not match the authoritative workflow identity"
            elif not view.get("placement_matches", True):
                if view.get("phase") == "await_verification":
                    rule = "verification_placement_required"
                    message = "task must currently be in Verification Queue"
                else:
                    code = "CONFLICT"
                    rule = "live_task_placement_drift"
                    message = "live task placement does not match the authoritative workflow placement"
            elif (
                action in {"approve", "reject"}
                and view.get("phase") == "await_verification"
                and view.get("cycle_reviewed")
                and not view.get("dish_inspect_current")
            ):
                rule = "dish_inspect_required"
                message = "approve or reject requires a current dish inspect fact"
            elif not view.get("required_cycle_exists", True):
                rule = "verification_cycle_missing"
                message = "the required Verification cycle is missing"
            elif not view.get("signoff_bound", True):
                rule = "signoff_not_completed"
                message = "submission requires durable exact-content signoff"
            raise DishRuleError(
                code, message, rule=rule,
                details={
                    "action": action,
                    "operation_id": operation_id,
                    "authoritative_view": view,
                },
            )
        return view

    def _post_operation_view(
        self,
        operation_id: str,
        result: T,
        *,
        schema=None,
    ) -> tuple[T, dict[str, object]]:
        """Return a post-operation view without converting committed success into failure."""
        try:
            return result, self.authoritative_view(operation_id, schema=schema)
        except Exception as exc:
            try:
                op = self.operation(operation_id)
                status = op["status"]
                phase = op["phase"]
            except Exception:
                status = None
                phase = None
            error = {"type": type(exc).__name__, "message": str(exc)}
            if isinstance(result, dict):
                result = dict(result)
                result.update({
                    "view_refresh_required": True,
                    "view_refresh_error": error,
                })
            fallback = {
                "status": status,
                "phase": phase,
                "legal_actions": [],
                "recovery_required": False,
                "view_refresh_required": True,
                "view_refresh_error": error,
            }
            return result, fallback

    def _execute_claimed(
        self,
        operation_id: str,
        command: str,
        executor: Callable[[], T],
        *,
        schema=None,
        assert_action: bool = True,
        claim_request_id: str | None | object = ...,
        abandonment_id: str | None = None,
        abandonment_execution_id: str | None = None,
    ) -> tuple[T, dict[str, object]]:
        execution_request_id = (
            self.request_id if claim_request_id is ... else claim_request_id
        )
        if abandonment_id is not None and abandonment_execution_id is not None:
            claim = claim_abandonment_execution(
                self.conn,
                abandonment_id=abandonment_id,
                execution_id=abandonment_execution_id,
            )
        else:
            claim = claim_operation_execution(
                self.conn,
                operation_id=operation_id,
                command=command,
                request_id=execution_request_id,
            )
        result: T
        try:
            if assert_action and not claim.resuming_uncertain:
                self.assert_action(operation_id, command, schema=schema)
            with operation_execution_provenance(
                self.conn,
                execution_id=claim.execution_id,
                operation_id=operation_id,
            ):
                result = executor()
            recovered_small_signoff = bool(
                command == "recover"
                and isinstance(result, dict)
                and any(
                    isinstance(action, dict)
                    and action.get("kind") == "workflow_step"
                    and action.get("step") == "small_signoff"
                    for action in result.get("actions", ())
                )
            )
            if command == "approve" or recovered_small_signoff:
                # Approval is not authoritative success until the complete
                # reviewed/corrected/signed evidence graph validates. Run this
                # before completing the execution journal or returning OK so a
                # broken approval is reported on the request that created it,
                # including restart recovery that finishes a Small signoff.
                _validate_semantic_evidence(self.conn)
        except Exception as exc:
            recovery = execution_recovery_state(
                self.conn,
                execution_id=claim.execution_id,
                failure_rule=_failure_rule_for_exception(exc),
                refresh=claim.resuming_uncertain,
            )
            if claim.resuming_uncertain and recovery is not None:
                recovery = dict(recovery)
                recovery.setdefault("root_command", recovery.get("command"))
                recovery["command"] = claim.command
            controlled_failure = isinstance(exc, DishRuleError) and exc.code in {
                "INVALID_ARGUMENT",
                "VALIDATION_FAILED",
                "CONFLICT",
                "WRONG_STATE",
                "AGENT_MISMATCH",
                "CONFIRMATION_REQUIRED",
                "BACKEND_REJECTED",
                "NOT_FOUND",
            }
            replayed_authoritative_failure = bool(
                claim.resuming_uncertain
                and controlled_failure
                and recovery is not None
                and not recovery.get("pending_steps")
                and recovery.get("write_state") != "uncertain"
                and recovery.get("movement_state") != "uncertain"
                and not (
                    isinstance(exc, DishRuleError)
                    and (
                        exc.code == "BACKEND_UNCERTAIN"
                        or exc.rule == "database_semantic_evidence_invalid"
                    )
                )
            )
            replayable_no_effect_failure = bool(
                command == "reject"
                and claim.request_id
                and recovery is not None
                and not recovery["recovery_required"]
                and not recovery["effects_observed"]
                and not controlled_failure
            )
            if replayable_no_effect_failure:
                recovery = dict(recovery)
                recovery.update({
                    "request_replay_required": True,
                    "required_next_action": "retry_exact_request",
                    "safe_to_retry": True,
                })
            partial_failure = bool(
                recovery is not None
                and not replayed_authoritative_failure
                and (
                    replayable_no_effect_failure
                    or (
                        recovery["recovery_required"]
                        and (
                            not controlled_failure
                            or recovery["write_state"] in {"confirmed", "uncertain"}
                            or recovery["movement_state"]
                            in {"confirmed", "uncertain"}
                            or (
                                isinstance(exc, DishRuleError)
                                and exc.code == "BACKEND_UNCERTAIN"
                            )
                        )
                    )
                )
            )
            if partial_failure:
                try:
                    finish_operation_execution(
                        self.conn, claim, status="uncertain", evidence=recovery
                    )
                except Exception as journal_error:
                    recovery = dict(recovery)
                    recovery["execution_journal_completion_failed"] = {
                        "type": type(journal_error).__name__,
                        "message": str(journal_error),
                    }
                raise partial_write_error(exc, recovery) from exc
            try:
                finish_operation_execution(
                    self.conn,
                    claim,
                    status="completed",
                    evidence=(recovery if claim.resuming_uncertain else None),
                )
            except Exception:
                raise
            raise

        release_error: Exception | None = None
        try:
            resolution = (
                execution_recovery_state(
                    self.conn, execution_id=claim.execution_id, refresh=True
                )
                if claim.resuming_uncertain
                else None
            )
            finish_operation_execution(
                self.conn, claim, status="completed", evidence=resolution
            )
        except Exception as exc:
            release_error = exc
        result, view = self._post_operation_view(operation_id, result, schema=schema)
        if release_error is not None:
            recovery = execution_recovery_state(
                self.conn, execution_id=claim.execution_id,
                failure_rule="operation_execution_completion_lost",
            )
            if isinstance(result, dict):
                result = dict(result)
                result.update({
                    "operation_execution_recovery_required": True,
                    "operation_execution_release_error": {
                        "type": type(release_error).__name__,
                        "message": str(release_error),
                    },
                    "operation_execution_recovery": recovery,
                })
            view = dict(view)
            view.update({
                "legal_actions": [],
                "recovery_required": True,
                "operation_execution_recovery_required": True,
            })
        return result, view

    def mutate(
        self,
        operation_id: str,
        action: str,
        executor: Callable[[], T],
        *,
        schema=None,
    ) -> tuple[T, dict[str, object]]:
        """Authorize and execute exactly one operation mutation at a time."""
        return self._execute_claimed(
            operation_id, action, executor, schema=schema, assert_action=True
        )

    def prepare(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, "prepare", executor, schema=schema)

    def start_verification(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, "verify", executor, schema=schema)

    def approve(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, "approve", executor, schema=schema)

    def reject(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, "reject", executor, schema=schema)

    def apply_proposal(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, "apply-proposal", executor, schema=schema)

    def submit(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, "submit", executor, schema=schema)

    def repair_destination(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, "repair-destination", executor, schema=schema)

    def create_task(self, executor: Callable[[], T]) -> T:
        return executor()

    def start_operation(self, executor: Callable[[], T]) -> T:
        return executor()

    def resolve_hold(self, operation_id: str, action: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, action, executor, schema=schema)

    def reopen_verification_hold(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        return self.mutate(operation_id, "reopen", executor, schema=schema)

    def recover(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        self.operation(operation_id)
        return self._execute_claimed(
            operation_id, "recover", executor, schema=schema, assert_action=False
        )

    def classify_abandonment(self, abandonment_id: str):
        """Return the internal clean/committed abandonment frontier."""
        from .abandonment import classify_abandonment_frontier

        return classify_abandonment_frontier(
            self.conn, self.backend, abandonment_id=abandonment_id
        )

    def settle_abandonment_frontier(
        self, abandonment_id: str, *, reason: str
    ):
        """Settle one already-authorized abandonment frontier."""
        from .abandonment import settle_abandonment_frontier

        return settle_abandonment_frontier(
            self.conn,
            self.backend,
            abandonment_id=abandonment_id,
            reason=reason,
        )

    def abandon_operation(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        """Create and settle one permanent-run abandonment under execution authority."""
        self.operation(operation_id)
        return self._execute_claimed(
            operation_id,
            "abandon-operation",
            executor,
            schema=schema,
            assert_action=False,
        )

    def reconcile_abandonment(
        self, operation_id: str, executor: Callable[[], T], *, schema=None
    ):
        """Resume one blocked or interrupted abandonment under execution authority."""
        self.operation(operation_id)
        return self._execute_claimed(
            operation_id,
            "reconcile-abandonment",
            executor,
            schema=schema,
            assert_action=False,
        )

    def resume_abandonment_execution(
        self,
        operation_id: str,
        abandonment_id: str,
        execution_id: str,
        executor: Callable[[], T],
        *,
        schema=None,
    ):
        """Reclaim the exact crashed admin execution instead of creating a chain."""
        self.operation(operation_id)
        execution = self.conn.execute(
            "SELECT * FROM operation_executions WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        if (
            execution is None
            or execution["operation_id"] != operation_id
            or execution["command"] not in {
                "abandon-operation",
                "reconcile-abandonment",
            }
            or execution["status"] not in {"started", "uncertain"}
        ):
            raise DishRuleError(
                "CONFLICT",
                "abandonment execution is not resumable",
                rule="abandonment_execution_not_resumable",
                details={"execution_id": execution_id},
            )
        return self._execute_claimed(
            operation_id,
            execution["command"],
            executor,
            schema=schema,
            assert_action=False,
            claim_request_id=execution["request_id"],
            abandonment_id=abandonment_id,
            abandonment_execution_id=execution_id,
        )

    def cancel(self, operation_id: str, executor: Callable[[], T], *, schema=None):
        def checked() -> T:
            op = self.operation(operation_id)
            if op["status"] not in {"open", "uncertain"}:
                raise DishRuleError(
                    "WRONG_STATE",
                    "operation is not cancellable",
                    rule="operation_not_cancellable",
                )
            return executor()

        return self._execute_claimed(
            operation_id, "cancel", checked, schema=schema, assert_action=False
        )


class OperationApplicationService:
    """Generation router plus the current workflow mutation authority."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        backend=None,
        *,
        request_id: str | None = None,
    ) -> None:
        self.conn = conn
        self.legacy = LegacyReadOnlyAdapter(conn)
        self.current = (
            None
            if backend is None
            else CurrentWorkflowService(conn, backend, request_id=request_id)
        )

    def route(self, identifier: str, *, command: str, protocol_version: str) -> RoutedTarget:
        operation = self.conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (identifier,)
        ).fetchone()
        if operation is not None:
            return RoutedTarget("operation", operation)
        legacy = self.legacy.get(identifier)
        if legacy is not None:
            self.legacy.assert_command_allowed(command, protocol_version=protocol_version)
            return RoutedTarget("legacy", legacy)
        return RoutedTarget("missing", None)

    def authoritative_view(self, operation_id: str, *, schema=None) -> dict[str, object]:
        if self.current is None:
            raise RuntimeError("current workflow service requires a backend")
        return self.current.authoritative_view(operation_id, schema=schema)


def derive_operation_state(conn: sqlite3.Connection, backend, operation_id: str, *, schema=None) -> dict[str, object]:
    """Compatibility wrapper for callers not yet constructed with the service."""
    return CurrentWorkflowService(conn, backend).authoritative_view(operation_id, schema=schema)
