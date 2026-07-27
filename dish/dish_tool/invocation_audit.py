"""Success-preserving invocation audit persistence for dish command surfaces."""

from __future__ import annotations

import json
import pathlib
import sqlite3
import uuid
from typing import Any, Mapping, MutableMapping

from .constants import AGENT_FAMILIES
from .database import record_audit, record_command_audit_repair


def _operation_id(
    conn: sqlite3.Connection,
    result: Mapping[str, Any],
    submission_id: str | None,
) -> str | None:
    operation_id = (result.get("data") or {}).get("operation_id")
    if operation_id:
        return str(operation_id)
    if not submission_id:
        return None
    try:
        exists = conn.execute(
            "SELECT 1 FROM operations WHERE operation_id=?", (submission_id,)
        ).fetchone()
    except Exception:
        return None
    return submission_id if exists is not None else None


def _write_emergency_repair(conn: sqlite3.Connection, payload: Mapping[str, Any]) -> bool:
    """Persist a JSONL repair when the database repair table is unavailable."""
    db_row = conn.execute("PRAGMA database_list").fetchone()
    db_path = "" if db_row is None else str(db_row[2] or "")
    if not db_path or db_path == ":memory:":
        return False
    fallback = pathlib.Path(db_path + ".audit-repair.jsonl")
    fallback.parent.mkdir(parents=True, exist_ok=True)
    with fallback.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(payload), sort_keys=True) + "\n")
    return True


def record_invocation_audit(
    conn: sqlite3.Connection,
    *,
    surface: str,
    command: str,
    result: MutableMapping[str, Any],
    task_gid: str | None,
    submission_id: str | None,
    actor: Any = None,
    actor_role: str | None = None,
    actor_run_id: str | None = None,
    audit_details: Mapping[str, Any] | None = None,
) -> None:
    """Record an invocation without ever reversing an already-produced result.

    A failed final audit is converted into durable repair intent. If both SQLite
    audit writes fail, an emergency JSONL record is attempted. Failure of all
    three persistence paths is reported in the successful result but never
    raises a retry signal after the governed mutation has completed.
    """

    valid_actor = str(actor) if actor in AGENT_FAMILIES else None
    details: dict[str, Any] = {
        "command": command,
        "ok": bool(result.get("ok")),
        "code": result.get("code"),
        "state": result.get("state"),
        "retryable": bool(result.get("retryable")),
        "errors": list(result.get("errors") or ()),
    }
    if actor_role:
        details["actor_role"] = actor_role
    message = (result.get("data") or {}).get("message")
    if message:
        details["message"] = message
    if actor is not None and valid_actor is None:
        details["requested_agent"] = str(actor)

    supplied = dict(audit_details or {})
    governed = supplied.pop("governed_audit", None)
    details.update(supplied)
    audit_kwargs: dict[str, Any] = {
        "actor_run_id": str(actor_run_id or "").strip() or None,
    }
    if isinstance(governed, Mapping) and bool(result.get("ok")):
        audit_kwargs.update({
            "governed_kind": governed.get("kind"),
            "before_state": governed.get("before"),
            "after_state": governed.get("after"),
            "actor_attestation": governed.get("attestation"),
        })
        governed_run_id = str(governed.get("run_id") or "").strip()
        if governed_run_id:
            audit_kwargs["actor_run_id"] = governed_run_id

    operation_id = _operation_id(conn, result, submission_id)
    audit_submission_id = None
    if submission_id:
        try:
            if conn.execute(
                "SELECT 1 FROM submissions WHERE submission_id=?", (submission_id,)
            ).fetchone() is not None:
                audit_submission_id = submission_id
        except Exception:
            audit_submission_id = None
    event_type = f"{surface}.{command}"
    try:
        record_audit(
            conn,
            submission_id=audit_submission_id,
            task_gid=task_gid,
            operation_id=operation_id,
            event_type=event_type,
            actor_agent=valid_actor,
            details=details,
            result_code=result.get("code"),
            result_ok=bool(result.get("ok")),
            **audit_kwargs,
        )
        return
    except Exception as audit_exc:
        audit_error = f"{type(audit_exc).__name__}: {audit_exc}"

    repair_id = str(uuid.uuid4())
    persisted_in_database = False
    persisted_in_fallback = False
    repair_payload = dict(result)
    repair_payload["_audit_payload"] = {
        "event_type": event_type,
        "details": details,
        "audit_kwargs": audit_kwargs,
    }
    try:
        repair_id = record_command_audit_repair(
            conn,
            command=event_type,
            result=repair_payload,
            audit_error=audit_error,
            operation_id=operation_id,
            submission_id=audit_submission_id,
            task_gid=task_gid,
            actor_agent=valid_actor,
        )
        persisted_in_database = True
    except Exception as repair_exc:
        emergency = {
            "repair_id": repair_id,
            "command": event_type,
            "operation_id": operation_id,
            "submission_id": audit_submission_id,
            "task_gid": task_gid,
            "actor_agent": valid_actor,
            "result": repair_payload,
            "audit_error": audit_error,
            "repair_error": f"{type(repair_exc).__name__}: {repair_exc}",
        }
        try:
            persisted_in_fallback = _write_emergency_repair(conn, emergency)
        except Exception:
            persisted_in_fallback = False

    data = dict(result.get("data") or {})
    data.update(
        {
            "audit_repair_required": True,
            "audit_repair_id": repair_id,
            "audit_repair_persisted_in_database": persisted_in_database,
            "audit_repair_persisted_in_fallback": persisted_in_fallback,
        }
    )
    result["data"] = data
