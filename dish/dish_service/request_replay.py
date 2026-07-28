"""Durable request identity for response-loss-safe service mutations."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from dish_tool.errors import DishRuleError
from dish_tool.models import utc_now


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
    conn.execute("BEGIN IMMEDIATE")
    try:
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
            conn.execute("COMMIT")
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
        conn.execute("COMMIT")
        return row, False
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def stored_result(row) -> dict[str, Any] | None:
    if row["status"] not in {"completed", "uncertain"} or row["result_json"] is None:
        return None
    result = json.loads(row["result_json"])
    result.setdefault("data", {})["request_replayed"] = True
    result["data"]["request_id"] = row["request_id"]
    return result


def complete_request(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    result: Mapping[str, Any],
) -> None:
    status = "uncertain" if result.get("code") == "BACKEND_UNCERTAIN" else "completed"
    encoded = json.dumps(dict(result), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    conn.execute(
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
