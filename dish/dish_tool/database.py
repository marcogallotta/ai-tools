"""Dish persistence operations, audit records, and state transitions."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import uuid
from typing import Any, Iterable, Mapping, Sequence

from .constants import SUBMISSION_STATES
from .database_schema import MIGRATIONS, initialize_database, migrate_database
from .errors import DishRuleError
from .models import ContentIdentity, OperationActors, agent_family, utc_now


@contextlib.contextmanager
def atomic_persistence(conn: sqlite3.Connection, label: str):
    """Make a local state mutation and its required audit one SQLite unit."""
    savepoint = f"dish_{label}_{uuid.uuid4().hex}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        yield
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    else:
        conn.execute(f"RELEASE {savepoint}")

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


def record_command_audit_repair(
    conn: sqlite3.Connection, *, command: str, result: Mapping[str, Any], audit_error: str,
    operation_id: str | None = None, submission_id: str | None = None,
    task_gid: str | None = None, actor_agent: str | None = None,
) -> str:
    repair_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO command_audit_repairs(
               repair_id,command,operation_id,submission_id,task_gid,actor_agent,
               result_json,audit_error,created_at
           ) VALUES (?,?,?,?,?,?,?,?,?)""",
        (repair_id, command, operation_id, submission_id, task_gid, actor_agent,
         json.dumps(dict(result), sort_keys=True, separators=(",", ":")),
         str(audit_error), utc_now()),
    )
    return repair_id


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
    version_id = str(uuid.uuid4())
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            INSERT INTO content_versions (
                content_version_id, task_gid, operation_id, boundary, identity,
                title, notes, confirmed, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (version_id, task_gid, operation_id, boundary, identity.digest,
             identity.title, identity.notes, now),
        )
        conn.execute(
            """
            INSERT INTO task_content_state (
                task_gid, last_confirmed_identity, last_confirmed_title,
                last_confirmed_notes, schema_version, confirmed_at,
                last_confirmed_content_version_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_gid) DO UPDATE SET
                last_confirmed_identity=excluded.last_confirmed_identity,
                last_confirmed_title=excluded.last_confirmed_title,
                last_confirmed_notes=excluded.last_confirmed_notes,
                schema_version=excluded.schema_version,
                confirmed_at=excluded.confirmed_at,
                last_confirmed_content_version_id=excluded.last_confirmed_content_version_id
            """,
            (task_gid, identity.digest, identity.title, identity.notes, schema_version, now, version_id),
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
        version = None
        version_id = attempt["confirmed_content_version_id"]
        if version_id:
            version = conn.execute(
                "SELECT * FROM content_versions WHERE content_version_id = ?", (version_id,)
            ).fetchone()
            if (
                version is None
                or version["confirmed"] != 1
                or version["operation_id"] != attempt["operation_id"]
                or version["task_gid"] != task_gid
                or version["identity"] != identity.digest
                or version["title"] != identity.title
                or version["notes"] != identity.notes
            ):
                raise DishRuleError(
                    "CONFLICT",
                    "pre-existing write binding is inconsistent",
                    rule="write_attempt_partial_binding_invalid",
                )
        else:
            version_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO content_versions (content_version_id, task_gid, operation_id, boundary, identity, title, notes, confirmed, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (version_id, task_gid, attempt["operation_id"], attempt["purpose"], identity.digest, identity.title, identity.notes, now),
            )
        conn.execute(
            """INSERT INTO task_content_state (
                   task_gid, last_confirmed_identity, last_confirmed_title,
                   last_confirmed_notes, schema_version, confirmed_at,
                   last_confirmed_content_version_id
               ) VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(task_gid) DO UPDATE SET
                 last_confirmed_identity=excluded.last_confirmed_identity,
                 last_confirmed_title=excluded.last_confirmed_title,
                 last_confirmed_notes=excluded.last_confirmed_notes,
                 schema_version=excluded.schema_version,
                 confirmed_at=excluded.confirmed_at,
                 last_confirmed_content_version_id=excluded.last_confirmed_content_version_id""",
            (task_gid, identity.digest, identity.title, identity.notes, schema_version, now, version_id),
        )
        conn.execute("UPDATE write_attempts SET outcome='confirmed', finished_at=COALESCE(finished_at, ?), confirmed_content_version_id=? WHERE attempt_id=?", (now, version_id, attempt_id))
        conn.execute("UPDATE operations SET content_write_completed_at=COALESCE(content_write_completed_at, ?) WHERE operation_id=?", (now, attempt["operation_id"]))
        context = json.loads(attempt["context_json"] or "{}")
        if attempt["purpose"] == "signoff":
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
        authorization_ids = tuple(context.get("authorization_ids") or ())
        if authorization_ids:
            consume_reserved_marco_authorizations(
                conn, operation_id=attempt["operation_id"],
                authorization_ids=authorization_ids, candidate_identity=identity.digest,
            )
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
        context = json.loads(row["context_json"] or "{}")
        authorization_ids = tuple(context.get("authorization_ids") or ())
        if authorization_ids:
            release_marco_authorization_reservations(
                conn, operation_id=row["operation_id"], authorization_ids=authorization_ids,
            )
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
    expected_section_gid: str | None = None,
    actors: OperationActors = OperationActors(),
) -> sqlite3.Row:
    operation_id = str(uuid.uuid4())
    conn.execute("BEGIN IMMEDIATE")
    try:
        state = conn.execute(
            """SELECT last_confirmed_identity, last_confirmed_content_version_id
                 FROM task_content_state WHERE task_gid = ?""",
            (task_gid,),
        ).fetchone()
        actual = None if state is None else state["last_confirmed_identity"]
        if state is not None and not state["last_confirmed_content_version_id"]:
            raise DishRuleError(
                "CONFLICT",
                "task content baseline lacks exact confirmed evidence",
                rule="task_content_baseline_unproven",
            )
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
                    expected_identity, schema_version, expected_section_gid, phase, created_at
                ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, 'prepare_required', ?)
                """,
                (operation_id, task_gid, operation_kind, actors.editor_agent,
                 actors.researcher_agent, actors.verifier_agent, actors.run_id,
                 actors.independence_attestation, expected_identity, schema_version, expected_section_gid, utc_now()),
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
        role = "planner" if operation_kind == "planning" else ("constructor" if operation_kind == "initial" else "material_editor")
        actor = actors.researcher_agent or actors.editor_agent
        if actor:
            record_actor_fact(conn, operation_id=operation_id, task_gid=task_gid, role=role, agent=actor, run_id=actors.run_id, independence_attestation=actors.independence_attestation, candidate_identity=None)
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
    with atomic_persistence(conn, "operation_marker"):
        conn.execute(
            f"UPDATE operations SET {column} = ? WHERE operation_id = ?",
            (utc_now(), operation_id),
        )
        row = conn.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise DishRuleError(
                "NOT_FOUND", f"operation not found: {operation_id}",
                rule="operation_not_found",
            )
        record_audit(
            conn, submission_id=None, task_gid=row["task_gid"],
            operation_id=operation_id, event_type="operation.marker",
            actor_agent=None, details={"marker": marker},
            result_code="OK", result_ok=True,
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
    with atomic_persistence(conn, "verification_cycle"):
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
        row = conn.execute(
            "SELECT * FROM verification_cycles WHERE cycle_id = ?", (cycle_id,)
        ).fetchone()
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
    "prepare_required": ("prepare", "reject"),
    "await_verification": ("verify", "approve", "reject"),
    "held_evidence": ("supply-evidence",),
    "held_human": ("record-human-decision",),
    "await_submission": ("submit",),
    "ready_move_failed": ("submit", "repair-destination"),
    "terminal": (),
}

def begin_planning_reopen_attempt(
    conn: sqlite3.Connection,
    *,
    task_gid: str,
    expected_identity: str,
    expected_section_gid: str | None,
    expected_modified_at: str | None,
    reason: str,
    actor_run_id: str | None,
    request_id: str | None,
) -> sqlite3.Row:
    attempt_id = str(uuid.uuid4())
    conn.execute(
        """INSERT INTO planning_reopen_attempts(
               attempt_id,task_gid,request_id,expected_identity,expected_section_gid,
               expected_modified_at,reason,actor_run_id,outcome,created_at
           ) VALUES (?,?,?,?,?,?,?,?, 'started', ?)""",
        (
            attempt_id, task_gid, request_id, expected_identity, expected_section_gid,
            expected_modified_at, reason, actor_run_id, utc_now(),
        ),
    )
    return conn.execute(
        "SELECT * FROM planning_reopen_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()


def finish_planning_reopen_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    outcome: str,
    confirmed_modified_at: str | None = None,
) -> sqlite3.Row:
    if outcome not in {"confirmed", "not_applied", "uncertain"}:
        raise ValueError(f"invalid planning reopen outcome: {outcome}")
    conn.execute(
        """UPDATE planning_reopen_attempts
              SET outcome=?, finished_at=?, confirmed_modified_at=?
            WHERE attempt_id=? AND outcome='started'""",
        (outcome, utc_now(), confirmed_modified_at, attempt_id),
    )
    row = conn.execute(
        "SELECT * FROM planning_reopen_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if row is None or row["outcome"] != outcome:
        raise DishRuleError(
            "CONFLICT", "planning reopen attempt is not pending",
            rule="planning_reopen_attempt_not_pending",
        )
    return row


def current_dish_inspect_fact(
    conn: sqlite3.Connection, *, cycle: Mapping[str, Any], section_gid: str
) -> sqlite3.Row | None:
    """Return an exact inspect fact for the current reviewed cycle binding."""
    if not (
        cycle["reviewed_content_version_id"]
        and cycle["reviewed_identity"]
        and cycle["verifier_agent"]
        and str(cycle["run_id"] or "").strip()
    ):
        return None
    return conn.execute(
        """SELECT * FROM dish_inspect_facts
             WHERE operation_id=? AND cycle_id=? AND task_gid=?
               AND reviewed_content_version_id=? AND reviewed_identity=?
               AND verifier_agent=? AND run_id=?
               AND COALESCE(independence_attestation,'')=COALESCE(?, '')
               AND section_gid=?
             ORDER BY created_at DESC, rowid DESC LIMIT 1""",
        (
            cycle["operation_id"], cycle["cycle_id"], cycle["task_gid"],
            cycle["reviewed_content_version_id"], cycle["reviewed_identity"],
            cycle["verifier_agent"], cycle["run_id"],
            cycle["independence_attestation"], section_gid,
        ),
    ).fetchone()


def record_dish_inspect_fact(
    conn: sqlite3.Connection, *, cycle: Mapping[str, Any], section_gid: str
) -> sqlite3.Row:
    """Append one exact verifier inspection fact after an authoritative reread."""
    existing = current_dish_inspect_fact(conn, cycle=cycle, section_gid=section_gid)
    if existing is not None:
        return existing
    actor = conn.execute(
        """SELECT 1 FROM operation_actor_facts
             WHERE operation_id=? AND task_gid=? AND role='verifier'
               AND agent=? AND run_id=?
               AND COALESCE(independence_attestation,'')=COALESCE(?, '')
               AND candidate_identity=? AND source_cycle_id=? LIMIT 1""",
        (
            cycle["operation_id"], cycle["task_gid"], cycle["verifier_agent"],
            cycle["run_id"], cycle["independence_attestation"],
            cycle["reviewed_identity"], cycle["cycle_id"],
        ),
    ).fetchone()
    version = conn.execute(
        """SELECT 1 FROM content_versions
             WHERE content_version_id=? AND operation_id=? AND task_gid=?
               AND identity=? AND confirmed=1""",
        (
            cycle["reviewed_content_version_id"], cycle["operation_id"],
            cycle["task_gid"], cycle["reviewed_identity"],
        ),
    ).fetchone()
    if actor is None or version is None:
        raise DishRuleError(
            "CONFLICT",
            "the current Verification review binding is incomplete",
            rule="dish_inspect_review_binding_invalid",
        )
    fact_id = str(uuid.uuid4())
    with atomic_persistence(conn, "dish_inspect_fact"):
        conn.execute(
            """INSERT INTO dish_inspect_facts(
                   fact_id,operation_id,cycle_id,task_gid,reviewed_content_version_id,
                   reviewed_identity,verifier_agent,run_id,independence_attestation,
                   section_gid,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                fact_id, cycle["operation_id"], cycle["cycle_id"], cycle["task_gid"],
                cycle["reviewed_content_version_id"], cycle["reviewed_identity"],
                cycle["verifier_agent"], cycle["run_id"],
                cycle["independence_attestation"], section_gid, utc_now(),
            ),
        )
        record_audit(
            conn, submission_id=None, task_gid=cycle["task_gid"],
            operation_id=cycle["operation_id"], event_type="verification.inspected",
            actor_agent=cycle["verifier_agent"],
            actor_run_id=cycle["run_id"],
            actor_attestation=cycle["independence_attestation"],
            actor_source="dish-inspect",
            details={
                "fact_id": fact_id, "cycle_id": cycle["cycle_id"],
                "reviewed_identity": cycle["reviewed_identity"],
                "reviewed_content_version_id": cycle["reviewed_content_version_id"],
                "section_gid": section_gid,
            },
            result_code="OK", result_ok=True,
        )
    return conn.execute(
        "SELECT * FROM dish_inspect_facts WHERE fact_id=?", (fact_id,)
    ).fetchone()


def resolve_signoff_cycle_for_identity(
    conn: sqlite3.Connection, *, task_gid: str, identity: str
) -> sqlite3.Row | None:
    """Resolve the approved cycle whose signoff still governs an exact task head.

    A directly approved identity is authoritative itself. A completed
    non-material check-in carries that same cycle forward to its confirmed
    candidate identity, allowing any number of non-material check-ins without
    pretending that a later identity was independently re-verified.
    """
    direct = conn.execute(
        """SELECT cycle.*
             FROM verification_cycles AS cycle
             JOIN content_versions AS signed
               ON signed.content_version_id=cycle.signed_content_version_id
            WHERE cycle.task_gid=? AND cycle.outcome='approved'
              AND cycle.completed_at IS NOT NULL
              AND cycle.signed_identity=?
              AND signed.confirmed=1
              AND signed.task_gid=cycle.task_gid
              AND signed.identity=cycle.signed_identity
            ORDER BY cycle.completed_at DESC LIMIT 1""",
        (task_gid, identity),
    ).fetchone()
    if direct is not None:
        return direct
    return conn.execute(
        """SELECT cycle.*
             FROM operations AS lineage
             JOIN write_attempts AS candidate_write
               ON candidate_write.operation_id=lineage.operation_id
              AND candidate_write.outcome='confirmed'
             JOIN content_versions AS candidate
               ON candidate.content_version_id=candidate_write.confirmed_content_version_id
             JOIN verification_cycles AS cycle
               ON cycle.cycle_id=lineage.inherited_signoff_cycle_id
             JOIN content_versions AS signed
               ON signed.content_version_id=cycle.signed_content_version_id
            WHERE lineage.task_gid=?
              AND lineage.status='completed'
              AND lineage.terminal_outcome='non_material_checkin'
              AND candidate_write.intended_identity=?
              AND candidate.confirmed=1
              AND candidate.task_gid=lineage.task_gid
              AND candidate.identity=?
              AND cycle.task_gid=lineage.task_gid
              AND cycle.outcome='approved'
              AND cycle.completed_at IS NOT NULL
              AND signed.confirmed=1
              AND signed.task_gid=cycle.task_gid
              AND signed.identity=cycle.signed_identity
            ORDER BY lineage.completed_at DESC LIMIT 1""",
        (task_gid, identity, identity),
    ).fetchone()


def transition_operation(conn: sqlite3.Connection, operation_id: str, *, phase: str, status: str | None = None, terminal_outcome: str | None = None, inherited_signoff_cycle_id: str | None = None) -> sqlite3.Row:
    with atomic_persistence(conn, "operation_transition"):
        row = conn.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            raise DishRuleError(
                "NOT_FOUND", f"operation not found: {operation_id}",
                rule="operation_not_found",
            )
        next_status = status or row["status"]
        completed_at = (
            utc_now() if next_status in {"completed", "cancelled"}
            else row["completed_at"]
        )
        conn.execute(
            """UPDATE operations
                  SET phase=?, status=?,
                      terminal_outcome=COALESCE(?, terminal_outcome),
                      inherited_signoff_cycle_id=COALESCE(?, inherited_signoff_cycle_id),
                      completed_at=?
                WHERE operation_id=?""",
            (phase, next_status, terminal_outcome, inherited_signoff_cycle_id,
             completed_at, operation_id),
        )
        updated = conn.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        record_audit(
            conn, submission_id=None, task_gid=row["task_gid"],
            operation_id=operation_id, event_type="operation.transition",
            actor_agent=None,
            details={
                "from_phase": row["phase"], "to_phase": phase,
                "from_status": row["status"], "to_status": next_status,
                "terminal_outcome": terminal_outcome,
            },
            result_code="OK", result_ok=True,
        )
    return updated


def legal_operation_actions(operation: Mapping[str, Any]) -> list[str]:
    if operation["status"] not in {"open", "uncertain"}:
        return []
    return list(_OPERATION_PHASE_ACTIONS.get(operation["phase"], ()))


def declare_operation_step(conn: sqlite3.Connection, operation_id: str, step_name: str, intended: Mapping[str, Any]) -> sqlite3.Row:
    intended_json = json.dumps(dict(intended), sort_keys=True, separators=(",", ":"))
    existing = conn.execute(
        "SELECT * FROM operation_steps WHERE operation_id=? AND step_name=?",
        (operation_id, step_name),
    ).fetchone()
    if existing is not None:
        if existing["intended_json"] != intended_json:
            raise DishRuleError(
                "CONFLICT",
                "workflow retry intent differs from the persisted operation step",
                rule="operation_step_intent_mismatch",
                details={"step_name": step_name, "persisted": json.loads(existing["intended_json"]), "requested": dict(intended)},
            )
        return existing
    conn.execute(
        "INSERT INTO operation_steps(operation_id, step_name, intended_json) VALUES (?, ?, ?)",
        (operation_id, step_name, intended_json),
    )
    return conn.execute("SELECT * FROM operation_steps WHERE operation_id=? AND step_name=?", (operation_id, step_name)).fetchone()

def complete_operation_step(conn: sqlite3.Connection, operation_id: str, step_name: str) -> sqlite3.Row:
    conn.execute("UPDATE operation_steps SET completed_at=COALESCE(completed_at, ?) WHERE operation_id=? AND step_name=?", (utc_now(), operation_id, step_name))
    row=conn.execute("SELECT * FROM operation_steps WHERE operation_id=? AND step_name=?", (operation_id, step_name)).fetchone()
    if row is None:
        raise DishRuleError("INTERNAL_ERROR", "operation step intent is missing", rule="operation_step_missing")
    return row

def pending_operation_steps(conn: sqlite3.Connection, operation_id: str) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM operation_steps WHERE operation_id=? AND completed_at IS NULL ORDER BY rowid", (operation_id,)).fetchall()


def record_actor_fact(conn: sqlite3.Connection, *, operation_id: str, task_gid: str, role: str, agent: str, run_id: str | None = None, independence_attestation: str | None = None, candidate_identity: str | None = None, source_cycle_id: str | None = None) -> sqlite3.Row:
    """Record an immutable actor fact idempotently within one operation."""
    clean_run = str(run_id or '').strip() or None
    clean_attestation = str(independence_attestation or '').strip() or None
    with atomic_persistence(conn, "actor_fact"):
        if clean_run is not None:
            existing_rows = conn.execute(
                """SELECT * FROM operation_actor_facts
                     WHERE operation_id=? AND role=? AND run_id=?
                     ORDER BY created_at, fact_id""",
                (operation_id, role, clean_run),
            ).fetchall()
            for existing in existing_rows:
                if existing["agent"] != agent:
                    raise DishRuleError(
                        "CONFLICT",
                        "actor lineage fact conflicts with persisted history",
                        rule="actor_fact_conflict",
                        details={"role": role, "run_id": clean_run},
                    )
                if (
                    existing["candidate_identity"] == candidate_identity
                    and str(existing["independence_attestation"] or "").strip()
                    == str(clean_attestation or "").strip()
                ):
                    return existing
            if existing_rows:
                operation = conn.execute(
                    "SELECT expected_identity FROM operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                baseline = None if operation is None else operation["expected_identity"]
                if not all(
                    row["candidate_identity"] in {None, baseline}
                    for row in existing_rows
                ):
                    raise DishRuleError(
                        "CONFLICT",
                        "actor lineage fact conflicts with persisted candidate",
                        rule="actor_fact_conflict",
                        details={"role": role, "run_id": clean_run},
                    )
        fact_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO operation_actor_facts(
                   fact_id,operation_id,task_gid,role,agent,run_id,
                   independence_attestation,candidate_identity,source_cycle_id,created_at
               ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (fact_id, operation_id, task_gid, role, agent, clean_run,
             clean_attestation, candidate_identity, source_cycle_id, utc_now()),
        )
        row = conn.execute(
            "SELECT * FROM operation_actor_facts WHERE fact_id=?", (fact_id,)
        ).fetchone()
    return row


def assert_fresh_verifier(conn: sqlite3.Connection, *, operation_id: str, agent: str, run_id: str | None, independence_attestation: str | None) -> None:
    clean_run = str(run_id or '').strip()
    if not clean_run:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "a verifier run ID is required",
            rule="verifier_identity_required",
        )
    prior = conn.execute(
        """SELECT role FROM operation_actor_facts
             WHERE operation_id=? AND run_id=?
               AND role IN ('constructor','material_editor')
             LIMIT 1""",
        (operation_id, clean_run),
    ).fetchone()
    if prior is not None:
        raise DishRuleError("AGENT_MISMATCH", "verifier run is already part of the candidate lineage", rule="verifier_not_independent", details={"prior_role": prior['role']})


def record_marco_authorization(conn: sqlite3.Connection, *, task_gid: str, operation_id: str | None, field_name: str, before: Any, after: Any, reason: str, actor_run_id: str | None = None) -> sqlite3.Row:
    authorization_id = str(uuid.uuid4())
    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise DishRuleError("INVALID_ARGUMENT", "authorization reason is required", rule="authorization_reason_required")
    before_json = json.dumps(before, sort_keys=True)
    after_json = json.dumps(after, sort_keys=True)
    clean_run_id = str(actor_run_id or "").strip() or None
    existing = conn.execute(
        """SELECT * FROM marco_authorizations
             WHERE task_gid=? AND operation_id IS ?
               AND field_name=? AND before_json=? AND after_json=?
               AND reason=? AND actor_run_id IS ?
               AND consumed_at IS NULL
             ORDER BY created_at LIMIT 1""",
        (
            task_gid,
            operation_id,
            field_name,
            before_json,
            after_json,
            clean_reason,
            clean_run_id,
        ),
    ).fetchone()
    if existing is not None:
        return existing
    conn.execute(
        """INSERT INTO marco_authorizations(authorization_id,task_gid,operation_id,field_name,before_json,after_json,reason,actor_run_id,created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (authorization_id, task_gid, operation_id, field_name, before_json, after_json, clean_reason, clean_run_id, utc_now()),
    )
    record_audit(conn, submission_id=None, task_gid=task_gid, operation_id=operation_id, event_type="marco.authorization", actor_agent=None,
                 details={"authorization_id": authorization_id, "field": field_name, "reason": clean_reason}, result_code="OK", result_ok=True,
                 governed_kind="decision", before_state={field_name: before}, after_state={field_name: after}, actor_run_id=actor_run_id, actor_source="marco-admin")
    return conn.execute("SELECT * FROM marco_authorizations WHERE authorization_id=?", (authorization_id,)).fetchone()

def reserve_marco_authorizations(
    conn: sqlite3.Connection,
    *,
    task_gid: str,
    operation_id: str,
    changes: Sequence[Mapping[str, Any]],
) -> tuple[sqlite3.Row, ...]:
    """Resolve and reserve the full authorization set atomically.

    Nothing is modified unless every governed change has an exact unused
    authorization. Existing reservations owned by this operation are reusable;
    reservations owned by another operation fail closed.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        rows: list[sqlite3.Row] = []
        for change in changes:
            before_json = json.dumps(change["before"], sort_keys=True)
            after_json = json.dumps(change["after"], sort_keys=True)
            row = conn.execute(
                """SELECT * FROM marco_authorizations
                     WHERE task_gid=? AND (operation_id IS NULL OR operation_id=?)
                       AND field_name=? AND before_json=? AND after_json=?
                       AND consumed_at IS NULL
                       AND (reserved_by_operation_id IS NULL OR reserved_by_operation_id=?)
                     ORDER BY created_at LIMIT 1""",
                (task_gid, operation_id, change["field"], before_json, after_json, operation_id),
            ).fetchone()
            if row is None:
                raise DishRuleError(
                    "VALIDATION_FAILED",
                    "candidate changes governed facts without persisted Marco authorization",
                    rule="governed_change_unauthorized",
                    details={"field": change["field"]},
                )
            rows.append(row)
        now = utc_now()
        for row in rows:
            conn.execute(
                """UPDATE marco_authorizations
                      SET reserved_by_operation_id=?, reserved_at=COALESCE(reserved_at, ?)
                    WHERE authorization_id=? AND consumed_at IS NULL
                      AND (reserved_by_operation_id IS NULL OR reserved_by_operation_id=?)""",
                (operation_id, now, row["authorization_id"], operation_id),
            )
        reserved = tuple(
            conn.execute("SELECT * FROM marco_authorizations WHERE authorization_id=?", (row["authorization_id"],)).fetchone()
            for row in rows
        )
        conn.execute("COMMIT")
        return reserved
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def release_marco_authorization_reservations(
    conn: sqlite3.Connection, *, operation_id: str, authorization_ids: Sequence[str]
) -> None:
    if not authorization_ids:
        return
    placeholders = ",".join("?" for _ in authorization_ids)
    conn.execute(
        f"""UPDATE marco_authorizations
               SET reserved_by_operation_id=NULL, reserved_at=NULL
             WHERE reserved_by_operation_id=? AND consumed_at IS NULL
               AND authorization_id IN ({placeholders})""",
        (operation_id, *authorization_ids),
    )


def consume_reserved_marco_authorizations(
    conn: sqlite3.Connection,
    *,
    operation_id: str,
    authorization_ids: Sequence[str],
    candidate_identity: str,
) -> None:
    if not authorization_ids:
        return
    placeholders = ",".join("?" for _ in authorization_ids)
    now = utc_now()
    cursor = conn.execute(
        f"""UPDATE marco_authorizations
               SET consumed_at=?, consumed_identity=?
             WHERE reserved_by_operation_id=? AND consumed_at IS NULL
               AND authorization_id IN ({placeholders})""",
        (now, candidate_identity, operation_id, *authorization_ids),
    )
    if cursor.rowcount != len(authorization_ids):
        raise DishRuleError(
            "CONFLICT",
            "governed authorization reservation is incomplete",
            rule="governed_authorization_reservation_lost",
        )



def _import_command_audit_repair_fallback(conn: sqlite3.Connection) -> int:
    """Import emergency JSONL repairs written when SQLite repair insertion failed."""
    from pathlib import Path
    db_path = str(conn.execute("PRAGMA database_list").fetchone()[2] or "")
    if not db_path or db_path == ":memory:":
        return 0
    path = Path(db_path + ".audit-repair.jsonl")
    if not path.exists():
        return 0
    imported = 0
    remaining: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
            conn.execute(
                """INSERT OR IGNORE INTO command_audit_repairs(
                       repair_id,command,operation_id,submission_id,task_gid,actor_agent,
                       result_json,audit_error,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (item["repair_id"], item["command"], item.get("operation_id"),
                 item.get("submission_id"), item.get("task_gid"), item.get("actor_agent"),
                 json.dumps(item["result"], sort_keys=True, separators=(",", ":")),
                 item.get("audit_error", "emergency audit repair"), utc_now()),
            )
            imported += 1
        except Exception:
            remaining.append(line)
    if remaining:
        path.write_text("\n".join(remaining) + "\n", encoding="utf-8")
    else:
        path.unlink(missing_ok=True)
    return imported


def process_command_audit_repairs(conn: sqlite3.Connection, *, limit: int = 100) -> int:
    """Import and replay pending invocation-audit repairs exactly once."""
    _import_command_audit_repair_fallback(conn)
    conn.execute("BEGIN IMMEDIATE")
    repaired = 0
    try:
        rows = conn.execute(
            """SELECT * FROM command_audit_repairs
                 WHERE repaired_at IS NULL
                 ORDER BY created_at, repair_id LIMIT ?""",
            (limit,),
        ).fetchall()
        for row in rows:
            result = json.loads(row["result_json"])
            payload = result.get("_audit_payload") if isinstance(result, dict) else None
            if isinstance(payload, dict):
                event_type = str(payload.get("event_type") or row["command"])
                details = dict(payload.get("details") or {})
                audit_kwargs = dict(payload.get("audit_kwargs") or {})
            else:
                event_type = str(row["command"])
                if not (event_type.startswith("dish.") or event_type.startswith("dish-admin.")):
                    event_type = f"dish.{event_type}"
                details = {
                    "command": row["command"],
                    "ok": bool(result.get("ok")),
                    "code": result.get("code"),
                    "state": result.get("state"),
                    "retryable": bool(result.get("retryable")),
                    "errors": list(result.get("errors") or ()),
                }
                audit_kwargs = {}
            details.update({
                "repaired_from": row["repair_id"],
                "original_audit_error": row["audit_error"],
            })
            conn.execute("SAVEPOINT audit_repair")
            try:
                record_audit(
                    conn, submission_id=row["submission_id"],
                    task_gid=row["task_gid"], operation_id=row["operation_id"],
                    event_type=event_type, actor_agent=row["actor_agent"],
                    details=details, result_code=result.get("code"),
                    result_ok=bool(result.get("ok")),
                    actor_source="audit-repair-worker", **audit_kwargs,
                )
                cursor = conn.execute(
                    """UPDATE command_audit_repairs SET repaired_at=?
                         WHERE repair_id=? AND repaired_at IS NULL""",
                    (utc_now(), row["repair_id"]),
                )
                if cursor.rowcount != 1:
                    raise DishRuleError(
                        "CONFLICT", "audit repair was claimed by another worker",
                        rule="audit_repair_claim_lost",
                    )
                conn.execute("RELEASE audit_repair")
                repaired += 1
            except Exception:
                conn.execute("ROLLBACK TO audit_repair")
                conn.execute("RELEASE audit_repair")
                break
        conn.execute("COMMIT")
        return repaired
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
