"""Durable two-call confirmation for Planning starts.

The first service request issues a challenge and completes without entering the
workflow application.  A later request from the same authenticated owner/run
must claim that exact challenge with an explicit intent basis before Planning
may start.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping
from typing import Any

from dish_tool.errors import DishRuleError
from dish_tool.models import agent_family, utc_now
from dish_tool.results import result_envelope
from dish_tool.transactions import require_transaction

from .identifiers import require_dish_uuid
from .leases import ServicePrincipal

_INTENT_FIELDS = frozenset(
    {"intent_challenge_id", "intent_basis", "override_reason"}
)
_INTENT_BASES = frozenset({"user_requested", "agent_override"})


def _target_arguments(arguments: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in arguments.items()
        if key not in _INTENT_FIELDS
    }


def planning_intent_target_hash(arguments: Mapping[str, Any]) -> str:
    payload = json.dumps(
        _target_arguments(arguments),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _confirmation_result(
    *,
    challenge_id: str,
    request_id: str,
    task_gid: str,
) -> dict[str, Any]:
    return result_envelope(
        command="start",
        ok=False,
        code="CONFIRMATION_REQUIRED",
        task_gid=task_gid,
        retryable=True,
        allowed_actions=["start"],
        data={
            "request_id": request_id,
            "intent_challenge_id": challenge_id,
            "required_start_kind": "planning",
            "required_intent_basis": ["user_requested", "agent_override"],
            "legal_next_step": (
                "Repeat start with kind=planning using a fresh client.request_id, "
                "this exact data.intent_challenge_id, and intent_basis=user_requested; "
                "or use intent_basis=agent_override with a non-blank override_reason."
            ),
            "planning_intent_confirmation": {
                "challenge_id": challenge_id,
                "status": "issued",
                "single_use": True,
                "task_gid": task_gid,
            },
            "message": "Planning start requires explicit user-intent confirmation",
        },
        errors=[
            {
                "rule": "planning_intent_confirmation_required",
                "field": "intent_challenge_id",
            }
        ],
    )


def _validate_followup_intent(arguments: Mapping[str, Any]) -> tuple[str, str | None]:
    basis = arguments.get("intent_basis")
    if not isinstance(basis, str) or basis not in _INTENT_BASES:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "Planning confirmation requires a valid intent basis",
            rule="planning_intent_basis_required",
            retryable=False,
            details={
                "field": "intent_basis",
                "allowed": sorted(_INTENT_BASES),
            },
        )
    reason = arguments.get("override_reason")
    if basis == "agent_override":
        if not isinstance(reason, str) or not reason.strip():
            raise DishRuleError(
                "INVALID_ARGUMENT",
                "agent override requires a non-blank reason",
                rule="planning_intent_override_reason_required",
                retryable=False,
                details={"field": "override_reason"},
            )
        return basis, reason.strip()
    if reason is not None:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "override_reason is valid only for agent_override",
            rule="planning_intent_override_reason_unexpected",
            retryable=False,
            details={"field": "override_reason"},
        )
    return basis, None


def issue_or_claim_planning_intent(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    principal: ServicePrincipal,
    arguments: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Issue the first-call challenge or claim one for a fresh follow-up.

    The durable service-request row already exists. The caller-owned transaction
    groups first-call challenge issuance with its completed confirmation result,
    or serializes the follow-up claim before workflow execution begins.
    """

    require_transaction(conn, operation="Planning intent gate")
    task_gid = str(arguments.get("task_gid") or "").strip()
    if not task_gid:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "task_gid is required",
            rule="task_gid_required",
            retryable=False,
            details={"field": "task_gid"},
        )
    agent = str(arguments.get("agent") or "").strip()
    if not agent:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "agent is required",
            rule="agent_required",
            retryable=False,
            details={"field": "agent"},
        )
    agent_family(agent)
    challenge_value = arguments.get("intent_challenge_id")
    if challenge_value is None:
        existing = conn.execute(
            """SELECT challenge_id FROM planning_intent_challenges
                 WHERE created_request_id=?""",
            (request_id,),
        ).fetchone()
        if existing is not None:
            return _confirmation_result(
                challenge_id=existing["challenge_id"],
                request_id=request_id,
                task_gid=task_gid,
            )
        challenge_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO planning_intent_challenges(
                   challenge_id,created_request_id,owner_id,run_id,task_gid,agent,
                   target_hash,status,created_at
               ) VALUES(?,?,?,?,?,?,?,'issued',?)""",
            (
                challenge_id,
                request_id,
                principal.owner_id,
                principal.run_id,
                task_gid,
                agent,
                planning_intent_target_hash(arguments),
                utc_now(),
            ),
        )
        return _confirmation_result(
            challenge_id=challenge_id,
            request_id=request_id,
            task_gid=task_gid,
        )

    challenge_id = require_dish_uuid(
        challenge_value, field="intent_challenge_id"
    )
    basis, override_reason = _validate_followup_intent(arguments)
    row = conn.execute(
        "SELECT * FROM planning_intent_challenges WHERE challenge_id=?",
        (challenge_id,),
    ).fetchone()
    if row is None:
        raise DishRuleError(
            "NOT_FOUND",
            "Planning intent challenge was not found",
            rule="planning_intent_challenge_not_found",
            retryable=False,
            details={"intent_challenge_id": challenge_id},
        )
    if (
        row["owner_id"] != principal.owner_id
        or row["run_id"] != principal.run_id
        or row["task_gid"] != task_gid
        or row["agent"] != agent
        or row["target_hash"] != planning_intent_target_hash(arguments)
    ):
        raise DishRuleError(
            "CONFLICT",
            "Planning intent challenge does not match this exact start request",
            rule="planning_intent_challenge_mismatch",
            retryable=False,
            details={"intent_challenge_id": challenge_id},
        )
    if row["created_request_id"] == request_id:
        raise DishRuleError(
            "CONFLICT",
            "Planning confirmation must use a fresh request ID",
            rule="planning_intent_fresh_request_required",
            retryable=False,
            details={"intent_challenge_id": challenge_id},
        )
    if row["status"] == "issued":
        cursor = conn.execute(
            """UPDATE planning_intent_challenges
                  SET status='claimed', claimed_request_id=?, intent_basis=?,
                      override_reason=?, claimed_at=?
                WHERE challenge_id=? AND status='issued'""",
            (request_id, basis, override_reason, utc_now(), challenge_id),
        )
        if cursor.rowcount != 1:
            raise DishRuleError(
                "CONFLICT",
                "Planning intent challenge was claimed concurrently",
                rule="planning_intent_challenge_already_used",
                retryable=False,
                details={"intent_challenge_id": challenge_id},
            )
        return None
    if row["claimed_request_id"] == request_id and row["status"] in {
        "claimed",
        "consumed",
    }:
        if row["intent_basis"] != basis or row["override_reason"] != override_reason:
            raise DishRuleError(
                "CONFLICT",
                "Planning intent claim does not match its original confirmation",
                rule="planning_intent_claim_conflict",
                retryable=False,
                details={"intent_challenge_id": challenge_id},
            )
        return None
    raise DishRuleError(
        "CONFLICT",
        "Planning intent challenge has already been used",
        rule="planning_intent_challenge_already_used",
        retryable=False,
        details={"intent_challenge_id": challenge_id},
    )


def planning_start_may_resume(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    arguments: Mapping[str, Any],
) -> bool:
    """Return whether exact replay may safely resume before local start effects."""

    challenge_id = arguments.get("intent_challenge_id")
    if not isinstance(challenge_id, str):
        return False
    challenge = conn.execute(
        """SELECT status,claimed_request_id FROM planning_intent_challenges
             WHERE challenge_id=?""",
        (challenge_id,),
    ).fetchone()
    if (
        challenge is None
        or challenge["claimed_request_id"] != request_id
        or challenge["status"] not in {"claimed", "consumed"}
    ):
        return False

    prepared_operation_id = str(
        arguments.get("prepared_operation_id") or ""
    ).strip()
    if prepared_operation_id:
        operation = conn.execute(
            """SELECT operation_kind,status,phase,run_id,successor_claim_mode
                 FROM operations WHERE operation_id=?""",
            (prepared_operation_id,),
        ).fetchone()
        return bool(
            operation is not None
            and operation["operation_kind"] == "planning"
            and operation["status"] == "open"
            and operation["phase"] == "prepare_required"
            and operation["run_id"] is None
            and operation["successor_claim_mode"] == "stage_actor"
        )

    task_gid = str(arguments.get("task_gid") or "").strip()
    operation = conn.execute(
        """SELECT 1 FROM operations
             WHERE task_gid=? AND status IN ('open','uncertain') LIMIT 1""",
        (task_gid,),
    ).fetchone()
    return operation is None


def consume_planning_intent(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    operation_id: str,
) -> None:
    """Bind one claimed challenge to the operation created by its request."""

    require_transaction(conn, operation="Planning intent consumption")
    row = conn.execute(
        """SELECT * FROM planning_intent_challenges
             WHERE claimed_request_id=?""",
        (request_id,),
    ).fetchone()
    if row is None:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "Planning start is missing its durable intent challenge",
            rule="planning_intent_challenge_missing",
            retryable=False,
            details={"request_id": request_id},
        )
    if row["status"] == "consumed":
        if row["operation_id"] != operation_id:
            raise DishRuleError(
                "CONFLICT",
                "Planning intent challenge is bound to another operation",
                rule="planning_intent_operation_conflict",
                retryable=False,
                details={"intent_challenge_id": row["challenge_id"]},
            )
        return
    cursor = conn.execute(
        """UPDATE planning_intent_challenges
              SET status='consumed', operation_id=?, consumed_at=?
            WHERE challenge_id=? AND status='claimed'
              AND claimed_request_id=?""",
        (operation_id, utc_now(), row["challenge_id"], request_id),
    )
    if cursor.rowcount != 1:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "Planning intent challenge could not be consumed",
            rule="planning_intent_consumption_failed",
            retryable=False,
            details={"intent_challenge_id": row["challenge_id"]},
        )
