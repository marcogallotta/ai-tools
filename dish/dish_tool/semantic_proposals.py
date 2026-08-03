"""Durable semantic proposals, atomic Marco approval, and claimable application state."""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Mapping, Sequence

from .database import create_verification_cycle, record_audit, transition_operation, utc_now
from .errors import DishRuleError
from .transactions import immediate_transaction

_ACTIVE = ("pending", "approved", "claimed")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _row_dict(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()} if hasattr(row, "keys") else dict(row)


def proposal_changes(conn: sqlite3.Connection, proposal_id: str) -> tuple[dict[str, Any], ...]:
    rows = conn.execute(
        """SELECT ordinal,field_name,before_json,after_json
             FROM semantic_proposal_changes
            WHERE proposal_id=? ORDER BY ordinal""",
        (proposal_id,),
    ).fetchall()
    return tuple(
        {
            "ordinal": int(row["ordinal"]),
            "field": row["field_name"],
            "before": json.loads(row["before_json"]),
            "after": json.loads(row["after_json"]),
        }
        for row in rows
    )


def proposal_payload(
    conn: sqlite3.Connection,
    row: sqlite3.Row | Mapping[str, Any],
    *,
    include_candidate: bool = True,
) -> dict[str, Any]:
    item = _row_dict(row)
    item["explanation"] = json.loads(item.pop("explanation_json"))
    item["linked_changes"] = json.loads(item.pop("linked_changes_json"))
    item["changes"] = list(proposal_changes(conn, item["proposal_id"]))
    # The frozen protocol text is already durably bound to the referenced
    # Verification cycle.  It is not review-queue content and can be very
    # large, so never expose it through operator/agent proposal payloads.
    item.pop("protocol_text", None)
    if not include_candidate:
        item.pop("candidate_notes", None)
    return item


def get_semantic_proposal(conn: sqlite3.Connection, proposal_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM semantic_proposals WHERE proposal_id=?", (proposal_id,)
    ).fetchone()
    if row is None:
        raise DishRuleError(
            "NOT_FOUND", "semantic proposal not found", rule="semantic_proposal_not_found",
            details={"proposal_id": proposal_id},
        )
    return row


def list_semantic_proposals(
    conn: sqlite3.Connection, *, statuses: Sequence[str] = _ACTIVE
) -> tuple[dict[str, Any], ...]:
    clean = tuple(dict.fromkeys(str(status).strip() for status in statuses if str(status).strip()))
    if not clean:
        return ()
    placeholders = ",".join("?" for _ in clean)
    rows = conn.execute(
        f"""SELECT proposal.*,
                   operation.status AS operation_status,
                   operation.phase AS operation_phase
              FROM semantic_proposals AS proposal
              JOIN operations AS operation ON operation.operation_id=proposal.operation_id
             WHERE proposal.status IN ({placeholders})
             ORDER BY proposal.created_at, proposal.proposal_id""",
        clean,
    ).fetchall()
    return tuple(proposal_payload(conn, row, include_candidate=False) for row in rows)


def active_proposal_for_operation(
    conn: sqlite3.Connection,
    operation_id: str,
    *,
    statuses: Sequence[str] = _ACTIVE,
) -> sqlite3.Row | None:
    clean = tuple(dict.fromkeys(statuses))
    placeholders = ",".join("?" for _ in clean)
    rows = conn.execute(
        f"""SELECT * FROM semantic_proposals
              WHERE operation_id=? AND status IN ({placeholders})
              ORDER BY created_at DESC, proposal_id DESC LIMIT 2""",
        (operation_id, *clean),
    ).fetchall()
    if len(rows) > 1:
        raise DishRuleError(
            "CONFLICT",
            "multiple active semantic proposals require operator review",
            rule="semantic_proposal_active_ambiguous",
            details={"operation_id": operation_id, "proposal_ids": [row["proposal_id"] for row in rows]},
        )
    return None if not rows else rows[0]


def queue_semantic_proposal(
    conn: sqlite3.Connection,
    *,
    task_gid: str,
    operation_id: str,
    cycle_id: str,
    baseline_identity: str,
    candidate_identity: str,
    candidate_title: str,
    candidate_notes: str,
    proposal_reason: str,
    explanation: Mapping[str, Any],
    linked_changes: Sequence[Mapping[str, Any]],
    changes: Sequence[Mapping[str, Any]],
    protocol_release: str,
    protocol_text: str,
    proposer_agent: str,
    proposer_run_id: str,
) -> sqlite3.Row:
    """Persist one exact pending proposal and detach it from proposer lease ownership."""
    if not changes:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "semantic proposal requires at least one governed change",
            rule="semantic_proposal_changes_required",
        )
    if not str(proposal_reason or "").strip():
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "semantic proposal requires a concrete reason",
            rule="semantic_proposal_reason_required",
        )
    proposal_id = str(uuid.uuid4())
    now = utc_now()
    with immediate_transaction(conn, "queue_semantic_proposal"):
        operation = conn.execute(
            "SELECT task_gid,status,phase FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if operation is None:
            raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
        if operation["task_gid"] != task_gid or operation["status"] != "open":
            raise DishRuleError(
                "WRONG_STATE", "semantic proposal requires the exact open operation",
                rule="semantic_proposal_operation_invalid",
                details={"operation_id": operation_id, "status": operation["status"]},
            )
        cycle = conn.execute(
            """SELECT * FROM verification_cycles
                 WHERE cycle_id=? AND operation_id=? AND task_gid=? AND completed_at IS NULL""",
            (cycle_id, operation_id, task_gid),
        ).fetchone()
        if cycle is None:
            raise DishRuleError(
                "WRONG_STATE", "semantic proposal requires the exact open Verification cycle",
                rule="semantic_proposal_cycle_invalid",
            )
        existing = conn.execute(
            """SELECT * FROM semantic_proposals
                 WHERE operation_id=? AND status IN ('pending','approved','claimed')
                 ORDER BY created_at, proposal_id LIMIT 1""",
            (operation_id,),
        ).fetchone()
        if existing is not None:
            if existing["candidate_identity"] == candidate_identity:
                return existing
            raise DishRuleError(
                "CONFLICT",
                "this operation is already parked on a different active semantic proposal",
                rule="semantic_proposal_operation_parked",
                details={
                    "operation_id": operation_id,
                    "proposal_id": existing["proposal_id"],
                    "proposal_status": existing["status"],
                    "existing_candidate_identity": existing["candidate_identity"],
                    "proposed_candidate_identity": candidate_identity,
                },
            )
        proposed_changes = [
            {
                "ordinal": ordinal,
                "field": str(change["field"]),
                "before": change["before"],
                "after": change["after"],
            }
            for ordinal, change in enumerate(changes)
        ]
        proposed_linked = [dict(item) for item in linked_changes]
        rejected_rows = conn.execute(
            """SELECT proposal_id,review_reason,reviewed_at,linked_changes_json
                 FROM semantic_proposals
                WHERE operation_id=? AND baseline_identity=? AND status='rejected'
                ORDER BY reviewed_at DESC, proposal_id DESC""",
            (operation_id, baseline_identity),
        ).fetchall()
        rejected = next(
            (
                item
                for item in rejected_rows
                if list(proposal_changes(conn, item["proposal_id"])) == proposed_changes
                and json.loads(item["linked_changes_json"]) == proposed_linked
            ),
            None,
        )
        if rejected is not None:
            raise DishRuleError(
                "CONFLICT",
                "Marco already rejected this exact semantic change bundle",
                rule="semantic_proposal_previously_rejected",
                details={
                    "operation_id": operation_id,
                    "proposal_id": rejected["proposal_id"],
                    "rejected_at": rejected["reviewed_at"],
                    "rejection_reason": rejected["review_reason"],
                    "required_change": (
                        "Propose a materially different change bundle or identify the new evidence "
                        "that changes the previously rejected interpretation."
                    ),
                },
            )
        conn.execute(
            """INSERT INTO semantic_proposals(
                   proposal_id,task_gid,operation_id,cycle_id,baseline_identity,
                   candidate_identity,candidate_title,candidate_notes,proposal_reason,
                   explanation_json,linked_changes_json,protocol_release,protocol_text,
                   correction_class,proposer_agent,proposer_run_id,status,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                proposal_id, task_gid, operation_id, cycle_id, baseline_identity,
                candidate_identity, candidate_title, candidate_notes, str(proposal_reason).strip(),
                _json(dict(explanation)), _json([dict(item) for item in linked_changes]),
                protocol_release, protocol_text, "large", proposer_agent, proposer_run_id,
                "pending", now,
            ),
        )
        for ordinal, change in enumerate(changes):
            conn.execute(
                """INSERT INTO semantic_proposal_changes(
                       proposal_id,ordinal,field_name,before_json,after_json
                   ) VALUES (?,?,?,?,?)""",
                (
                    proposal_id, ordinal, str(change["field"]),
                    _json(change["before"]), _json(change["after"]),
                ),
            )
        # The proposal is now the durable continuation point. The proposing run
        # no longer owns the operation and cannot race the eventual applicant.
        conn.execute(
            """UPDATE service_leases
                  SET released_at=?, release_reason='semantic_proposal_queued'
                WHERE operation_id=? AND released_at IS NULL""",
            (now, operation_id),
        )
        record_audit(
            conn, submission_id=None, task_gid=task_gid, operation_id=operation_id,
            event_type="semantic_proposal.queued", actor_agent=proposer_agent,
            actor_run_id=proposer_run_id,
            details={
                "proposal_id": proposal_id,
                "cycle_id": cycle_id,
                "candidate_identity": candidate_identity,
                "change_fields": [str(change["field"]) for change in changes],
                "proposal_reason": proposal_reason,
            },
            result_code="OK", result_ok=True,
        )
        return conn.execute(
            "SELECT * FROM semantic_proposals WHERE proposal_id=?", (proposal_id,)
        ).fetchone()


def _record_authorization_in_transaction(
    conn: sqlite3.Connection,
    *,
    task_gid: str,
    operation_id: str,
    field_name: str,
    before: Any,
    after: Any,
    reason: str,
    proposal_id: str,
) -> str:
    before_json, after_json = _json(before), _json(after)
    rows = conn.execute(
        """SELECT * FROM marco_authorizations
             WHERE task_gid=? AND operation_id=? AND field_name=?
               AND before_json=? AND after_json=? AND consumed_at IS NULL
             ORDER BY created_at,authorization_id""",
        (task_gid, operation_id, field_name, before_json, after_json),
    ).fetchall()
    if len(rows) > 1:
        raise DishRuleError(
            "CONFLICT", "multiple equivalent unused authorizations require operator review",
            rule="governed_authorization_history_ambiguous",
            details={"field": field_name, "authorization_count": len(rows)},
        )
    if rows:
        return str(rows[0]["authorization_id"])
    authorization_id = str(uuid.uuid4())
    now = utc_now()
    conn.execute(
        """INSERT INTO marco_authorizations(
               authorization_id,task_gid,operation_id,field_name,before_json,after_json,
               reason,actor_run_id,created_at
           ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (
            authorization_id, task_gid, operation_id, field_name, before_json,
            after_json, reason, None, now,
        ),
    )
    record_audit(
        conn, submission_id=None, task_gid=task_gid, operation_id=operation_id,
        event_type="marco.authorization", actor_agent=None,
        details={
            "authorization_id": authorization_id,
            "field": field_name,
            "reason": reason,
            "proposal_id": proposal_id,
        },
        result_code="OK", result_ok=True, governed_kind="decision",
        before_state={field_name: before}, after_state={field_name: after},
        actor_source="marco-admin",
    )
    return authorization_id


def approve_semantic_proposal(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    live_identity: str,
    reason: str,
    approved_by: str = "Marco",
) -> sqlite3.Row:
    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise DishRuleError(
            "INVALID_ARGUMENT", "approval reason is required", rule="semantic_proposal_reason_required"
        )
    now = utc_now()
    with immediate_transaction(conn, "approve_semantic_proposal"):
        row = get_semantic_proposal(conn, proposal_id)
        if row["status"] == "approved":
            return row
        if row["status"] != "pending":
            raise DishRuleError(
                "WRONG_STATE", "only a pending proposal can be approved",
                rule="semantic_proposal_not_pending", details={"status": row["status"]},
            )
        if row["baseline_identity"] != live_identity:
            conn.execute(
                "UPDATE semantic_proposals SET status='stale',reviewed_at=?,review_reason=? WHERE proposal_id=?",
                (now, "live content changed before approval", proposal_id),
            )
            raise DishRuleError(
                "CONFLICT", "proposal baseline changed before approval",
                rule="semantic_proposal_stale",
                details={"expected_identity": row["baseline_identity"], "actual_identity": live_identity},
            )
        operation = conn.execute(
            "SELECT status FROM operations WHERE operation_id=?", (row["operation_id"],)
        ).fetchone()
        if operation is None or operation["status"] != "open":
            raise DishRuleError(
                "WRONG_STATE", "proposal operation is no longer open",
                rule="semantic_proposal_operation_closed",
            )
        authorization_ids = []
        for change in proposal_changes(conn, proposal_id):
            authorization_ids.append(
                _record_authorization_in_transaction(
                    conn, task_gid=row["task_gid"], operation_id=row["operation_id"],
                    field_name=change["field"], before=change["before"], after=change["after"],
                    reason=clean_reason, proposal_id=proposal_id,
                )
            )
        cursor = conn.execute(
            """UPDATE semantic_proposals
                  SET status='approved',reviewed_at=?,review_reason=?,approved_by=?
                WHERE proposal_id=? AND status='pending'""",
            (now, clean_reason, approved_by, proposal_id),
        )
        if cursor.rowcount != 1:
            raise DishRuleError(
                "CONFLICT", "proposal was reviewed concurrently",
                rule="semantic_proposal_review_race",
            )
        conn.execute(
            """UPDATE service_leases
                  SET released_at=COALESCE(released_at,?),
                      release_reason=COALESCE(release_reason,'semantic_proposal_approved')
                WHERE operation_id=? AND released_at IS NULL""",
            (now, row["operation_id"]),
        )
        record_audit(
            conn, submission_id=None, task_gid=row["task_gid"], operation_id=row["operation_id"],
            event_type="semantic_proposal.approved", actor_agent=None,
            details={
                "proposal_id": proposal_id,
                "reason": clean_reason,
                "authorization_ids": authorization_ids,
                "change_fields": [item["field"] for item in proposal_changes(conn, proposal_id)],
            },
            result_code="OK", result_ok=True, governed_kind="decision",
            before_state={"proposal_status": "pending"},
            after_state={"proposal_status": "approved", "authorization_ids": authorization_ids},
            actor_source="marco-admin",
        )
        return get_semantic_proposal(conn, proposal_id)


def reject_semantic_proposal(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    reason: str,
    live_identity: str,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    """Reject one proposal and atomically restart Verification on the unchanged baseline."""
    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise DishRuleError(
            "INVALID_ARGUMENT", "rejection reason is required", rule="semantic_proposal_reason_required"
        )
    now = utc_now()
    with immediate_transaction(conn, "reject_semantic_proposal"):
        row = get_semantic_proposal(conn, proposal_id)
        if row["status"] == "rejected":
            cycle = conn.execute(
                """SELECT * FROM verification_cycles
                     WHERE operation_id=? AND completed_at IS NULL
                     ORDER BY cycle_number DESC LIMIT 1""",
                (row["operation_id"],),
            ).fetchone()
            if cycle is None:
                raise DishRuleError(
                    "CONFLICT",
                    "rejected proposal has no fresh Verification continuation",
                    rule="semantic_proposal_rejection_continuation_missing",
                )
            return row, cycle
        if row["status"] != "pending":
            raise DishRuleError(
                "WRONG_STATE", "only a pending proposal can be rejected",
                rule="semantic_proposal_not_pending", details={"status": row["status"]},
            )
        if row["baseline_identity"] != live_identity:
            conn.execute(
                "UPDATE semantic_proposals SET status='stale',reviewed_at=?,review_reason=? WHERE proposal_id=?",
                (now, "live content changed before rejection", proposal_id),
            )
            raise DishRuleError(
                "CONFLICT",
                "proposal baseline changed before rejection",
                rule="semantic_proposal_stale",
                details={"expected_identity": row["baseline_identity"], "actual_identity": live_identity},
            )
        operation = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (row["operation_id"],)
        ).fetchone()
        cycle = conn.execute(
            """SELECT * FROM verification_cycles
                 WHERE cycle_id=? AND operation_id=? AND completed_at IS NULL""",
            (row["cycle_id"], row["operation_id"]),
        ).fetchone()
        if operation is None or operation["status"] != "open" or cycle is None:
            raise DishRuleError(
                "WRONG_STATE",
                "proposal operation or Verification cycle is no longer open",
                rule="semantic_proposal_operation_closed",
            )
        conn.execute(
            """UPDATE semantic_proposals
                  SET status='rejected',reviewed_at=?,review_reason=?,approved_by='Marco'
                WHERE proposal_id=? AND status='pending'""",
            (now, clean_reason, proposal_id),
        )
        conn.execute(
            """UPDATE verification_cycles
                  SET correction_class='large', outcome='rejected', route=NULL,
                      resume_state=NULL, completed_at=?
                WHERE cycle_id=? AND completed_at IS NULL""",
            (now, row["cycle_id"]),
        )
        next_number = conn.execute(
            "SELECT COALESCE(MAX(cycle_number),0)+1 FROM verification_cycles WHERE task_gid=?",
            (row["task_gid"],),
        ).fetchone()[0]
        new_cycle = create_verification_cycle(
            conn,
            operation_id=row["operation_id"],
            task_gid=row["task_gid"],
            cycle_number=next_number,
            protocol_release=cycle["protocol_release"],
            protocol_text=cycle["protocol_text"],
            route=None,
        )
        transition_operation(conn, row["operation_id"], phase="await_verification")
        conn.execute(
            """UPDATE operations
                  SET verifier_agent=NULL, run_id=NULL, independence_attestation=NULL
                WHERE operation_id=?""",
            (row["operation_id"],),
        )
        record_audit(
            conn, submission_id=None, task_gid=row["task_gid"], operation_id=row["operation_id"],
            event_type="semantic_proposal.rejected", actor_agent=None,
            details={
                "proposal_id": proposal_id,
                "reason": clean_reason,
                "completed_cycle_id": row["cycle_id"],
                "new_cycle_id": new_cycle["cycle_id"],
                "baseline_identity": live_identity,
            },
            result_code="OK", result_ok=True, governed_kind="decision",
            before_state={"proposal_status": "pending", "cycle_id": row["cycle_id"]},
            after_state={
                "proposal_status": "rejected",
                "new_cycle_id": new_cycle["cycle_id"],
                "candidate_identity": live_identity,
            },
            actor_source="marco-admin",
        )
        return get_semantic_proposal(conn, proposal_id), new_cycle


def claim_semantic_proposal(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    agent: str,
    run_id: str,
    request_id: str | None,
) -> sqlite3.Row:
    now = utc_now()
    with immediate_transaction(conn, "claim_semantic_proposal"):
        row = get_semantic_proposal(conn, proposal_id)
        if row["status"] == "claimed":
            if row["claimed_run_id"] == run_id and row["claimed_agent"] == agent:
                return row
            raise DishRuleError(
                "CONFLICT", "approved proposal is already claimed by another run",
                rule="semantic_proposal_claimed",
                details={"claimed_run_id": row["claimed_run_id"]},
            )
        if row["status"] != "approved":
            raise DishRuleError(
                "WRONG_STATE", "proposal is not approved and claimable",
                rule="semantic_proposal_not_claimable", details={"status": row["status"]},
            )
        cursor = conn.execute(
            """UPDATE semantic_proposals
                  SET status='claimed',claimed_at=?,claimed_agent=?,claimed_run_id=?,claim_request_id=?
                WHERE proposal_id=? AND status='approved'""",
            (now, agent, run_id, request_id, proposal_id),
        )
        if cursor.rowcount != 1:
            raise DishRuleError(
                "CONFLICT", "proposal was claimed concurrently",
                rule="semantic_proposal_claim_race",
            )
        record_audit(
            conn, submission_id=None, task_gid=row["task_gid"], operation_id=row["operation_id"],
            event_type="semantic_proposal.claimed", actor_agent=agent, actor_run_id=run_id,
            details={"proposal_id": proposal_id, "request_id": request_id},
            result_code="OK", result_ok=True,
        )
        return get_semantic_proposal(conn, proposal_id)


def release_semantic_proposal_claim(
    conn: sqlite3.Connection, *, proposal_id: str, run_id: str, reason: str
) -> None:
    with immediate_transaction(conn, "release_semantic_proposal_claim"):
        row = get_semantic_proposal(conn, proposal_id)
        if row["status"] != "claimed" or row["claimed_run_id"] != run_id:
            return
        conn.execute(
            """UPDATE semantic_proposals
                  SET status='approved',claimed_at=NULL,claimed_agent=NULL,
                      claimed_run_id=NULL,claim_request_id=NULL
                WHERE proposal_id=? AND status='claimed' AND claimed_run_id=?""",
            (proposal_id, run_id),
        )
        record_audit(
            conn, submission_id=None, task_gid=row["task_gid"], operation_id=row["operation_id"],
            event_type="semantic_proposal.claim_released", actor_agent=row["claimed_agent"],
            actor_run_id=run_id, details={"proposal_id": proposal_id, "reason": reason},
            result_code="OK", result_ok=True,
        )


def mark_semantic_proposal_applied(
    conn: sqlite3.Connection,
    *,
    proposal_id: str,
    run_id: str,
    applied_identity: str,
) -> sqlite3.Row:
    now = utc_now()
    row = get_semantic_proposal(conn, proposal_id)
    cursor = conn.execute(
        """UPDATE semantic_proposals
              SET status='applied',applied_at=?,applied_identity=?
            WHERE proposal_id=? AND status='claimed' AND claimed_run_id=?""",
        (now, applied_identity, proposal_id, run_id),
    )
    if cursor.rowcount != 1:
        raise DishRuleError(
            "CONFLICT", "proposal application claim was lost",
            rule="semantic_proposal_claim_lost",
        )
    record_audit(
        conn, submission_id=None, task_gid=row["task_gid"], operation_id=row["operation_id"],
        event_type="semantic_proposal.applied", actor_agent=row["claimed_agent"],
        actor_run_id=run_id,
        details={"proposal_id": proposal_id, "applied_identity": applied_identity},
        result_code="OK", result_ok=True,
    )
    return get_semantic_proposal(conn, proposal_id)


def claimable_proposal_for_run(
    conn: sqlite3.Connection, *, proposal_id: str, operation_id: str, run_id: str
) -> bool:
    row = conn.execute(
        """SELECT status,operation_id,claimed_run_id FROM semantic_proposals
             WHERE proposal_id=?""",
        (proposal_id,),
    ).fetchone()
    if row is None or row["operation_id"] != operation_id:
        return False
    return row["status"] == "approved" or (
        row["status"] == "claimed" and row["claimed_run_id"] == run_id
    )
