"""Write-attempt identity and recovery state handling."""

from __future__ import annotations

import os
import socket
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from .database import transition_submission
from .errors import DishRuleError
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
        updates["notes_written_at"] = utc_now()

    assignments = ["status = ?"] + [f"{column} = ?" for column in updates]
    params = [target_state, *updates.values(), submission_id, attempt_id]
    conn.execute("BEGIN IMMEDIATE")
    try:
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
            conn.execute("ROLLBACK")
            raise DishRuleError(
                "CONFLICT",
                "write attempt no longer owns this submission",
                rule="stale_write_attempt",
            )
        row = conn.execute(
            "SELECT * FROM submissions WHERE submission_id = ?", (submission_id,)
        ).fetchone()
        conn.execute("COMMIT")
        return row
    except DishRuleError:
        raise
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
