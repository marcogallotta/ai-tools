"""Durable semantic proposals, atomic Marco approval, and claimable application state."""
from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Mapping, Sequence

from .database import (
    content_identity,
    create_verification_cycle,
    record_audit,
    transition_operation,
    utc_now,
)
from .governed_diff import (
    agent_attested_decision_appends, canonical_diff, governed_changes,
    governed_changes_requiring_authorization, validate_semantic_proposal,
)
from .task_document import (
    DocumentParseError,
    document_parse_error_payloads,
    finding_payload,
    parse_task_document,
    validate_task_document,
)
from .errors import DishRuleError
from .transactions import immediate_transaction, require_transaction

_ACTIVE = ("pending", "approved", "claimed")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _proposal_audit_actor(agent: str) -> tuple[str | None, str]:
    """Map the internal mechanical proposal actor onto the audit provenance model."""
    if agent == "dish":
        return None, "dish-mechanical"
    return agent, "command"


def _row_dict(row: sqlite3.Row | Mapping[str, Any]) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()} if hasattr(row, "keys") else dict(row)


def _json_value(value: Any) -> Any:
    """Return the stable JSON representation used by durable proposal evidence.

    Canonical task structures use tuples for some fields (notably Decisions), while
    SQLite proposal evidence round-trips through JSON arrays.  Comparing the Python
    container types directly made valid approved proposals fail at apply time.
    """

    return json.loads(_json(value))


def _normalized_change_records(changes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "ordinal": int(change.get("ordinal", ordinal)),
            "field": str(change["field"]),
            "before": _json_value(change["before"]),
            "after": _json_value(change["after"]),
        }
        for ordinal, change in enumerate(changes)
    ]


def _normalized_linked_changes(changes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "path": str(change["path"]),
            "before": str(change.get("before", "")),
            "after": str(change.get("after", "")),
        }
        for change in changes
    ]


def _render_document(document) -> tuple[str, str]:
    lines = document.render().splitlines()
    return lines[0], "\n".join(lines[1:]) + "\n"


def _record_mismatches(expected: Sequence[Mapping[str, Any]], actual: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    limit = max(len(expected), len(actual))
    for index in range(limit):
        expected_item = None if index >= len(expected) else dict(expected[index])
        actual_item = None if index >= len(actual) else dict(actual[index])
        if expected_item != actual_item:
            mismatches.append(
                {
                    "ordinal": index,
                    "stored": expected_item,
                    "derived": actual_item,
                }
            )
    return mismatches


def validate_semantic_proposal_integrity(
    conn: sqlite3.Connection,
    row: sqlite3.Row | Mapping[str, Any],
    *,
    baseline_title: str,
    baseline_notes: str,
) -> tuple[Any, Any]:
    """Prove stored proposal evidence matches the exact baseline and candidate.

    This check intentionally runs before Marco approval as well as before application,
    so an internally malformed proposal cannot become an approved dead end.
    """

    proposal = _row_dict(row)
    baseline_identity = content_identity(baseline_title, baseline_notes).digest
    if baseline_identity != proposal["baseline_identity"]:
        raise DishRuleError(
            "CONFLICT",
            "proposal baseline content does not match its stored baseline identity",
            rule="semantic_proposal_baseline_invalid",
            details={
                "proposal_id": proposal["proposal_id"],
                "expected_identity": proposal["baseline_identity"],
                "actual_identity": baseline_identity,
            },
        )
    candidate_identity = content_identity(
        str(proposal["candidate_title"]), str(proposal["candidate_notes"])
    ).digest
    if candidate_identity != proposal["candidate_identity"]:
        raise DishRuleError(
            "CONFLICT",
            "stored proposal identity does not match its exact candidate",
            rule="semantic_proposal_identity_invalid",
            details={
                "proposal_id": proposal["proposal_id"],
                "expected_identity": proposal["candidate_identity"],
                "actual_identity": candidate_identity,
            },
        )
    try:
        before_document = parse_task_document(f"{baseline_title}\n{baseline_notes}")
        candidate_document = parse_task_document(
            f"{proposal['candidate_title']}\n{proposal['candidate_notes']}"
        )
    except DocumentParseError as exc:
        raise DishRuleError(
            "VALIDATION_FAILED",
            "stored semantic proposal is not canonical",
            rule="semantic_proposal_candidate_invalid",
            errors=document_parse_error_payloads(exc),
            details={"proposal_id": proposal["proposal_id"]},
        ) from exc
    validate_semantic_proposal(before_document, candidate_document)
    rendered_title, rendered_notes = _render_document(candidate_document)
    rendered_identity = content_identity(rendered_title, rendered_notes).digest
    if rendered_identity != proposal["candidate_identity"]:
        raise DishRuleError(
            "CONFLICT",
            "stored proposal no longer renders to the approved identity",
            rule="semantic_proposal_render_drift",
            details={
                "proposal_id": proposal["proposal_id"],
                "expected_identity": proposal["candidate_identity"],
                "actual_identity": rendered_identity,
            },
        )
    actual_linked = _normalized_linked_changes(
        [
            {"path": path, "before": old, "after": new}
            for path, (old, new) in canonical_diff(before_document, candidate_document).items()
        ]
    )
    stored_linked = _normalized_linked_changes(json.loads(proposal["linked_changes_json"]))
    if actual_linked != stored_linked:
        raise DishRuleError(
            "CONFLICT",
            "stored proposal linked-change evidence does not match its exact candidate",
            rule="semantic_proposal_linked_changes_invalid",
            details={
                "proposal_id": proposal["proposal_id"],
                "mismatches": _record_mismatches(stored_linked, actual_linked),
            },
        )
    actual_governed = _normalized_change_records(
        [
            {"ordinal": index, "field": item.field, "before": item.before, "after": item.after}
            for index, item in enumerate(governed_changes(before_document, candidate_document))
        ]
    )
    stored_governed = _normalized_change_records(proposal_changes(conn, proposal["proposal_id"]))
    if actual_governed != stored_governed:
        raise DishRuleError(
            "CONFLICT",
            "stored proposal governed-change evidence does not match its exact candidate",
            rule="semantic_proposal_governed_changes_invalid",
            details={
                "proposal_id": proposal["proposal_id"],
                "mismatches": _record_mismatches(stored_governed, actual_governed),
            },
        )
    stored_attested_raw = json.loads(proposal.get("agent_attested_decisions_json") or "[]")
    if not isinstance(stored_attested_raw, list) or not all(
        isinstance(item, str) for item in stored_attested_raw
    ):
        raise DishRuleError(
            "CONFLICT",
            "stored proposal Decision-attestation evidence is malformed",
            rule="semantic_proposal_attestation_invalid",
            details={"proposal_id": proposal["proposal_id"]},
        )
    stored_attested = tuple(stored_attested_raw)
    actual_attested = agent_attested_decision_appends(before_document, candidate_document)
    if stored_attested and stored_attested != actual_attested:
        raise DishRuleError(
            "CONFLICT",
            "stored proposal Decision-attestation evidence does not match its exact candidate",
            rule="semantic_proposal_attestation_invalid",
            details={
                "proposal_id": proposal["proposal_id"],
                "stored": list(stored_attested),
                "actual": list(actual_attested),
            },
        )
    return before_document, candidate_document


def semantic_proposal_baseline_content(
    conn: sqlite3.Connection, row: sqlite3.Row | Mapping[str, Any]
) -> tuple[str, str]:
    proposal = _row_dict(row)
    baseline = conn.execute(
        """SELECT title,notes FROM content_versions
             WHERE task_gid=? AND identity=? AND confirmed=1
             ORDER BY created_at DESC,rowid DESC LIMIT 1""",
        (proposal["task_gid"], proposal["baseline_identity"]),
    ).fetchone()
    if baseline is None:
        raise DishRuleError(
            "CONFLICT",
            "semantic proposal baseline lacks durable confirmed content evidence",
            rule="semantic_proposal_baseline_evidence_missing",
            details={
                "proposal_id": proposal["proposal_id"],
                "task_gid": proposal["task_gid"],
                "baseline_identity": proposal["baseline_identity"],
            },
        )
    return str(baseline["title"]), str(baseline["notes"])


def semantic_proposal_drift_details(
    conn: sqlite3.Connection,
    row: sqlite3.Row | Mapping[str, Any],
    *,
    live_title: str,
    live_notes: str,
) -> dict[str, Any]:
    """Explain exact canonical content drift without blaming unrelated task metadata."""

    proposal = _row_dict(row)
    details: dict[str, Any] = {
        "proposal_id": proposal["proposal_id"],
        "expected_identity": proposal["baseline_identity"],
        "actual_identity": content_identity(live_title, live_notes).digest,
        "candidate_identity": proposal["candidate_identity"],
        "metadata_note": (
            "Content identity covers task title and notes only; due date, section placement, "
            "assignee, completion, and comments do not invalidate a semantic proposal."
        ),
    }
    try:
        baseline_title, baseline_notes = semantic_proposal_baseline_content(conn, proposal)
    except DishRuleError:
        details["baseline_content_available"] = False
        return details
    details["baseline_content_available"] = True
    try:
        baseline_document = parse_task_document(f"{baseline_title}\n{baseline_notes}")
        live_document = parse_task_document(f"{live_title}\n{live_notes}")
    except DocumentParseError:
        details["content_changes"] = [
            {
                "path": "raw_title_or_notes",
                "before_identity": proposal["baseline_identity"],
                "after_identity": details["actual_identity"],
            }
        ]
        return details
    details["content_changes"] = [
        {"path": path, "before": old, "after": new}
        for path, (old, new) in canonical_diff(baseline_document, live_document).items()
    ]
    return details


def semantic_proposal_action_facts(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    live_title: str,
    live_notes: str,
    current_cycle_id: str | None,
    expected_schema_version: str,
    schema=None,
) -> dict[str, Any] | None:
    """Return read-only facts used by the workflow legal-action authority.

    The mutation path still repeats these checks after it owns the execution claim;
    this projection exists so callers do not advertise ``apply-proposal`` from a
    looser status-only test than the command itself enforces.
    """

    row = active_proposal_for_operation(conn, operation_id)
    if row is None:
        return None
    facts: dict[str, Any] = {
        "proposal_id": row["proposal_id"],
        "status": row["status"],
        "candidate_identity": row["candidate_identity"],
        "baseline_identity": row["baseline_identity"],
        "cycle_id": row["cycle_id"],
        "claimed_agent": row["claimed_agent"],
        "claimed_run_id": row["claimed_run_id"],
        "actionable": False,
    }
    if row["status"] not in {"approved", "claimed"}:
        return facts

    if current_cycle_id is None:
        facts["block"] = {
            "code": "WRONG_STATE",
            "message": "operation has no pending Verification cycle",
            "rule": "verification_cycle_missing",
            "details": {},
        }
        return facts
    if str(current_cycle_id) != str(row["cycle_id"]):
        facts["block"] = {
            "code": "CONFLICT",
            "message": "the proposal's Verification cycle is no longer current",
            "rule": "semantic_proposal_cycle_stale",
            "details": {
                "proposal_cycle_id": row["cycle_id"],
                "current_cycle_id": current_cycle_id,
            },
        }
        return facts

    live_identity = content_identity(live_title, live_notes).digest
    live_matches_baseline = live_identity == row["baseline_identity"]
    candidate_already_live = live_identity == row["candidate_identity"]
    facts["candidate_already_live"] = candidate_already_live
    if not live_matches_baseline and not candidate_already_live:
        facts["block"] = {
            "code": "CONFLICT",
            "message": "the live task content changed after the proposal was created",
            "rule": "semantic_proposal_stale",
            "details": {
                **semantic_proposal_drift_details(
                    conn, row, live_title=live_title, live_notes=live_notes
                ),
                "live_matches_candidate": False,
                "required_action": (
                    "Inspect the exact title/notes diff before deciding whether fresh review is "
                    "required. Due date, section, assignee, completion, and comments are not "
                    "part of semantic proposal content identity."
                ),
            },
        }
        return facts

    try:
        if live_matches_baseline:
            baseline_title, baseline_notes = live_title, live_notes
        else:
            baseline_title, baseline_notes = semantic_proposal_baseline_content(conn, row)
        _, candidate_document = validate_semantic_proposal_integrity(
            conn,
            row,
            baseline_title=baseline_title,
            baseline_notes=baseline_notes,
        )
        check = validate_task_document(
            candidate_document,
            expected_schema_version=expected_schema_version,
            schema=schema,
        )
        if not check.ok:
            raise DishRuleError(
                "VALIDATION_FAILED",
                (
                    "approved candidate already live but failed deterministic validation"
                    if candidate_already_live
                    else "candidate failed deterministic validation"
                ),
                errors=[finding_payload(finding) for finding in check.findings],
            )
    except DishRuleError as exc:
        facts["block"] = {
            "code": exc.code,
            "message": str(exc),
            "rule": exc.rule,
            "details": dict(exc.details),
            "errors": [dict(item) for item in exc.errors],
            "retryable": exc.retryable,
        }
        return facts

    facts["actionable"] = True
    return facts


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
    item["agent_attested_decisions"] = json.loads(
        item.pop("agent_attested_decisions_json", "[]")
    )
    item["changes"] = list(proposal_changes(conn, item["proposal_id"]))
    changed_fields = [str(change.get("field") or "").strip() for change in item["changes"]]
    changed_fields = [field for field in changed_fields if field]
    item["review_summary"] = {
        "outcome": "needs Marco review" if item.get("status") == "pending" else str(item.get("status") or "proposal"),
        "issue": str(item.get("proposal_reason") or "").strip(),
        "governed_changes": changed_fields,
        "simplest_next_step": (
            "Approve or reject this exact stored change bundle."
            if item.get("status") == "pending"
            else None
        ),
    }
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
    agent_attested_decisions: Sequence[str] = (),
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
                stored_attested = tuple(json.loads(existing["agent_attested_decisions_json"]))
                supplied_attested = tuple(str(item) for item in agent_attested_decisions)
                if stored_attested == supplied_attested:
                    return existing
                raise DishRuleError(
                    "CONFLICT",
                    "active semantic proposal has different Decision-attestation provenance",
                    rule="semantic_proposal_attestation_mismatch",
                    details={
                        "proposal_id": existing["proposal_id"],
                        "stored": list(stored_attested),
                        "supplied": list(supplied_attested),
                    },
                )
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
        proposed_changes = _normalized_change_records(
            [
                {
                    "ordinal": ordinal,
                    "field": str(change["field"]),
                    "before": change["before"],
                    "after": change["after"],
                }
                for ordinal, change in enumerate(changes)
            ]
        )
        proposed_linked = _normalized_linked_changes(linked_changes)
        baseline = conn.execute(
            """SELECT title,notes FROM content_versions
                 WHERE task_gid=? AND identity=? AND confirmed=1
                 ORDER BY created_at DESC,rowid DESC LIMIT 1""",
            (task_gid, baseline_identity),
        ).fetchone()
        if baseline is None:
            raise DishRuleError(
                "CONFLICT",
                "semantic proposal baseline lacks durable confirmed content evidence",
                rule="semantic_proposal_baseline_evidence_missing",
                details={
                    "task_gid": task_gid,
                    "operation_id": operation_id,
                    "baseline_identity": baseline_identity,
                },
            )
        try:
            baseline_document = parse_task_document(f"{baseline['title']}\n{baseline['notes']}")
            candidate_document = parse_task_document(f"{candidate_title}\n{candidate_notes}")
        except DocumentParseError as exc:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "semantic proposal candidate or baseline is not canonical",
                rule="semantic_proposal_candidate_invalid",
                errors=document_parse_error_payloads(exc),
            ) from exc
        validate_semantic_proposal(baseline_document, candidate_document)
        actual_attested = agent_attested_decision_appends(
            baseline_document, candidate_document
        )
        supplied_attested = tuple(str(item) for item in agent_attested_decisions)
        if supplied_attested and supplied_attested != actual_attested:
            raise DishRuleError(
                "CONFLICT",
                "agent-attested Decision evidence does not match the exact proposal candidate",
                rule="decision_attestation_mismatch",
                details={
                    "supplied": list(supplied_attested),
                    "actual": list(actual_attested),
                },
            )
        rendered_title, rendered_notes = _render_document(candidate_document)
        rendered_identity = content_identity(rendered_title, rendered_notes).digest
        if rendered_identity != candidate_identity:
            raise DishRuleError(
                "CONFLICT",
                "semantic proposal candidate identity does not match its exact rendered content",
                rule="semantic_proposal_identity_invalid",
                details={
                    "expected_identity": candidate_identity,
                    "actual_identity": rendered_identity,
                },
            )
        derived_linked = _normalized_linked_changes(
            [
                {"path": path, "before": old, "after": new}
                for path, (old, new) in canonical_diff(
                    baseline_document, candidate_document
                ).items()
            ]
        )
        derived_governed = _normalized_change_records(
            [
                {
                    "ordinal": index,
                    "field": item.field,
                    "before": item.before,
                    "after": item.after,
                }
                for index, item in enumerate(
                    governed_changes(baseline_document, candidate_document)
                )
            ]
        )
        if derived_linked != proposed_linked or derived_governed != proposed_changes:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "semantic proposal evidence does not match its exact baseline and candidate",
                rule="semantic_proposal_evidence_invalid",
                details={
                    "linked_change_mismatches": _record_mismatches(
                        proposed_linked, derived_linked
                    ),
                    "governed_change_mismatches": _record_mismatches(
                        proposed_changes, derived_governed
                    ),
                },
            )
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
                   explanation_json,linked_changes_json,agent_attested_decisions_json,
                   protocol_release,protocol_text,correction_class,proposer_agent,proposer_run_id,
                   status,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                proposal_id, task_gid, operation_id, cycle_id, baseline_identity,
                candidate_identity, candidate_title, candidate_notes, str(proposal_reason).strip(),
                _json(dict(explanation)), _json(proposed_linked), _json(supplied_attested),
                protocol_release, protocol_text, "large", proposer_agent, proposer_run_id,
                "pending", now,
            ),
        )
        for change in proposed_changes:
            conn.execute(
                """INSERT INTO semantic_proposal_changes(
                       proposal_id,ordinal,field_name,before_json,after_json
                   ) VALUES (?,?,?,?,?)""",
                (
                    proposal_id, change["ordinal"], change["field"],
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
                "agent_attested_decisions": list(supplied_attested),
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
    live_title: str,
    live_notes: str,
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
        live_identity = content_identity(live_title, live_notes).digest
        if row["baseline_identity"] != live_identity:
            conn.execute(
                "UPDATE semantic_proposals SET status='stale',reviewed_at=?,review_reason=? WHERE proposal_id=?",
                (now, "live content changed before approval", proposal_id),
            )
            raise DishRuleError(
                "CONFLICT", "proposal baseline changed before approval",
                rule="semantic_proposal_stale",
                details=semantic_proposal_drift_details(
                    conn, row, live_title=live_title, live_notes=live_notes
                ),
            )
        before_document, candidate_document = validate_semantic_proposal_integrity(
            conn, row, baseline_title=live_title, baseline_notes=live_notes
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
        stored_attested = tuple(json.loads(row["agent_attested_decisions_json"]))
        authorization_changes = governed_changes_requiring_authorization(
            before_document,
            candidate_document,
            agent_attested_decisions=stored_attested,
        )
        for change in authorization_changes:
            authorization_ids.append(
                _record_authorization_in_transaction(
                    conn, task_gid=row["task_gid"], operation_id=row["operation_id"],
                    field_name=change.field, before=change.before, after=change.after,
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
                "authorization_fields": [change.field for change in authorization_changes],
                "agent_attested_decisions": list(stored_attested),
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
    owner_id: str | None = None,
) -> sqlite3.Row:
    now = utc_now()
    with immediate_transaction(conn, "claim_semantic_proposal"):
        row = get_semantic_proposal(conn, proposal_id)
        clean_owner = str(owner_id or "").strip() or None
        if row["status"] == "claimed":
            if (
                row["claimed_run_id"] == run_id
                and row["claimed_agent"] == agent
                and (clean_owner is None or row["claimed_owner_id"] in {None, clean_owner})
            ):
                if clean_owner is not None and row["claimed_owner_id"] is None:
                    cursor = conn.execute(
                        """UPDATE semantic_proposals
                              SET claimed_owner_id=?
                            WHERE proposal_id=? AND status='claimed'
                              AND claimed_run_id=? AND claimed_owner_id IS NULL""",
                        (clean_owner, proposal_id, run_id),
                    )
                    if cursor.rowcount != 1:
                        raise DishRuleError(
                            "CONFLICT",
                            "proposal claim owner changed during idempotent recovery",
                            rule="semantic_proposal_claim_race",
                        )
                    row = get_semantic_proposal(conn, proposal_id)
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
                  SET status='claimed',claimed_at=?,claimed_agent=?,claimed_owner_id=?,
                      claimed_run_id=?,claim_request_id=?
                WHERE proposal_id=? AND status='approved'""",
            (now, agent, clean_owner, run_id, request_id, proposal_id),
        )
        if cursor.rowcount != 1:
            raise DishRuleError(
                "CONFLICT", "proposal was claimed concurrently",
                rule="semantic_proposal_claim_race",
            )
        audit_agent, audit_source = _proposal_audit_actor(agent)
        record_audit(
            conn, submission_id=None, task_gid=row["task_gid"], operation_id=row["operation_id"],
            event_type="semantic_proposal.claimed", actor_agent=audit_agent, actor_run_id=run_id,
            actor_source=audit_source,
            details={
                "proposal_id": proposal_id, "request_id": request_id,
                "application_actor": agent, "owner_id": clean_owner,
            },
            result_code="OK", result_ok=True,
        )
        return get_semantic_proposal(conn, proposal_id)


def release_semantic_proposal_claim_in_transaction(
    conn: sqlite3.Connection, *, proposal_id: str, run_id: str, reason: str
) -> None:
    require_transaction(conn, operation="release semantic proposal claim")
    row = get_semantic_proposal(conn, proposal_id)
    if row["status"] != "claimed" or row["claimed_run_id"] != run_id:
        return
    conn.execute(
        """UPDATE semantic_proposals
              SET status='approved',claimed_at=NULL,claimed_agent=NULL,claimed_owner_id=NULL,
                  claimed_run_id=NULL,claim_request_id=NULL
            WHERE proposal_id=? AND status='claimed' AND claimed_run_id=?""",
        (proposal_id, run_id),
    )
    audit_agent, audit_source = _proposal_audit_actor(str(row["claimed_agent"]))
    record_audit(
        conn, submission_id=None, task_gid=row["task_gid"], operation_id=row["operation_id"],
        event_type="semantic_proposal.claim_released", actor_agent=audit_agent,
        actor_run_id=run_id, actor_source=audit_source,
        details={
            "proposal_id": proposal_id, "reason": reason,
            "application_actor": row["claimed_agent"],
        },
        result_code="OK", result_ok=True,
    )


def release_semantic_proposal_claim(
    conn: sqlite3.Connection, *, proposal_id: str, run_id: str, reason: str
) -> None:
    with immediate_transaction(conn, "release_semantic_proposal_claim"):
        release_semantic_proposal_claim_in_transaction(
            conn, proposal_id=proposal_id, run_id=run_id, reason=reason
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
    audit_agent, audit_source = _proposal_audit_actor(str(row["claimed_agent"]))
    record_audit(
        conn, submission_id=None, task_gid=row["task_gid"], operation_id=row["operation_id"],
        event_type="semantic_proposal.applied", actor_agent=audit_agent,
        actor_run_id=run_id, actor_source=audit_source,
        details={
            "proposal_id": proposal_id, "applied_identity": applied_identity,
            "application_actor": row["claimed_agent"],
        },
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
