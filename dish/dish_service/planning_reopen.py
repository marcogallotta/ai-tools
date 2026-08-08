"""Request replay and startup reconciliation for Planning reopen effects."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database import (
    planning_reopen_attempt_by_request,
    unresolved_planning_reopen_attempts,
)
from dish_tool.errors import BackendFailure
from dish_tool.invocation_audit import record_invocation_audit
from dish_tool.results import error_envelope, result_envelope
from dish_tool.task_store import (
    planning_reopen_recovery_details,
    planning_reopen_success_data,
    reconcile_planning_reopen_attempt,
)
from dish_tool.transactions import immediate_transaction

from .request_replay import complete_request


class PlanningReopenCoordinator:
    """Own exact-request and startup reconciliation for Planning reopen attempts."""

    def __init__(
        self,
        *,
        backend_factory: Callable[[], Any],
        close_backend: Callable[[Any], None],
    ) -> None:
        self._backend_factory = backend_factory
        self._close_backend = close_backend

    @staticmethod
    def _result(
        attempt: Mapping[str, Any],
        *,
        state: str,
        live=None,
    ) -> dict[str, Any]:
        if state == "confirmed":
            return result_envelope(
                command="reopen-planning",
                task_gid=attempt["task_gid"],
                allowed_actions=["start"],
                data=planning_reopen_success_data(attempt, live=live),
            )
        if state == "not_applied":
            return error_envelope(
                "reopen-planning",
                BackendFailure(
                    "BACKEND_REJECTED",
                    "task completion state was not reopened",
                    rule="planning_reopen_not_applied",
                    retryable=True,
                    details={
                        "attempt_id": attempt["attempt_id"],
                        "task_gid": attempt["task_gid"],
                    },
                ),
                task_gid=attempt["task_gid"],
            )
        raise ValueError(f"cannot build terminal Planning reopen result for {state}")

    @staticmethod
    def _ensure_invocation_audit(
        conn, *, attempt: Mapping[str, Any], result: dict[str, Any]
    ) -> None:
        request_id = str(attempt["request_id"] or "").strip() or None
        if request_id is None:
            return
        existing = conn.execute(
            """SELECT event_id FROM audit_events
                 WHERE event_type='dish-admin.reopen-planning'
                   AND json_extract(details, '$.request_id')=?
                 LIMIT 1""",
            (request_id,),
        ).fetchone()
        if existing is not None:
            return
        record_invocation_audit(
            conn,
            surface="dish-admin",
            command="reopen-planning",
            result=result,
            task_gid=attempt["task_gid"],
            submission_id=None,
            actor_role="marco",
            actor_run_id=attempt["actor_run_id"],
            audit_details={
                "attempt_id": attempt["attempt_id"],
                "request_id": request_id,
                "reason": attempt["reason"],
                "completed_before": True,
                "completed_after": result.get("ok") is True,
                "identity": attempt["expected_identity"],
                "section_gid": attempt["expected_section_gid"],
                "reconciled": True,
            },
        )

    def _complete_request(
        self,
        *,
        conn,
        attempt: Mapping[str, Any],
        state: str,
        live=None,
    ) -> dict[str, Any]:
        result = self._result(attempt, state=state, live=live)
        request_id = str(attempt["request_id"] or "").strip() or None
        with immediate_transaction(conn, "planning_reopen_request_completion"):
            self._ensure_invocation_audit(
                conn, attempt=attempt, result=result
            )
            if request_id:
                result.setdefault("data", {})["request_id"] = request_id
                complete_request(conn, request_id=request_id, result=result)
        return result

    def complete_terminal_request(
        self, *, conn, request_id: str
    ) -> dict[str, Any] | None:
        attempt = planning_reopen_attempt_by_request(conn, request_id=request_id)
        if attempt is None or attempt["outcome"] not in {"confirmed", "not_applied"}:
            return None
        return self._complete_request(
            conn=conn, attempt=attempt, state=attempt["outcome"], live=None
        )

    def reconcile_pending_request(
        self,
        *,
        conn,
        backend,
        request_id: str,
    ) -> dict[str, Any] | None:
        attempt = planning_reopen_attempt_by_request(conn, request_id=request_id)
        if attempt is None:
            # The process died after request journaling but before durable effect
            # intent. The original request may resume through the normal handler.
            return None
        try:
            recovery = reconcile_planning_reopen_attempt(
                conn,
                backend,
                attempt_id=attempt["attempt_id"],
                project_gid=COOKING_PROJECT_GID,
                allow_external_retry=True,
                recovered_by="exact_request_replay",
            )
        except BackendFailure as exc:
            result = error_envelope(
                "reopen-planning", exc, task_gid=attempt["task_gid"]
            )
            result.setdefault("data", {}).update(exc.details)
            result["data"]["request_id"] = request_id
            # An unresolved Planning reopen remains pending so a later exact
            # replay can converge it when live evidence becomes authoritative.
            if exc.code != "BACKEND_UNCERTAIN":
                with immediate_transaction(
                    conn, "planning_reopen_failed_request_completion"
                ):
                    self._ensure_invocation_audit(
                        conn, attempt=attempt, result=result
                    )
                    complete_request(conn, request_id=request_id, result=result)
            return result
        state = recovery["state"]
        return self._complete_request(
            conn=conn,
            attempt=recovery["attempt"],
            state=state,
            live=recovery["live"],
        )

    def reconcile_startup(self, conn) -> dict[str, Any]:
        attempts = unresolved_planning_reopen_attempts(conn)
        summary: dict[str, Any] = {
            "discovered": len(attempts),
            "confirmed": 0,
            "not_applied": 0,
            "resume_safe": 0,
            "applied_pending_replay": 0,
            "uncertain": 0,
            "pending": [],
            "errors": [],
        }
        if not attempts:
            return summary

        live_attempts = []
        for attempt in attempts:
            if attempt["outcome"] in {"confirmed", "not_applied"}:
                try:
                    self._complete_request(
                        conn=conn, attempt=attempt, state=attempt["outcome"], live=None
                    )
                    summary[attempt["outcome"]] += 1
                except Exception as exc:
                    summary["errors"].append({
                        "attempt_id": attempt["attempt_id"],
                        "task_gid": attempt["task_gid"],
                        "error_type": type(exc).__name__,
                    })
            else:
                live_attempts.append(attempt)
        if not live_attempts:
            return summary

        try:
            backend = self._backend_factory()
        except Exception as exc:
            summary["errors"].append({"error_type": type(exc).__name__})
            summary["uncertain"] += len(live_attempts)
            for attempt in live_attempts:
                details = planning_reopen_recovery_details(attempt)
                details.update({
                    "observed_state": "backend_unavailable",
                    "error_type": type(exc).__name__,
                })
                summary["pending"].append(details)
            return summary
        try:
            for attempt in live_attempts:
                try:
                    recovery = reconcile_planning_reopen_attempt(
                        conn,
                        backend,
                        attempt_id=attempt["attempt_id"],
                        project_gid=COOKING_PROJECT_GID,
                        allow_external_retry=False,
                        recovered_by="service_startup",
                        finalize_observed_applied=False,
                    )
                    state = recovery["state"]
                    summary[state] += 1
                    if state in {"confirmed", "not_applied"}:
                        self._complete_request(
                            conn=conn,
                            attempt=recovery["attempt"],
                            state=state,
                            live=recovery["live"],
                        )
                    else:
                        recovery_attempt = dict(recovery["attempt"])
                        recovery_attempt["request_status"] = attempt["request_status"]
                        details = planning_reopen_recovery_details(
                            recovery_attempt,
                            safe_to_resume=state == "resume_safe",
                        )
                        details["observed_state"] = state
                        summary["pending"].append(details)
                except BackendFailure as exc:
                    summary["uncertain"] += 1
                    details = planning_reopen_recovery_details(attempt)
                    details.update({
                        "observed_state": "uncertain",
                        "code": exc.code,
                        "rule": exc.rule,
                    })
                    summary["pending"].append(details)
                    summary["errors"].append({
                        "attempt_id": attempt["attempt_id"],
                        "task_gid": attempt["task_gid"],
                        "code": exc.code,
                        "rule": exc.rule,
                    })
                except Exception as exc:
                    summary["uncertain"] += 1
                    details = planning_reopen_recovery_details(attempt)
                    details.update({
                        "observed_state": "reconciliation_error",
                        "error_type": type(exc).__name__,
                    })
                    summary["pending"].append(details)
                    summary["errors"].append({
                        "attempt_id": attempt["attempt_id"],
                        "task_gid": attempt["task_gid"],
                        "error_type": type(exc).__name__,
                    })
        finally:
            self._close_backend(backend)
        return summary

