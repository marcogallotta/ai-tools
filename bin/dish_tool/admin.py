"""Marco-only lifecycle commands for the separate ``dish-admin`` surface."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Mapping

from .database import get_submission, record_audit, transition_submission
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

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

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
        result = error_envelope(command, error, submission_id=submission_id)
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

    def _command_unblock(
        self,
        *,
        trace: AdminTrace,
        submission_id: str,
        reason: str,
    ) -> dict[str, Any]:
        clean_submission_id = _clean_required(
            submission_id,
            rule="submission_id_required",
            label="submission ID",
        )
        row = get_submission(self.conn, clean_submission_id)
        trace.submission_id = clean_submission_id
        trace.known_submission = True
        trace.task_gid = row["task_gid"]
        trace.state = row["status"]
        if row["status"] != "awaiting_human":
            raise DishRuleError(
                "WRONG_STATE",
                f"submission is {row['status']}, expected awaiting_human",
                rule="wrong_state",
                details={
                    "actual": row["status"],
                    "expected": ["awaiting_human"],
                },
            )
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
            clean_submission_id,
            {"awaiting_human"},
            "drafting",
            updates={"failed_verification_passes": 0},
        )
        trace.state = final["status"]
        return result_envelope(
            command="unblock",
            task_gid=row["task_gid"],
            submission_id=clean_submission_id,
            state=final["status"],
            data={"reason": clean_reason},
        )
