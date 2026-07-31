"""Durable request identity for response-loss-safe service mutations."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping, MutableMapping

from dish_tool.errors import DishRuleError
from dish_tool.models import utc_now
from dish_tool.transactions import immediate_transaction, join_or_begin_immediate


def request_hash(command: str, arguments: Mapping[str, Any]) -> str:
    payload = json.dumps(
        {"command": command, "arguments": dict(arguments)},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def begin_request(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    owner_id: str,
    run_id: str,
    command: str,
    arguments: Mapping[str, Any],
):
    digest = request_hash(command, arguments)
    with immediate_transaction(conn, "begin_service_request"):
        row = conn.execute(
            "SELECT * FROM service_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO service_requests(
                       request_id,owner_id,run_id,command,request_hash,status,created_at
                   ) VALUES(?,?,?,?,?,'pending',?)""",
                (request_id, owner_id, run_id, command, digest, utc_now()),
            )
            row = conn.execute(
                "SELECT * FROM service_requests WHERE request_id=?", (request_id,)
            ).fetchone()
            return row, True
        if (
            row["owner_id"] != owner_id
            or row["run_id"] != run_id
            or row["command"] != command
            or row["request_hash"] != digest
        ):
            raise DishRuleError(
                "CONFLICT",
                "request ID was already used for different work",
                rule="service_request_identity_conflict",
                details={"request_id": request_id},
            )
        return row, False


def stored_result(
    row, *, permit_uncertain_resume: bool = False
) -> dict[str, Any] | None:
    if row["status"] not in {"completed", "uncertain"}:
        return None
    if row["status"] == "uncertain" and permit_uncertain_resume:
        return None
    encoded = (
        row["resolution_result_json"]
        if row["status"] == "completed" and row["resolution_result_json"]
        else row["result_json"]
    )
    if encoded is None:
        return None
    result = json.loads(encoded)
    result.setdefault("data", {})["request_replayed"] = True
    result["data"]["request_id"] = row["request_id"]
    return result


def _resolved_execution_exists(conn: sqlite3.Connection, request_id: str) -> bool:
    """Return whether exact replay durably resolved this request's execution."""

    return conn.execute(
        """SELECT 1 FROM operation_executions
             WHERE request_id=? AND status='completed'
               AND resolution_evidence_json IS NOT NULL
               AND resolved_at IS NOT NULL
             LIMIT 1""",
        (request_id,),
    ).fetchone() is not None


def _authoritative_result(row: Mapping[str, Any]) -> dict[str, Any] | None:
    encoded = (
        row["resolution_result_json"]
        if row["status"] == "completed" and row["resolution_result_json"]
        else row["result_json"]
    )
    return None if encoded is None else json.loads(encoded)


def complete_request(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist and return the request's one authoritative durable result.

    Ordinary completion is first-writer-wins.  If an executor and a recovery
    caller race, every loser returns the stored envelope instead of a
    contradictory local result.  An uncertain result may advance to a resolved
    result only after the matching operation execution has itself been durably
    resolved by exact replay evidence.
    """

    status = "uncertain" if result.get("code") == "BACKEND_UNCERTAIN" else "completed"
    encoded = json.dumps(
        dict(result), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    with join_or_begin_immediate(conn, "complete_service_request"):
        row = conn.execute(
            "SELECT * FROM service_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise DishRuleError(
                "INTERNAL_ERROR",
                "request result could not be made durable",
                rule="service_request_completion_missing",
                retryable=False,
                details={"request_id": request_id},
            )

        wrote = False
        if row["status"] == "pending":
            cursor = conn.execute(
                """UPDATE service_requests
                      SET status=?,
                          operation_id=(
                              SELECT operation_id FROM operations WHERE operation_id=?
                          ),
                          task_gid=?, result_json=?, completed_at=?
                    WHERE request_id=? AND status='pending'""",
                (
                    status,
                    result.get("submission_id"),
                    result.get("task_gid"),
                    encoded,
                    utc_now(),
                    request_id,
                ),
            )
            wrote = cursor.rowcount == 1
        elif (
            row["status"] == "uncertain"
            and status == "completed"
            and row["resolved_at"] is None
            and _resolved_execution_exists(conn, request_id)
        ):
            cursor = conn.execute(
                """UPDATE service_requests
                      SET status='completed', resolution_result_json=?, resolved_at=?
                    WHERE request_id=? AND status='uncertain' AND resolved_at IS NULL""",
                (encoded, utc_now(), request_id),
            )
            wrote = cursor.rowcount == 1

        row = conn.execute(
            "SELECT * FROM service_requests WHERE request_id=?", (request_id,)
        ).fetchone()
        authoritative = None if row is None else _authoritative_result(row)
        if authoritative is None:
            raise DishRuleError(
                "INTERNAL_ERROR",
                "request result could not be made durable",
                rule="service_request_completion_missing",
                retryable=False,
                details={"request_id": request_id},
            )
        if not wrote:
            authoritative.setdefault("data", {})["request_replayed"] = True
            authoritative["data"]["request_id"] = request_id
            authoritative["data"]["request_completion_race_resolved"] = True

    if isinstance(result, MutableMapping):
        result.clear()
        result.update(authoritative)
    return authoritative


def pending_error(command: str, request_id: str, *, operation_id: str | None = None):
    return DishRuleError(
        "BACKEND_UNCERTAIN",
        "an earlier request with this ID may have applied; do not repeat the mutation",
        rule="service_request_pending",
        retryable=False,
        details={
            "request_id": request_id,
            "operation_id": operation_id,
            "required_admin_action": "inspect",
        },
    )
