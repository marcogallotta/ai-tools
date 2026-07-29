"""Durable identity and completion facts for request-scoped backup creation."""

from __future__ import annotations

import sqlite3
from typing import Any

from dish_tool.errors import DishRuleError
from dish_tool.models import utc_now

from .backup import BackupRecord


def creation_for_request(conn: sqlite3.Connection, request_id: str):
    return conn.execute(
        "SELECT * FROM backup_creations WHERE request_id=?", (request_id,)
    ).fetchone()


def reserve_backup_creation(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    backup_id: str,
):
    """Commit the exact output identity before snapshot creation may begin."""

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = creation_for_request(conn, request_id)
        if row is None:
            conn.execute(
                """INSERT INTO backup_creations(
                       request_id, backup_id, status, created_at
                   ) VALUES(?,?,'reserved',?)""",
                (request_id, backup_id, utc_now()),
            )
            row = creation_for_request(conn, request_id)
        conn.execute("COMMIT")
        return row
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def complete_backup_creation(
    conn: sqlite3.Connection,
    *,
    request_id: str,
    record: BackupRecord,
) -> None:
    """Complete the reserved identity inside the caller's result transaction."""

    if not conn.in_transaction:
        raise RuntimeError("backup creation completion requires an active transaction")
    row = creation_for_request(conn, request_id)
    if row is None:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "backup creation identity is missing",
            rule="backup_creation_identity_missing",
            retryable=False,
            details={"request_id": request_id, "backup_id": record.backup_id},
        )
    if row["backup_id"] != record.backup_id:
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
    if row["status"] == "completed":
        if row["sha256"] != record.sha256 or row["size_bytes"] != record.size_bytes:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "completed backup creation metadata no longer matches its file",
                rule="backup_creation_metadata_mismatch",
                retryable=False,
                details={"request_id": request_id, "backup_id": record.backup_id},
            )
        return
    cursor = conn.execute(
        """UPDATE backup_creations
              SET status='completed', sha256=?, size_bytes=?, completed_at=?
            WHERE request_id=? AND status='reserved'""",
        (record.sha256, record.size_bytes, utc_now(), request_id),
    )
    if cursor.rowcount != 1:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "backup creation completion could not be made durable",
            rule="backup_creation_completion_missing",
            retryable=False,
            details={"request_id": request_id, "backup_id": record.backup_id},
        )
