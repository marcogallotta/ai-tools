"""Durable identity and outcome facts for request-scoped backup creation."""
from __future__ import annotations

import sqlite3

from dish_tool.errors import DishRuleError
from dish_tool.models import utc_now
from dish_tool.transactions import immediate_transaction, require_transaction

from .backup import BackupRecord


BACKUP_CREATION_OUTCOMES = frozenset({"confirmed", "not_applied", "uncertain"})


def creation_for_request(conn: sqlite3.Connection, request_id: str):
    return conn.execute(
        "SELECT * FROM backup_creations WHERE request_id=?", (request_id,)
    ).fetchone()


def unresolved_backup_creations(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Return exact reservations whose filesystem or request closure is unfinished."""
    return conn.execute(
        """SELECT creation.*
             FROM backup_creations AS creation
             JOIN service_requests AS request
               ON request.request_id=creation.request_id
            WHERE creation.status IN ('reserved','uncertain')
               OR request.status IN ('pending','uncertain')
               OR (creation.status='confirmed'
                   AND request.status='completed'
                   AND COALESCE(
                         json_extract(request.resolution_result_json, '$.ok'),
                         json_extract(request.result_json, '$.ok'),
                         0
                       ) <> 1)
            ORDER BY creation.created_at, creation.rowid"""
    ).fetchall()


def reserve_backup_creation(
    conn: sqlite3.Connection, *, request_id: str, backup_id: str
):
    """Commit the exact output identity before snapshot creation may begin."""
    with immediate_transaction(conn, "reserve_backup_creation"):
        row = creation_for_request(conn, request_id)
        if row is None:
            conn.execute(
                """INSERT INTO backup_creations(
                       request_id, backup_id, status, created_at
                   ) VALUES(?,?,'reserved',?)""",
                (request_id, backup_id, utc_now()),
            )
            row = creation_for_request(conn, request_id)
        return row


def finish_backup_creation(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    outcome: str,
    reason: str,
    record: BackupRecord | None = None,
) -> sqlite3.Row:
    """Durably classify one exact reserved destination."""
    if outcome not in BACKUP_CREATION_OUTCOMES:
        raise ValueError("unsupported backup creation outcome")
    require_transaction(conn, operation="backup creation outcome")
    row = creation_for_request(conn, request_id)
    if row is None:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "backup creation identity is missing",
            rule="backup_creation_identity_missing",
            retryable=False,
            details={"request_id": request_id},
        )
    if record is not None and row["backup_id"] != record.backup_id:
        raise DishRuleError(
            "CONFLICT",
            "backup creation identity does not match the durable request",
            rule="backup_creation_identity_conflict",
            retryable=False,
            details={
                "request_id": request_id,
                "expected_backup_id": row["backup_id"],
                "actual_backup_id": record.backup_id,
            },
        )
    if outcome == "confirmed" and record is None:
        raise ValueError("confirmed backup creation requires a record")
    if outcome == "not_applied" and record is not None:
        raise ValueError("not_applied backup creation cannot carry file metadata")
    if row["status"] in {"confirmed", "not_applied"}:
        if row["status"] != outcome:
            raise DishRuleError(
                "BACKEND_UNCERTAIN",
                "terminal backup creation evidence conflicts with the current destination",
                rule="backup_creation_terminal_conflict",
                retryable=False,
                details={"request_id": request_id, "backup_id": row["backup_id"]},
            )
        if outcome == "confirmed" and (
            row["sha256"] != record.sha256 or row["size_bytes"] != record.size_bytes
        ):
            raise DishRuleError(
                "VALIDATION_FAILED",
                "confirmed backup metadata no longer matches its file",
                rule="backup_creation_metadata_mismatch",
                retryable=False,
                details={"request_id": request_id, "backup_id": row["backup_id"]},
            )
        return row
    cursor = conn.execute(
        """UPDATE backup_creations
              SET status=?, sha256=?, size_bytes=?, completed_at=?, resolution_reason=?
            WHERE request_id=? AND status IN ('reserved','uncertain')""",
        (
            outcome,
            None if record is None else record.sha256,
            None if record is None else record.size_bytes,
            utc_now(),
            str(reason).strip() or outcome,
            request_id,
        ),
    )
    if cursor.rowcount != 1:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "backup creation outcome could not be made durable",
            rule="backup_creation_completion_missing",
            retryable=False,
            details={"request_id": request_id, "backup_id": row["backup_id"]},
        )
    return creation_for_request(conn, request_id)

