"""Read-only submission and destination authority derived from durable evidence."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from .errors import DishRuleError


@dataclass(frozen=True)
class SubmissionAuthorityFacts:
    approved_identity: str
    approved_cycle_id: str
    effective_identity: str
    destination_repair: dict[str, Any] | None
    movement_failure: dict[str, Any] | None

    @property
    def destination_repair_required(self) -> bool:
        return bool(
            self.movement_failure is not None
            and not bool(self.movement_failure.get("movement_retry_safe"))
        )

    def identity_evidence(self) -> dict[str, Any]:
        return {
            "approved_identity": self.approved_identity,
            "approved_cycle_id": self.approved_cycle_id,
            "effective_identity": self.effective_identity,
            "destination_repair": self.destination_repair,
        }


def approved_signoff(conn: sqlite3.Connection, operation_id: str):
    row = conn.execute(
        """SELECT cycle.*, version.identity AS version_identity,
                  version.confirmed AS version_confirmed,
                  version.task_gid AS version_task_gid
             FROM verification_cycles AS cycle
             JOIN content_versions AS version
               ON version.content_version_id=cycle.signed_content_version_id
            WHERE cycle.operation_id=? AND cycle.outcome='approved'
              AND cycle.completed_at IS NOT NULL
            ORDER BY cycle.completed_at DESC, cycle.rowid DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if (
        row is None
        or not row["signed_identity"]
        or not row["signed_content_version_id"]
        or row["version_confirmed"] != 1
        or row["version_identity"] != row["signed_identity"]
        or row["version_task_gid"] != row["task_gid"]
    ):
        raise DishRuleError(
            "CONFLICT",
            "confirmed signed content version is missing or inconsistent",
            rule="signed_content_binding_invalid",
        )
    return row


def submission_identity_evidence(
    conn: sqlite3.Connection, operation_id: str
) -> dict[str, Any]:
    """Return immutable approval plus any completed Marco destination-repair chain."""

    approved = approved_signoff(conn, operation_id)
    approved_identity = approved["signed_identity"]
    effective_identity = approved_identity
    latest_repair = None
    rows = conn.execute(
        """SELECT attempt.*, version.identity AS version_identity,
                  version.confirmed AS version_confirmed
             FROM write_attempts AS attempt
             JOIN content_versions AS version
               ON version.content_version_id=attempt.confirmed_content_version_id
            WHERE attempt.operation_id=?
              AND attempt.purpose='destination_repair'
              AND attempt.outcome='confirmed'
            ORDER BY attempt.started_at, attempt.rowid""",
        (operation_id,),
    ).fetchall()
    for row in rows:
        context = json.loads(row["context_json"] or "{}")
        step_name = str(context.get("repair_step") or "")
        completed_step = (
            None
            if not step_name
            else conn.execute(
                "SELECT completed_at FROM operation_steps WHERE operation_id=? AND step_name=?",
                (operation_id, step_name),
            ).fetchone()
        )
        if completed_step is None or completed_step["completed_at"] is None:
            continue
        if (
            context.get("authorization_kind") != "marco_destination_repair"
            or context.get("approved_identity") != approved_identity
            or context.get("source_identity") != effective_identity
            or row["expected_identity"] != effective_identity
            or row["intended_identity"] != row["version_identity"]
            or row["version_confirmed"] != 1
        ):
            raise DishRuleError(
                "CONFLICT",
                "destination repair evidence is inconsistent",
                rule="destination_repair_evidence_invalid",
            )
        effective_identity = row["intended_identity"]
        latest_repair = {
            "repair_step": step_name,
            "source_identity": context.get("source_identity"),
            "repaired_identity": effective_identity,
            "before_destination": context.get("before_destination"),
            "after_destination": context.get("after_destination"),
            "reason": context.get("reason"),
            "actor_run_id": context.get("actor_run_id"),
            "write_attempt_id": row["attempt_id"],
            "content_version_id": row["confirmed_content_version_id"],
        }
    return {
        "approved_identity": approved_identity,
        "approved_cycle_id": approved["cycle_id"],
        "effective_identity": effective_identity,
        "destination_repair": latest_repair,
    }


def latest_destination_failure(
    conn: sqlite3.Connection, operation_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        """SELECT details FROM audit_events
             WHERE operation_id=? AND event_type='operation.destination_movement_failed'
             ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (operation_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        details = json.loads(row["details"])
    except (TypeError, json.JSONDecodeError):
        return None
    return details if isinstance(details, dict) else None


def submission_authority_facts(
    conn: sqlite3.Connection, operation_id: str
) -> SubmissionAuthorityFacts:
    identity = submission_identity_evidence(conn, operation_id)
    return SubmissionAuthorityFacts(
        approved_identity=str(identity["approved_identity"]),
        approved_cycle_id=str(identity["approved_cycle_id"]),
        effective_identity=str(identity["effective_identity"]),
        destination_repair=identity["destination_repair"],
        movement_failure=latest_destination_failure(conn, operation_id),
    )
