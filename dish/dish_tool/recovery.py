"""Write-attempt identity and recovery state handling."""

from __future__ import annotations

import json
import math
import os
import socket
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .constants import RECOVERY_QUARANTINE_SECONDS
from .database import atomic_persistence, record_audit, transition_submission
from .errors import DishRuleError
from .transactions import immediate_transaction
from .models import ProcessIdentity, WriteAttempt, utc_now


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


def begin_write_attempt(conn: sqlite3.Connection, submission_id: str) -> WriteAttempt:
    attempt = WriteAttempt(
        attempt_id=str(uuid.uuid4()),
        started_at=utc_now(),
        identity=current_process_identity(),
    )
    transition_submission(
        conn,
        submission_id,
        {"ready"},
        "in_flight",
        updates={
            "write_attempt_id": attempt.attempt_id,
            "in_flight_at": attempt.started_at,
            "in_flight_hostname": attempt.identity.hostname,
            "in_flight_pid": attempt.identity.pid,
            "in_flight_process_start": attempt.identity.process_start,
        },
    )
    return attempt


def finish_write_attempt(
    conn: sqlite3.Connection,
    submission_id: str,
    *,
    attempt_id: str,
    target_state: str,
) -> sqlite3.Row:
    if target_state not in {"ready", "written", "uncertain"}:
        raise ValueError("write attempt target must be ready, written, or uncertain")
    updates: dict[str, Any] = {}
    if target_state == "ready":
        updates.update(
            {
                "write_attempt_id": None,
                "in_flight_at": None,
                "in_flight_hostname": None,
                "in_flight_pid": None,
                "in_flight_process_start": None,
            }
        )
    elif target_state == "written":
        updates["task_content_written_at"] = utc_now()

    assignments = ["status = ?"] + [f"{column} = ?" for column in updates]
    params = [target_state, *updates.values(), submission_id, attempt_id]
    with immediate_transaction(conn, "finish_write_attempt"):
        cursor = conn.execute(
            f"""
            UPDATE submissions
               SET {", ".join(assignments)}
             WHERE submission_id = ?
               AND status = 'in_flight'
               AND write_attempt_id = ?
            """,
            params,
        )
        if cursor.rowcount != 1:
            raise DishRuleError(
                "CONFLICT",
                "write attempt no longer owns this submission",
                rule="stale_write_attempt",
            )
        row = conn.execute(
            "SELECT * FROM submissions WHERE submission_id = ?", (submission_id,)
        ).fetchone()
        return row


def _parse_recorded_timestamp(value: Any) -> datetime:
    raw = str(value or "").strip()
    if not raw:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "write attempt is missing its recorded start time",
            rule="recovery_metadata_missing",
            details={"field": "in_flight_at"},
        )
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "write attempt has an invalid recorded start time",
            rule="recovery_metadata_invalid",
            details={"field": "in_flight_at"},
        ) from exc
    if parsed.tzinfo is None:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "write attempt start time is missing a timezone",
            rule="recovery_metadata_invalid",
            details={"field": "in_flight_at"},
        )
    return parsed.astimezone(timezone.utc)


def _recorded_identity(row: Mapping[str, Any]) -> ProcessIdentity:
    hostname = str(row["in_flight_hostname"] or "").strip()
    process_start = str(row["in_flight_process_start"] or "").strip()
    pid = row["in_flight_pid"]
    missing = []
    if not hostname:
        missing.append("in_flight_hostname")
    if pid is None:
        missing.append("in_flight_pid")
    if not process_start:
        missing.append("in_flight_process_start")
    if missing:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "write attempt is missing recorded process identity",
            rule="recovery_metadata_missing",
            details={"fields": missing},
        )
    try:
        clean_pid = int(pid)
    except (TypeError, ValueError) as exc:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "write attempt has an invalid recorded process ID",
            rule="recovery_metadata_invalid",
            details={"field": "in_flight_pid"},
        ) from exc
    return ProcessIdentity(
        hostname=hostname,
        pid=clean_pid,
        process_start=process_start,
    )


def validate_recovery_window(
    row: Mapping[str, Any],
    *,
    now: datetime,
    process_liveness_checker: Callable[[ProcessIdentity], bool] = process_identity_is_live,
) -> float:
    """Require a dead recorded process and an elapsed recovery quarantine."""

    if now.tzinfo is None:
        raise ValueError("recovery clock must be timezone-aware")
    identity = _recorded_identity(row)
    if process_liveness_checker(identity):
        raise DishRuleError(
            "CONFLICT",
            "recorded write process is still live",
            rule="recovery_process_live",
            retryable=True,
            details={"hostname": identity.hostname, "pid": identity.pid},
        )

    started_at = _parse_recorded_timestamp(row["in_flight_at"])
    elapsed = (now.astimezone(timezone.utc) - started_at).total_seconds()
    if elapsed < RECOVERY_QUARANTINE_SECONDS:
        remaining = max(0, math.ceil(RECOVERY_QUARANTINE_SECONDS - elapsed))
        raise DishRuleError(
            "CONFLICT",
            "recovery quarantine has not elapsed",
            rule="recovery_quarantine_active",
            retryable=True,
            details={
                "elapsed_seconds": max(0, elapsed),
                "required_seconds": RECOVERY_QUARANTINE_SECONDS,
                "remaining_seconds": remaining,
            },
        )
    return elapsed


def recover_write_attempt(
    conn: sqlite3.Connection,
    submission_id: str,
    *,
    attempt_id: str,
    target_state: str,
) -> sqlite3.Row:
    """Invalidate a stuck attempt and recover to ready or written atomically."""

    if target_state not in {"ready", "written"}:
        raise ValueError("recovery target must be ready or written")
    clean_attempt_id = str(attempt_id or "").strip()
    if not clean_attempt_id:
        raise DishRuleError(
            "INTERNAL_ERROR",
            "recoverable submission is missing its write-attempt ID",
            rule="recovery_metadata_missing",
            details={"field": "write_attempt_id"},
        )

    content_written_at = utc_now() if target_state == "written" else None

    with immediate_transaction(conn, "recover_write_attempt"):
        cursor = conn.execute(
            """
            UPDATE submissions
               SET status = ?,
                   task_content_written_at = ?,
                   write_attempt_id = NULL,
                   in_flight_at = NULL,
                   in_flight_hostname = NULL,
                   in_flight_pid = NULL,
                   in_flight_process_start = NULL
             WHERE submission_id = ?
               AND status IN ('in_flight', 'uncertain')
               AND write_attempt_id = ?
            """,
            (
                target_state,
                content_written_at,
                submission_id,
                clean_attempt_id,
            ),
        )
        if cursor.rowcount != 1:
            raise DishRuleError(
                "CONFLICT",
                "write attempt changed before recovery completed",
                rule="stale_write_attempt",
            )
        row = conn.execute(
            "SELECT * FROM submissions WHERE submission_id = ?", (submission_id,)
        ).fetchone()
        return row


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
) -> str:
    """Persist one complete write intent before the backend call begins."""
    attempt_id = str(uuid.uuid4())
    try:
        with atomic_persistence(conn, "write_attempt_start"):
            conn.execute(
                """INSERT INTO write_attempts (
                    attempt_id, operation_id, expected_identity, intended_identity, outcome, started_at,
                    purpose, intended_title, intended_notes, schema_version, context_json
                ) VALUES (?, ?, ?, ?, 'started', ?, ?, ?, ?, ?, ?)""",
                (attempt_id, operation_id, expected_identity, intended_identity, utc_now(), purpose,
                 intended_title, intended_notes, schema_version,
                 None if context is None else json.dumps(context, sort_keys=True, separators=(",", ":"))),
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
    with atomic_persistence(conn, "write_attempt_finish"):
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
) -> str:
    attempt_id = str(uuid.uuid4())
    try:
        with atomic_persistence(conn, "movement_attempt_start"):
            conn.execute(
                """INSERT INTO movement_attempts (
                    attempt_id, operation_id, expected_section_gid, intended_section_gid,
                    outcome, started_at, purpose
                ) VALUES (?, ?, ?, ?, 'started', ?, ?)""",
                (attempt_id, operation_id, expected_section_gid, intended_section_gid,
                 utc_now(), purpose),
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
    with atomic_persistence(conn, "movement_attempt_finish"):
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
