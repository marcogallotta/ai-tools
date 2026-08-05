"""Write-attempt identity and recovery state handling."""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .database import record_audit
from .transactions import savepoint_transaction
from .errors import DishRuleError
from .transactions import immediate_transaction
from .models import ProcessIdentity, utc_now


def _linux_process_start(pid: int) -> str | None:
    stat_path = Path(f"/proc/{pid}/stat")
    try:
        stat = stat_path.read_text()
        close_paren = stat.rfind(")")
        if close_paren < 0:
            return None
        tail = stat[close_paren + 2 :].split()
        start_ticks = tail[19]
        try:
            boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        except OSError:
            boot_id = "unknown-boot"
        return f"{boot_id}:{start_ticks}"
    except (OSError, IndexError):
        return None


def current_process_identity() -> ProcessIdentity:
    pid = os.getpid()
    process_start = _linux_process_start(pid)
    if process_start is None:
        process_start = f"fallback:{pid}"
    return ProcessIdentity(
        hostname=socket.gethostname(),
        pid=pid,
        process_start=process_start,
    )


def process_identity_is_live(identity: ProcessIdentity) -> bool:
    # A different host cannot be inspected safely from this local-only tool, so
    # recovery fails closed and treats that recorded process as live.
    if identity.hostname != socket.gethostname():
        return True
    current_start = _linux_process_start(identity.pid)
    if current_start is not None:
        return current_start == identity.process_start
    try:
        os.kill(identity.pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return identity.process_start == f"fallback:{identity.pid}"


def begin_operation_write_attempt(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    expected_identity: str,
    intended_identity: str | None,
    intended_title: str | None = None,
    intended_notes: str | None = None,
    schema_version: str | None = None,
    purpose: str = "content_write",
    context: dict[str, object] | None = None,
    expected_modified_at: str | None = None,
    version_source: str | None = None,
    version_reliable: bool = False,
) -> str:
    """Persist one complete write intent before the backend call begins."""
    attempt_id = str(uuid.uuid4())
    try:
        with savepoint_transaction(conn, "write_attempt_start"):
            conn.execute(
                """INSERT INTO write_attempts (
                    attempt_id, operation_id, expected_identity, intended_identity, outcome, started_at,
                    purpose, intended_title, intended_notes, schema_version, context_json,
                    expected_modified_at, version_source, version_reliable
                ) VALUES (?, ?, ?, ?, 'started', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (attempt_id, operation_id, expected_identity, intended_identity, utc_now(), purpose,
                 intended_title, intended_notes, schema_version,
                 None if context is None else json.dumps(context, sort_keys=True, separators=(",", ":")),
                 expected_modified_at, version_source, int(version_reliable)),
            )
            operation = conn.execute(
                "SELECT task_gid FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise DishRuleError(
                    "NOT_FOUND", "operation not found", rule="operation_not_found"
                )
            record_audit(
                conn, submission_id=None, task_gid=operation["task_gid"],
                operation_id=operation_id, event_type="write_attempt.started",
                actor_agent=None,
                details={
                    "attempt_id": attempt_id, "purpose": purpose,
                    "intended_identity": intended_identity,
                },
                result_code="OK", result_ok=True,
            )
    except sqlite3.IntegrityError as exc:
        if "write_attempts.operation_id" not in str(exc):
            raise
        unresolved = conn.execute(
            """SELECT attempt_id, outcome FROM write_attempts
                 WHERE operation_id=? AND outcome IN ('started','uncertain')
                 ORDER BY started_at LIMIT 1""",
            (operation_id,),
        ).fetchone()
        raise DishRuleError(
            "CONFLICT",
            "an earlier write attempt must be recovered before another write can begin",
            rule="unresolved_write_attempt",
            details={} if unresolved is None else {
                "attempt_id": unresolved["attempt_id"],
                "outcome": unresolved["outcome"],
            },
        ) from exc
    return attempt_id


def finish_operation_write_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    outcome: str,
) -> sqlite3.Row:
    if outcome not in {"not_applied", "uncertain"}:
        raise ValueError("confirmed writes must use finalize_confirmed_write_attempt with exact live evidence")
    with savepoint_transaction(conn, "write_attempt_finish"):
        cursor = conn.execute(
            """UPDATE write_attempts
                  SET outcome = ?, finished_at = ?
                WHERE attempt_id = ? AND outcome = 'started'""",
            (outcome, utc_now(), attempt_id),
        )
        if cursor.rowcount != 1:
            raise DishRuleError(
                "CONFLICT", "write attempt is no longer open",
                rule="stale_write_attempt",
            )
        row = conn.execute(
            "SELECT * FROM write_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        operation = conn.execute(
            "SELECT task_gid FROM operations WHERE operation_id = ?",
            (row["operation_id"],),
        ).fetchone()
        record_audit(
            conn, submission_id=None, task_gid=operation["task_gid"],
            operation_id=row["operation_id"], event_type="write_attempt.finished",
            actor_agent=None, details={"attempt_id": attempt_id, "outcome": outcome},
            result_code="OK", result_ok=True,
        )
    return row


def begin_movement_attempt(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    expected_section_gid: str | None,
    intended_section_gid: str,
    purpose: str = "unspecified",
    expected_modified_at: str | None = None,
    version_source: str | None = None,
    version_reliable: bool = False,
) -> str:
    attempt_id = str(uuid.uuid4())
    try:
        with savepoint_transaction(conn, "movement_attempt_start"):
            conn.execute(
                """INSERT INTO movement_attempts (
                    attempt_id, operation_id, expected_section_gid, intended_section_gid,
                    outcome, started_at, purpose, expected_modified_at, version_source,
                    version_reliable
                ) VALUES (?, ?, ?, ?, 'started', ?, ?, ?, ?, ?)""",
                (attempt_id, operation_id, expected_section_gid, intended_section_gid,
                 utc_now(), purpose, expected_modified_at, version_source, int(version_reliable)),
            )
            operation = conn.execute(
                "SELECT task_gid FROM operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise DishRuleError(
                    "NOT_FOUND", "operation not found", rule="operation_not_found"
                )
            record_audit(
                conn, submission_id=None, task_gid=operation["task_gid"],
                operation_id=operation_id, event_type="movement_attempt.started",
                actor_agent=None,
                details={
                    "attempt_id": attempt_id, "purpose": purpose,
                    "intended_section_gid": intended_section_gid,
                },
                result_code="OK", result_ok=True,
            )
    except sqlite3.IntegrityError as exc:
        if "movement_attempts.operation_id" not in str(exc):
            raise
        unresolved = conn.execute(
            """SELECT attempt_id, outcome FROM movement_attempts
                 WHERE operation_id=? AND outcome IN ('started','uncertain')
                 ORDER BY started_at LIMIT 1""",
            (operation_id,),
        ).fetchone()
        raise DishRuleError(
            "CONFLICT",
            "an earlier movement attempt must be recovered before another movement can begin",
            rule="unresolved_movement_attempt",
            details={} if unresolved is None else {
                "attempt_id": unresolved["attempt_id"],
                "outcome": unresolved["outcome"],
            },
        ) from exc
    return attempt_id


def finish_movement_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    outcome: str,
) -> sqlite3.Row:
    if outcome not in {"not_applied", "uncertain"}:
        raise ValueError("confirmed movements must use finalize_confirmed_movement_attempt with exact live evidence")
    with savepoint_transaction(conn, "movement_attempt_finish"):
        cursor = conn.execute(
            """UPDATE movement_attempts
                  SET outcome = ?, finished_at = ?
                WHERE attempt_id = ? AND outcome = 'started'""",
            (outcome, utc_now(), attempt_id),
        )
        if cursor.rowcount != 1:
            raise DishRuleError(
                "CONFLICT", "movement attempt is no longer open",
                rule="stale_movement_attempt",
            )
        row = conn.execute(
            "SELECT * FROM movement_attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        operation = conn.execute(
            "SELECT task_gid FROM operations WHERE operation_id = ?",
            (row["operation_id"],),
        ).fetchone()
        record_audit(
            conn, submission_id=None, task_gid=operation["task_gid"],
            operation_id=row["operation_id"], event_type="movement_attempt.finished",
            actor_agent=None, details={"attempt_id": attempt_id, "outcome": outcome},
            result_code="OK", result_ok=True,
        )
    return row
