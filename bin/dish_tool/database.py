"""Dish persistence operations, audit records, and state transitions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from typing import Any, Iterable, Mapping

from .constants import SUBMISSION_STATES
from .database_schema import MIGRATIONS, initialize_database, migrate_database
from .errors import DishRuleError
from .models import ContentIdentity, OperationActors, agent_family, utc_now

def record_audit(
    conn: sqlite3.Connection,
    *,
    submission_id: str | None,
    task_gid: str | None,
    event_type: str,
    actor_agent: str | None,
    details: Mapping[str, Any],
    created_at: str | None = None,
    operation_id: str | None = None,
    result_code: str | None = None,
    result_ok: bool | None = None,
    governed_kind: str | None = None,
    before_state: Mapping[str, Any] | None = None,
    after_state: Mapping[str, Any] | None = None,
    actor_run_id: str | None = None,
    actor_attestation: str | None = None,
    actor_source: str = "command",
) -> str:
    if actor_agent is not None:
        agent_family(actor_agent)
    if governed_kind not in {None, "lock", "exemption", "decision"}:
        raise ValueError("invalid governed audit kind")
    if governed_kind is not None and (before_state is None or after_state is None):
        raise ValueError("governed audit events require before and after state")
    provenance = {
        "agent": actor_agent,
        "run_id": str(actor_run_id or "").strip() or None,
        "independence_attestation": str(actor_attestation or "").strip() or None,
        "source": actor_source,
    }
    event_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO audit_events (
            event_id, submission_id, task_gid, event_type, actor_agent, details,
            created_at, operation_id, result_code, result_ok, governed_kind,
            before_state, after_state, actor_provenance
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id, submission_id, task_gid, event_type, actor_agent,
            json.dumps(dict(details), sort_keys=True, separators=(",", ":")),
            created_at or utc_now(), operation_id, result_code,
            None if result_ok is None else int(result_ok), governed_kind,
            None if before_state is None else json.dumps(dict(before_state), sort_keys=True, separators=(",", ":")),
            None if after_state is None else json.dumps(dict(after_state), sort_keys=True, separators=(",", ":")),
            json.dumps(provenance, sort_keys=True, separators=(",", ":")),
        ),
    )
    return event_id


def latest_change_diff_telemetry(
    conn: sqlite3.Connection, submission_id: str
) -> dict[str, Any] | None:
    """Return the latest source-free prepare telemetry for a move-only retry."""

    rows = conn.execute(
        """
        SELECT details
          FROM audit_events
         WHERE submission_id = ?
           AND event_type = 'dish.prepare'
         ORDER BY created_at DESC, rowid DESC
        """,
        (submission_id,),
    ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details"])
        except (TypeError, json.JSONDecodeError):
            continue
        summary = details.get("change_diff")
        if isinstance(summary, dict):
            counts = {}
            for key in (
                "characters_added",
                "characters_removed",
                "lines_added",
                "lines_removed",
            ):
                value = summary.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    counts = {}
                    break
                counts[key] = value
            headings = summary.get("headings_changed")
            if counts and isinstance(headings, list) and all(
                isinstance(heading, str) for heading in headings
            ):
                counts["headings_changed"] = list(headings)
                return {"change_diff": counts}
        reason = details.get("change_diff_unavailable")
        if isinstance(reason, str) and reason:
            return {"change_diff_unavailable": reason}
    return None


def latest_successful_rejection_reason(
    conn: sqlite3.Connection, submission_id: str
) -> str | None:
    rows = conn.execute(
        """
        SELECT details
          FROM audit_events
         WHERE submission_id = ?
           AND event_type = 'dish.reject'
         ORDER BY created_at DESC
        """,
        (submission_id,),
    ).fetchall()
    for row in rows:
        try:
            details = json.loads(row["details"])
        except (TypeError, json.JSONDecodeError):
            continue
        reason = str(details.get("reason") or "").strip()
        if details.get("ok") is True and details.get("decision") == "reject" and reason:
            return reason
    return None


def get_submission(conn: sqlite3.Connection, submission_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM submissions WHERE submission_id = ?", (submission_id,)
    ).fetchone()
    if row is None:
        raise DishRuleError(
            "NOT_FOUND",
            f"submission not found: {submission_id}",
            rule="submission_not_found",
        )
    return row


def get_open_submission_for_task(
    conn: sqlite3.Connection, task_gid: str
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT *
          FROM submissions
         WHERE task_gid = ?
           AND status NOT IN ('consumed', 'discarded')
        """,
        (task_gid,),
    ).fetchone()


def create_submission(
    conn: sqlite3.Connection,
    *,
    task_gid: str,
    submission_kind: str,
    protocol_release: str,
    release_commit: str,
    protocol_bundle: Mapping[str, str],
    canonical_manifest_text: str,
    baseline_exemption_tags: Iterable[str] | None,
    baseline_title: str,
    baseline_title_fields: Mapping[str, Any] | None,
    editor_agent: str,
    change_level: str | None,
    change_reason: str | None,
    baseline_verification_line: str | None,
) -> sqlite3.Row:
    """Atomically claim the per-task lock and create a drafting submission."""

    editor_family = agent_family(editor_agent)
    submission_id = str(uuid.uuid4())
    baseline_json = (
        None
        if baseline_exemption_tags is None
        else json.dumps(
            sorted(set(baseline_exemption_tags)), separators=(",", ":")
        )
    )
    baseline_title_json = (
        None
        if baseline_title_fields is None
        else json.dumps(
            dict(baseline_title_fields),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        existing = get_open_submission_for_task(conn, task_gid)
        if existing is not None:
            raise DishRuleError(
                "CONFLICT",
                f"task already has an open submission: {existing['submission_id']}",
                rule="open_submission_exists",
                details={
                    "existing_submission_id": existing["submission_id"],
                    "existing_state": existing["status"],
                },
            )
        try:
            conn.execute(
                """
                INSERT INTO submissions (
                    submission_id, task_gid, submission_kind, protocol_release,
                    release_commit, protocol_bundle, canonical_manifest,
                    baseline_exemption_tags, baseline_title, baseline_title_fields,
                    editor_agent, editor_family, change_level, change_reason,
                    baseline_verification_line, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'drafting', ?)
                """,
                (
                    submission_id,
                    task_gid,
                    submission_kind,
                    protocol_release,
                    release_commit,
                    json.dumps(
                        dict(protocol_bundle),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    canonical_manifest_text,
                    baseline_json,
                    baseline_title,
                    baseline_title_json,
                    editor_agent,
                    editor_family,
                    change_level,
                    change_reason,
                    baseline_verification_line,
                    utc_now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            if "submissions_one_open_per_task" in str(exc) or (
                "UNIQUE constraint failed: submissions.task_gid" in str(exc)
            ):
                raise DishRuleError(
                    "CONFLICT",
                    "task already has an open submission",
                    rule="open_submission_exists",
                ) from exc
            raise
        row = get_submission(conn, submission_id)
        conn.execute("COMMIT")
        return row
    except DishRuleError:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise

_ALLOWED_SUBMISSION_UPDATE_COLUMNS = {
    "prepared_exemption_tags",
    "prepared_title",
    "prepared_title_fields",
    "destination_section_name",
    "destination_section_gid",
    "exemption_revision",
    "editor_agent",
    "editor_family",
    "failed_verification_passes",
    "required_verifier_family",
    "verifier_agent",
    "verifier_family",
    "write_attempt_id",
    "in_flight_at",
    "in_flight_hostname",
    "in_flight_pid",
    "in_flight_process_start",
    "approved_at",
    "completed_at",
    "research_queue_moved_at",
    "task_content_written_at",
    "destination_moved_at",
}


def transition_submission(
    conn: sqlite3.Connection,
    submission_id: str,
    expected_states: Iterable[str],
    target_state: str,
    *,
    updates: Mapping[str, Any] | None = None,
) -> sqlite3.Row:
    expected = tuple(sorted(set(expected_states)))
    if not expected or not set(expected) <= SUBMISSION_STATES:
        raise ValueError("expected_states contains an invalid state")
    if target_state not in SUBMISSION_STATES:
        raise ValueError("target_state is invalid")
    changes = dict(updates or {})
    unknown = set(changes) - _ALLOWED_SUBMISSION_UPDATE_COLUMNS
    if unknown:
        raise ValueError(f"unsupported submission update columns: {sorted(unknown)}")

    assignments = ["status = ?"] + [f"{column} = ?" for column in changes]
    params: list[Any] = [target_state, *changes.values(), submission_id, *expected]
    placeholders = ",".join("?" for _ in expected)
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            f"""
            UPDATE submissions
               SET {", ".join(assignments)}
             WHERE submission_id = ?
               AND status IN ({placeholders})
            """,
            params,
        )
        if cursor.rowcount != 1:
            row = conn.execute(
                "SELECT status FROM submissions WHERE submission_id = ?",
                (submission_id,),
            ).fetchone()
            conn.execute("ROLLBACK")
            if row is None:
                raise DishRuleError(
                    "NOT_FOUND",
                    f"submission not found: {submission_id}",
                    rule="submission_not_found",
                )
            raise DishRuleError(
                "WRONG_STATE",
                f"submission is {row['status']}, expected one of {expected}",
                rule="wrong_state",
                details={"actual": row["status"], "expected": list(expected)},
            )
        row = conn.execute(
            "SELECT * FROM submissions WHERE submission_id = ?",
            (submission_id,),
        ).fetchone()
        conn.execute("COMMIT")
        return row
    except DishRuleError:
        raise
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def normalize_transport_text(value: str) -> str:
    """Normalize only the proven CRLF transport difference."""

    return str(value).replace("\r\n", "\n")


def content_identity(title: str, notes: str) -> ContentIdentity:
    clean_title = normalize_transport_text(title)
    clean_notes = normalize_transport_text(notes)
    payload = (
        len(clean_title.encode("utf-8")).to_bytes(8, "big")
        + clean_title.encode("utf-8")
        + len(clean_notes.encode("utf-8")).to_bytes(8, "big")
        + clean_notes.encode("utf-8")
    )
    return ContentIdentity(
        digest=hashlib.sha256(payload).hexdigest(),
        title=clean_title,
        notes=clean_notes,
    )


def confirm_task_content(
    conn: sqlite3.Connection,
    *,
    task_gid: str,
    title: str,
    notes: str,
    schema_version: str,
    operation_id: str | None = None,
    boundary: str = "confirmed",
) -> ContentIdentity:
    identity = content_identity(title, notes)
    now = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO content_versions (
                content_version_id, task_gid, operation_id, boundary, identity,
                title, notes, confirmed, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (str(uuid.uuid4()), task_gid, operation_id, boundary, identity.digest,
             identity.title, identity.notes, now),
        )
        conn.execute(
            """
            INSERT INTO task_content_state (
                task_gid, last_confirmed_identity, last_confirmed_title,
                last_confirmed_notes, schema_version, confirmed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_gid) DO UPDATE SET
                last_confirmed_identity=excluded.last_confirmed_identity,
                last_confirmed_title=excluded.last_confirmed_title,
                last_confirmed_notes=excluded.last_confirmed_notes,
                schema_version=excluded.schema_version,
                confirmed_at=excluded.confirmed_at
            """,
            (task_gid, identity.digest, identity.title, identity.notes, schema_version, now),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    return identity


def finalize_confirmed_write_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    task_gid: str,
    title: str,
    notes: str,
    schema_version: str,
) -> sqlite3.Row:
    """Atomically bind a confirmed external write to all local facts it proves."""
    identity = content_identity(title, notes)
    now = utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        attempt = conn.execute("SELECT * FROM write_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if attempt is None:
            raise DishRuleError("NOT_FOUND", "write attempt not found", rule="write_attempt_not_found")
        if attempt["outcome"] == "confirmed" and attempt["confirmed_content_version_id"]:
            version = conn.execute("SELECT * FROM content_versions WHERE content_version_id = ?", (attempt["confirmed_content_version_id"],)).fetchone()
            if version is None or version["identity"] != identity.digest:
                raise DishRuleError("CONFLICT", "confirmed write binding is inconsistent", rule="confirmed_write_binding_invalid")
            conn.execute("COMMIT")
            return version
        if attempt["outcome"] not in {"started", "uncertain", "confirmed"}:
            raise DishRuleError("CONFLICT", "write attempt cannot be confirmed from its current state", rule="stale_write_attempt")
        if attempt["intended_identity"] != identity.digest:
            raise DishRuleError("CONFLICT", "live content does not match the durable write intent", rule="write_intent_mismatch")
        version_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO content_versions (content_version_id, task_gid, operation_id, boundary, identity, title, notes, confirmed, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
            (version_id, task_gid, attempt["operation_id"], attempt["purpose"], identity.digest, identity.title, identity.notes, now),
        )
        conn.execute(
            """INSERT INTO task_content_state (task_gid, last_confirmed_identity, last_confirmed_title, last_confirmed_notes, schema_version, confirmed_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_gid) DO UPDATE SET
                 last_confirmed_identity=excluded.last_confirmed_identity, last_confirmed_title=excluded.last_confirmed_title,
                 last_confirmed_notes=excluded.last_confirmed_notes, schema_version=excluded.schema_version, confirmed_at=excluded.confirmed_at""",
            (task_gid, identity.digest, identity.title, identity.notes, schema_version, now),
        )
        conn.execute("UPDATE write_attempts SET outcome='confirmed', finished_at=?, confirmed_content_version_id=? WHERE attempt_id=?", (now, version_id, attempt_id))
        conn.execute("UPDATE operations SET content_write_completed_at=COALESCE(content_write_completed_at, ?) WHERE operation_id=?", (now, attempt["operation_id"]))
        if attempt["purpose"] == "signoff":
            context = json.loads(attempt["context_json"] or "{}")
            cycle_id = context.get("cycle_id")
            correction_class = context.get("correction_class")
            if not cycle_id:
                raise DishRuleError("INTERNAL_ERROR", "signoff intent lacks cycle identity", rule="signoff_intent_invalid")
            conn.execute(
                """UPDATE verification_cycles SET correction_class=?, outcome='approved', completed_at=?,
                       signed_content_version_id=?, signed_identity=?
                     WHERE cycle_id=? AND completed_at IS NULL""",
                (correction_class, now, version_id, identity.digest, cycle_id),
            )
            conn.execute("UPDATE operations SET signoff_completed_at=COALESCE(signoff_completed_at, ?) WHERE operation_id=?", (now, attempt["operation_id"]))
        record_audit(conn, submission_id=None, task_gid=task_gid, operation_id=attempt["operation_id"],
                     event_type="write_attempt.reconciled", actor_agent=None,
                     details={"attempt_id": attempt_id, "outcome": "confirmed", "purpose": attempt["purpose"], "content_version_id": version_id},
                     result_code="OK", result_ok=True)
        version = conn.execute("SELECT * FROM content_versions WHERE content_version_id = ?", (version_id,)).fetchone()
        conn.execute("COMMIT")
        return version
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def finalize_not_applied_write_attempt(conn: sqlite3.Connection, *, attempt_id: str) -> sqlite3.Row:
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT * FROM write_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise DishRuleError("NOT_FOUND", "write attempt not found", rule="write_attempt_not_found")
        if row["outcome"] not in {"started", "uncertain", "not_applied"}:
            raise DishRuleError("CONFLICT", "write attempt cannot be marked not applied", rule="stale_write_attempt")
        conn.execute("UPDATE write_attempts SET outcome='not_applied', finished_at=COALESCE(finished_at, ?) WHERE attempt_id=?", (utc_now(), attempt_id))
        record_audit(conn, submission_id=None, task_gid=conn.execute("SELECT task_gid FROM operations WHERE operation_id=?", (row["operation_id"],)).fetchone()[0], operation_id=row["operation_id"], event_type="write_attempt.reconciled", actor_agent=None, details={"attempt_id": attempt_id, "outcome": "not_applied", "purpose": row["purpose"]}, result_code="OK", result_ok=True)
        out=conn.execute("SELECT * FROM write_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        conn.execute("COMMIT")
        return out
    except Exception:
        if conn.in_transaction: conn.execute("ROLLBACK")
        raise


def finalize_confirmed_movement_attempt(conn: sqlite3.Connection, *, attempt_id: str, live_section_gid: str) -> sqlite3.Row:
    now=utc_now(); conn.execute("BEGIN IMMEDIATE")
    try:
        row=conn.execute("SELECT * FROM movement_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None: raise DishRuleError("NOT_FOUND", "movement attempt not found", rule="movement_attempt_not_found")
        if live_section_gid != row["intended_section_gid"]: raise DishRuleError("CONFLICT", "live placement does not match movement intent", rule="movement_intent_mismatch")
        if row["outcome"] not in {"started","uncertain","confirmed"}: raise DishRuleError("CONFLICT", "movement attempt cannot be confirmed", rule="stale_movement_attempt")
        conn.execute("UPDATE movement_attempts SET outcome='confirmed', finished_at=COALESCE(finished_at, ?), confirmed_section_gid=? WHERE attempt_id=?", (now, live_section_gid, attempt_id))
        if row["purpose"] == "destination_submission":
            conn.execute("UPDATE operations SET movement_completed_at=COALESCE(movement_completed_at, ?), destination_movement_attempt_id=? WHERE operation_id=?", (now, attempt_id, row["operation_id"]))
        task_gid=conn.execute("SELECT task_gid FROM operations WHERE operation_id=?", (row["operation_id"],)).fetchone()[0]
        record_audit(conn, submission_id=None, task_gid=task_gid, operation_id=row["operation_id"], event_type="movement_attempt.reconciled", actor_agent=None, details={"attempt_id":attempt_id,"outcome":"confirmed","purpose":row["purpose"],"section_gid":live_section_gid}, result_code="OK", result_ok=True)
        out=conn.execute("SELECT * FROM movement_attempts WHERE attempt_id=?", (attempt_id,)).fetchone(); conn.execute("COMMIT"); return out
    except Exception:
        if conn.in_transaction: conn.execute("ROLLBACK")
        raise


def finalize_not_applied_movement_attempt(conn: sqlite3.Connection, *, attempt_id: str) -> sqlite3.Row:
    now=utc_now(); conn.execute("BEGIN IMMEDIATE")
    try:
        row=conn.execute("SELECT * FROM movement_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None: raise DishRuleError("NOT_FOUND", "movement attempt not found", rule="movement_attempt_not_found")
        if row["outcome"] not in {"started","uncertain","not_applied"}: raise DishRuleError("CONFLICT", "movement attempt cannot be marked not applied", rule="stale_movement_attempt")
        conn.execute("UPDATE movement_attempts SET outcome='not_applied', finished_at=COALESCE(finished_at, ?) WHERE attempt_id=?", (now, attempt_id))
        task_gid=conn.execute("SELECT task_gid FROM operations WHERE operation_id=?", (row["operation_id"],)).fetchone()[0]
        record_audit(conn, submission_id=None, task_gid=task_gid, operation_id=row["operation_id"], event_type="movement_attempt.reconciled", actor_agent=None, details={"attempt_id":attempt_id,"outcome":"not_applied","purpose":row["purpose"]}, result_code="OK", result_ok=True)
        out=conn.execute("SELECT * FROM movement_attempts WHERE attempt_id=?", (attempt_id,)).fetchone(); conn.execute("COMMIT"); return out
    except Exception:
        if conn.in_transaction: conn.execute("ROLLBACK")
        raise


def assert_expected_identity(
    conn: sqlite3.Connection, *, task_gid: str, expected_identity: str
) -> sqlite3.Row:
    """Atomically reject stale callers before they can create an operation."""

    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM task_content_state WHERE task_gid = ?", (task_gid,)
        ).fetchone()
        if row is None or row["last_confirmed_identity"] != expected_identity:
            conn.execute("ROLLBACK")
            raise DishRuleError(
                "CONFLICT",
                "live task content differs from the expected identity",
                rule="stale_content_identity",
                details={
                    "expected_identity": expected_identity,
                    "actual_identity": None if row is None else row["last_confirmed_identity"],
                },
            )
        conn.execute("COMMIT")
        return row
    except DishRuleError:
        raise
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def create_operation(
    conn: sqlite3.Connection,
    *,
    task_gid: str,
    operation_kind: str,
    expected_identity: str,
    schema_version: str,
    actors: OperationActors = OperationActors(),
) -> sqlite3.Row:
    operation_id = str(uuid.uuid4())
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = conn.execute(
            "SELECT last_confirmed_identity FROM task_content_state WHERE task_gid = ?",
            (task_gid,),
        ).fetchone()
        actual = None if state is None else state["last_confirmed_identity"]
        if actual != expected_identity:
            raise DishRuleError(
                "CONFLICT", "live task content differs from the expected identity",
                rule="stale_content_identity",
                details={"expected_identity": expected_identity, "actual_identity": actual},
            )
        try:
            conn.execute(
                """
                INSERT INTO operations (
                    operation_id, task_gid, operation_kind, status, editor_agent,
                    researcher_agent, verifier_agent, run_id, independence_attestation,
                    expected_identity, schema_version, phase, created_at
                ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, 'prepare_required', ?)
                """,
                (operation_id, task_gid, operation_kind, actors.editor_agent,
                 actors.researcher_agent, actors.verifier_agent, actors.run_id,
                 actors.independence_attestation, expected_identity, schema_version, utc_now()),
            )
        except sqlite3.IntegrityError as exc:
            if "operations.task_gid" in str(exc):
                raise DishRuleError(
                    "CONFLICT", "task already has an open operation",
                    rule="open_operation_exists",
                ) from exc
            raise
        row = conn.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        record_audit(
            conn, submission_id=None, task_gid=task_gid,
            operation_id=operation_id, event_type="operation.created",
            actor_agent=actors.editor_agent or actors.researcher_agent,
            details={"operation_kind": operation_kind, "status": "open"},
            result_code="OK", result_ok=True, governed_kind="lock",
            before_state={"open_operation_id": None},
            after_state={"open_operation_id": operation_id, "status": "open"},
            actor_run_id=actors.run_id, actor_attestation=actors.independence_attestation,
        )
        conn.execute("COMMIT")
        return row
    except DishRuleError:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def mark_operation_completion(
    conn: sqlite3.Connection, operation_id: str, marker: str
) -> sqlite3.Row:
    columns = {
        "content_write": "content_write_completed_at",
        "signoff": "signoff_completed_at",
        "movement": "movement_completed_at",
    }
    try:
        column = columns[marker]
    except KeyError as exc:
        raise ValueError(f"unknown completion marker: {marker}") from exc
    conn.execute(f"UPDATE operations SET {column} = ? WHERE operation_id = ?", (utc_now(), operation_id))
    row = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if row is None:
        raise DishRuleError("NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found")
    record_audit(
        conn, submission_id=None, task_gid=row["task_gid"], operation_id=operation_id,
        event_type="operation.marker", actor_agent=None,
        details={"marker": marker}, result_code="OK", result_ok=True,
    )
    return row


def create_verification_cycle(
    conn: sqlite3.Connection, *, operation_id: str, task_gid: str,
    cycle_number: int, protocol_release: str, protocol_text: str | None = None,
    verifier_agent: str | None = None,
    run_id: str | None = None, independence_attestation: str | None = None,
    correction_class: str | None = None, outcome: str | None = None,
    route: str | None = None, resume_state: str | None = None,
) -> sqlite3.Row:
    cycle_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO verification_cycles (
            cycle_id, operation_id, task_gid, cycle_number, protocol_release, protocol_text,
            verifier_agent, run_id, independence_attestation, correction_class,
            outcome, route, resume_state, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (cycle_id, operation_id, task_gid, cycle_number, protocol_release, protocol_text,
         verifier_agent, run_id, independence_attestation, correction_class,
         outcome, route, resume_state, utc_now()),
    )
    row = conn.execute("SELECT * FROM verification_cycles WHERE cycle_id = ?", (cycle_id,)).fetchone()
    record_audit(
        conn, submission_id=None, task_gid=task_gid, operation_id=operation_id,
        event_type="verification_cycle.created", actor_agent=verifier_agent,
        details={"cycle_number": cycle_number, "protocol_release": protocol_release},
        result_code="OK", result_ok=True,
    )
    return row


def inspect_legacy_submissions(conn: sqlite3.Connection, *, task_gid: str | None = None) -> list[sqlite3.Row]:
    if task_gid is None:
        return conn.execute(
            "SELECT * FROM legacy_submission_quarantine ORDER BY quarantined_at, rowid"
        ).fetchall()
    return conn.execute(
        "SELECT * FROM legacy_submission_quarantine WHERE task_gid = ? ORDER BY quarantined_at, rowid",
        (task_gid,),
    ).fetchall()


_OPERATION_PHASE_ACTIONS = {
    "prepare_required": ("prepare",),
    "await_verification": ("verify", "approve", "reject"),
    "held_evidence": ("supply-evidence",),
    "held_human": ("record-human-decision",),
    "await_submission": ("submit",),
    "terminal": (),
}

def transition_operation(conn: sqlite3.Connection, operation_id: str, *, phase: str, status: str | None = None, terminal_outcome: str | None = None, inherited_signoff_cycle_id: str | None = None) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    if row is None:
        raise DishRuleError("NOT_FOUND", f"operation not found: {operation_id}", rule="operation_not_found")
    next_status = status or row["status"]
    completed_at = utc_now() if next_status in {"completed", "cancelled"} else row["completed_at"]
    conn.execute("""UPDATE operations SET phase=?, status=?, terminal_outcome=COALESCE(?, terminal_outcome), inherited_signoff_cycle_id=COALESCE(?, inherited_signoff_cycle_id), completed_at=? WHERE operation_id=?""", (phase, next_status, terminal_outcome, inherited_signoff_cycle_id, completed_at, operation_id))
    updated = conn.execute("SELECT * FROM operations WHERE operation_id = ?", (operation_id,)).fetchone()
    record_audit(conn, submission_id=None, task_gid=row["task_gid"], operation_id=operation_id, event_type="operation.transition", actor_agent=None, details={"from_phase": row["phase"], "to_phase": phase, "from_status": row["status"], "to_status": next_status, "terminal_outcome": terminal_outcome}, result_code="OK", result_ok=True)
    return updated

def legal_operation_actions(operation: Mapping[str, Any]) -> list[str]:
    if operation["status"] not in {"open", "uncertain"}:
        return []
    return list(_OPERATION_PHASE_ACTIONS.get(operation["phase"], ()))
