"""Durable single-executor claims for operation-scoped mutations."""
from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass

from .errors import DishRuleError
from .models import ProcessIdentity, utc_now
from .recovery import current_process_identity, process_identity_is_live


@dataclass(frozen=True)
class OperationExecutionClaim:
    operation_id: str
    claim_id: str


def _identity(row) -> ProcessIdentity:
    return ProcessIdentity(
        hostname=row["hostname"],
        pid=int(row["pid"]),
        process_start=row["process_start"],
    )


def _recovery_pending(conn: sqlite3.Connection, operation_id: str) -> bool:
    return conn.execute(
        """SELECT 1 FROM write_attempts
             WHERE operation_id=? AND outcome IN ('started','uncertain')
           UNION ALL
           SELECT 1 FROM movement_attempts
             WHERE operation_id=? AND outcome IN ('started','uncertain')
           UNION ALL
           SELECT 1 FROM operation_steps
             WHERE operation_id=? AND completed_at IS NULL
           LIMIT 1""",
        (operation_id, operation_id, operation_id),
    ).fetchone() is not None


def claim_operation_execution(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    command: str,
) -> OperationExecutionClaim:
    """Atomically reserve one operation for one command executor.

    A dead process claim is discarded only when no durable workflow step or
    external-effect attempt requires recovery. Otherwise the operation remains
    fail-closed for administrative reconciliation.
    """
    identity = current_process_identity()
    claim_id = str(uuid.uuid4())
    conn.execute("BEGIN IMMEDIATE")
    try:
        operation = conn.execute(
            "SELECT status FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if operation is None:
            raise DishRuleError(
                "NOT_FOUND", "operation not found", rule="operation_not_found"
            )
        existing = conn.execute(
            "SELECT * FROM operation_execution_claims WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if existing is not None:
            if process_identity_is_live(_identity(existing)):
                raise DishRuleError(
                    "CONFLICT",
                    "another mutation is already executing for this operation",
                    rule="operation_mutation_in_progress",
                    retryable=True,
                    details={
                        "operation_id": operation_id,
                        "command": existing["command"],
                        "acquired_at": existing["acquired_at"],
                    },
                )
            if _recovery_pending(conn, operation_id):
                raise DishRuleError(
                    "CONFLICT",
                    "a crashed operation mutation requires recovery before another mutation",
                    rule="operation_mutation_recovery_required",
                    retryable=False,
                    details={
                        "operation_id": operation_id,
                        "command": existing["command"],
                    },
                )
            conn.execute(
                "DELETE FROM operation_execution_claims WHERE operation_id=? AND claim_id=?",
                (operation_id, existing["claim_id"]),
            )
        conn.execute(
            """INSERT INTO operation_execution_claims(
                   operation_id,claim_id,command,hostname,pid,process_start,acquired_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                operation_id,
                claim_id,
                str(command).strip(),
                identity.hostname,
                identity.pid,
                identity.process_start,
                utc_now(),
            ),
        )
        conn.execute("COMMIT")
        return OperationExecutionClaim(operation_id=operation_id, claim_id=claim_id)
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def release_operation_execution(
    conn: sqlite3.Connection, claim: OperationExecutionClaim
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "DELETE FROM operation_execution_claims WHERE operation_id=? AND claim_id=?",
            (claim.operation_id, claim.claim_id),
        )
        if cursor.rowcount != 1:
            raise DishRuleError(
                "CONFLICT",
                "operation execution claim changed before release",
                rule="operation_execution_claim_lost",
                retryable=False,
                details={"operation_id": claim.operation_id},
            )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
