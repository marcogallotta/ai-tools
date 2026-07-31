"""Dish persistence operations, audit records, and state transitions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from .audit_repair_sidecar import fsync_parent, locked_audit_repair_sidecar
from .constants import SUBMISSION_STATES
from .database_schema import MIGRATIONS, initialize_database, migrate_database
from .errors import DishRuleError
from .models import ContentIdentity, OperationActors, agent_family, utc_now
from .transactions import immediate_transaction, savepoint_transaction


@dataclass(frozen=True)
class _AbandonmentSuccessionSpec:
    abandonment_id: str
    succession_id: str
    successor_operation_id: str
    source_content_version_id: str
    successor_content_version_id: str
    successor_operation_kind: str
    successor_phase: str
    successor_expected_section_gid: str
    successor_schema_version: str
    successor_claim_mode: str
    transition_reason: str
    candidate_transfer_kind: str
    source_cycle_id: str | None
    close_source_cycle_as_abandoned: bool
    successor_cycle_id: str | None
    successor_cycle_number: int | None
    successor_protocol_release: str | None
    successor_protocol_text: str | None
    successor_editor_agent: str | None
    successor_researcher_agent: str | None
    successor_verifier_agent: str | None
    successor_run_id: str | None
    successor_independence_attestation: str | None
    successor_actor_facts: Sequence[Mapping[str, Any]]
    successor_completed_steps: Mapping[str, Mapping[str, Any]]
    result: Mapping[str, Any] | None
    created_at: str


def atomic_persistence(conn: sqlite3.Connection, label: str):
    """Backward-compatible alias for an isolated nested persistence unit."""

    return savepoint_transaction(conn, label)


def immediate_persistence(conn: sqlite3.Connection, label: str):
    """Backward-compatible alias for a serialized persistence unit."""

    return immediate_transaction(conn, label)


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
    with immediate_persistence(conn, "create_submission"):
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
        return row

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
    with immediate_persistence(conn, "transition_submission"):
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
        return row


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
    with immediate_persistence(conn, "confirm_task_content"):
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
    with immediate_persistence(conn, "finalize_confirmed_write_attempt"):
        attempt = conn.execute("SELECT * FROM write_attempts WHERE attempt_id = ?", (attempt_id,)).fetchone()
        if attempt is None:
            raise DishRuleError("NOT_FOUND", "write attempt not found", rule="write_attempt_not_found")
        if attempt["outcome"] == "confirmed" and attempt["confirmed_content_version_id"]:
            version = conn.execute("SELECT * FROM content_versions WHERE content_version_id = ?", (attempt["confirmed_content_version_id"],)).fetchone()
            if version is None or version["identity"] != identity.digest:
                raise DishRuleError("CONFLICT", "confirmed write binding is inconsistent", rule="confirmed_write_binding_invalid")
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
        return version


def finalize_not_applied_write_attempt(
    conn: sqlite3.Connection, *, attempt_id: str
) -> sqlite3.Row:
    with immediate_persistence(conn, "finalize_not_applied_write_attempt"):
        row = conn.execute(
            "SELECT * FROM write_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise DishRuleError(
                "NOT_FOUND", "write attempt not found", rule="write_attempt_not_found"
            )
        if row["outcome"] not in {"started", "uncertain", "not_applied"}:
            raise DishRuleError(
                "CONFLICT",
                "write attempt cannot be marked not applied",
                rule="stale_write_attempt",
            )
        conn.execute(
            """UPDATE write_attempts
                  SET outcome='not_applied', finished_at=COALESCE(finished_at, ?)
                WHERE attempt_id=?""",
            (utc_now(), attempt_id),
        )
        context = json.loads(row["context_json"] or "{}")
        authorization_ids = tuple(context.get("authorization_ids") or ())
        if authorization_ids:
            release_marco_authorization_reservations(
                conn,
                operation_id=row["operation_id"],
                authorization_ids=authorization_ids,
            )
        task_gid = conn.execute(
            "SELECT task_gid FROM operations WHERE operation_id=?",
            (row["operation_id"],),
        ).fetchone()[0]
        record_audit(
            conn,
            submission_id=None,
            task_gid=task_gid,
            operation_id=row["operation_id"],
            event_type="write_attempt.reconciled",
            actor_agent=None,
            details={
                "attempt_id": attempt_id,
                "outcome": "not_applied",
                "purpose": row["purpose"],
            },
            result_code="OK",
            result_ok=True,
        )
        return conn.execute(
            "SELECT * FROM write_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()



def finalize_confirmed_movement_attempt(
    conn: sqlite3.Connection, *, attempt_id: str, live_section_gid: str
) -> sqlite3.Row:
    now = utc_now()
    with immediate_persistence(conn, "finalize_confirmed_movement_attempt"):
        row = conn.execute(
            "SELECT * FROM movement_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise DishRuleError(
                "NOT_FOUND",
                "movement attempt not found",
                rule="movement_attempt_not_found",
            )
        if live_section_gid != row["intended_section_gid"]:
            raise DishRuleError(
                "CONFLICT",
                "live placement does not match movement intent",
                rule="movement_intent_mismatch",
            )
        if row["outcome"] not in {"started", "uncertain", "confirmed"}:
            raise DishRuleError(
                "CONFLICT",
                "movement attempt cannot be confirmed",
                rule="stale_movement_attempt",
            )
        conn.execute(
            """UPDATE movement_attempts
                  SET outcome='confirmed', finished_at=COALESCE(finished_at, ?),
                      confirmed_section_gid=?
                WHERE attempt_id=?""",
            (now, live_section_gid, attempt_id),
        )
        if row["purpose"] == "destination_submission":
            conn.execute(
                """UPDATE operations
                      SET movement_completed_at=COALESCE(movement_completed_at, ?),
                          destination_movement_attempt_id=?
                    WHERE operation_id=?""",
                (now, attempt_id, row["operation_id"]),
            )
        task_gid = conn.execute(
            "SELECT task_gid FROM operations WHERE operation_id=?",
            (row["operation_id"],),
        ).fetchone()[0]
        record_audit(
            conn,
            submission_id=None,
            task_gid=task_gid,
            operation_id=row["operation_id"],
            event_type="movement_attempt.reconciled",
            actor_agent=None,
            details={
                "attempt_id": attempt_id,
                "outcome": "confirmed",
                "purpose": row["purpose"],
                "section_gid": live_section_gid,
            },
            result_code="OK",
            result_ok=True,
        )
        return conn.execute(
            "SELECT * FROM movement_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()



def finalize_not_applied_movement_attempt(
    conn: sqlite3.Connection, *, attempt_id: str
) -> sqlite3.Row:
    now = utc_now()
    with immediate_persistence(conn, "finalize_not_applied_movement_attempt"):
        row = conn.execute(
            "SELECT * FROM movement_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise DishRuleError(
                "NOT_FOUND",
                "movement attempt not found",
                rule="movement_attempt_not_found",
            )
        if row["outcome"] not in {"started", "uncertain", "not_applied"}:
            raise DishRuleError(
                "CONFLICT",
                "movement attempt cannot be marked not applied",
                rule="stale_movement_attempt",
            )
        conn.execute(
            """UPDATE movement_attempts
                  SET outcome='not_applied', finished_at=COALESCE(finished_at, ?)
                WHERE attempt_id=?""",
            (now, attempt_id),
        )
        task_gid = conn.execute(
            "SELECT task_gid FROM operations WHERE operation_id=?",
            (row["operation_id"],),
        ).fetchone()[0]
        record_audit(
            conn,
            submission_id=None,
            task_gid=task_gid,
            operation_id=row["operation_id"],
            event_type="movement_attempt.reconciled",
            actor_agent=None,
            details={
                "attempt_id": attempt_id,
                "outcome": "not_applied",
                "purpose": row["purpose"],
            },
            result_code="OK",
            result_ok=True,
        )
        return conn.execute(
            "SELECT * FROM movement_attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone()



def assert_expected_identity(
    conn: sqlite3.Connection, *, task_gid: str, expected_identity: str
) -> sqlite3.Row:
    """Atomically reject stale callers before they can create an operation."""

    with immediate_persistence(conn, "assert_expected_identity"):
        row = conn.execute(
            "SELECT * FROM task_content_state WHERE task_gid = ?", (task_gid,)
        ).fetchone()
        if row is None or row["last_confirmed_identity"] != expected_identity:
            raise DishRuleError(
                "CONFLICT",
                "live task content differs from the expected identity",
                rule="stale_content_identity",
                details={
                    "expected_identity": expected_identity,
                    "actual_identity": None if row is None else row["last_confirmed_identity"],
                },
            )
        return row


def create_operation(
    conn: sqlite3.Connection,
    *,
    task_gid: str,
    operation_kind: str,
    expected_identity: str,
    schema_version: str,
    expected_section_gid: str | None = None,
    actors: OperationActors = OperationActors(),
    initial_steps: Mapping[str, Mapping[str, Any]] | None = None,
) -> sqlite3.Row:
    operation_id = str(uuid.uuid4())
    with immediate_persistence(conn, "create_operation"):
        abandonment = conn.execute(
            """SELECT abandonment_id,status,source_operation_id,successor_operation_id
                 FROM abandonment_attempts
                WHERE task_gid=? AND status!='completed'
                ORDER BY created_at DESC LIMIT 1""",
            (task_gid,),
        ).fetchone()
        if abandonment is not None:
            command = (
                f"dish-admin reconcile-abandonment "
                f"{abandonment['abandonment_id']}"
            )
            raise DishRuleError(
                "WRONG_STATE",
                "task is fenced by an active permanent-run abandonment",
                rule="abandonment_fence_active",
                details={
                    "abandonment_id": abandonment["abandonment_id"],
                    "abandonment_status": abandonment["status"],
                    "source_operation_id": abandonment["source_operation_id"],
                    "successor_operation_id": abandonment["successor_operation_id"],
                    "required_admin_action": "reconcile-abandonment",
                    "admin_command": command,
                },
            )
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
        if operation_kind == "planning":
            blocker = planning_reopen_blocker_for_task(conn, task_gid=task_gid)
            if blocker is not None:
                raise DishRuleError(
                    "BACKEND_UNCERTAIN",
                    "Planning cannot start until the interrupted task reopen is reconciled",
                    rule="planning_reopen_reconciliation_required",
                    retryable=False,
                    details={
                        "attempt_id": blocker["attempt_id"],
                        "task_gid": task_gid,
                        "request_id": blocker["request_id"],
                    },
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
        for step_name, intended in dict(initial_steps or {}).items():
            declare_operation_step(conn, operation_id, step_name, intended)
            complete_operation_step(conn, operation_id, step_name)
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
        return row



def _require_writer_transaction(conn: sqlite3.Connection, *, operation: str) -> None:
    if not conn.in_transaction:
        raise RuntimeError(f"{operation} requires an existing SQLite writer transaction")


def get_abandonment_attempt(
    conn: sqlite3.Connection, abandonment_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM abandonment_attempts WHERE abandonment_id=?",
        (abandonment_id,),
    ).fetchone()
    if row is None:
        raise DishRuleError(
            "NOT_FOUND",
            "abandonment attempt not found",
            rule="abandonment_not_found",
            details={"abandonment_id": abandonment_id},
        )
    return row


def create_abandonment_attempt_in_transaction(
    conn: sqlite3.Connection,
    *,
    abandonment_id: str,
    task_gid: str,
    source_operation_id: str,
    source_lease_id: str,
    abandoned_owner_id: str,
    abandoned_run_id: str,
    reason: str,
    attempt_cycle_id: str | None = None,
    current_execution_id: str | None = None,
    created_at: str | None = None,
) -> sqlite3.Row:
    """Persist one exact abandoned actor attempt inside the caller's transaction.

    This is deliberately not an operator command.  The future admin use case
    must validate liveness and claim execution authority before calling it.
    Database triggers enforce the exact actor lease, latest-attempt, active-task,
    and one-active-abandonment boundaries.
    """

    _require_writer_transaction(conn, operation="abandonment creation")
    stamp = created_at or utc_now()
    try:
        conn.execute(
            """INSERT INTO abandonment_attempts(
                   abandonment_id,task_gid,source_operation_id,source_lease_id,
                   abandoned_owner_id,abandoned_run_id,attempt_cycle_id,status,
                   current_execution_id,reason,created_at,updated_at
               ) VALUES(?,?,?,?,?,?,?,'started',?,?,?,?)""",
            (
                abandonment_id,
                task_gid,
                source_operation_id,
                source_lease_id,
                abandoned_owner_id,
                abandoned_run_id,
                attempt_cycle_id,
                current_execution_id,
                str(reason).strip(),
                stamp,
                stamp,
            ),
        )
    except sqlite3.IntegrityError as exc:
        message = str(exc)
        if "abandonment_attempts.task_gid" in message:
            rule = "abandonment_task_in_progress"
            text = "task already has a non-completed abandonment"
        elif "abandonment_attempts.source_operation_id" in message:
            rule = "abandonment_attempt_exists"
            text = "the exact actor attempt already has an abandonment record"
        else:
            rule = "abandonment_authority_invalid"
            text = "abandonment authority does not match the exact latest actor attempt"
        raise DishRuleError("CONFLICT", text, rule=rule) from exc
    record_audit(
        conn,
        submission_id=None,
        task_gid=task_gid,
        operation_id=source_operation_id,
        event_type="operation.abandonment_started",
        actor_agent=None,
        details={
            "abandonment_id": abandonment_id,
            "source_lease_id": source_lease_id,
            "abandoned_owner_id": abandoned_owner_id,
            "abandoned_run_id": abandoned_run_id,
            "attempt_cycle_id": attempt_cycle_id,
            "reason": str(reason).strip(),
        },
        result_code="OK",
        result_ok=True,
    )
    return get_abandonment_attempt(conn, abandonment_id)


def bind_abandonment_execution_in_transaction(
    conn: sqlite3.Connection,
    *,
    abandonment_id: str,
    execution_id: str,
    resumed_at: str | None = None,
) -> sqlite3.Row:
    """Bind the exact live admin execution that is advancing an abandonment."""

    _require_writer_transaction(conn, operation="abandonment execution binding")
    abandonment = get_abandonment_attempt(conn, abandonment_id)
    execution = conn.execute(
        "SELECT * FROM operation_executions WHERE execution_id=?",
        (execution_id,),
    ).fetchone()
    if (
        execution is None
        or execution["operation_id"] != abandonment["source_operation_id"]
        or execution["command"] not in {"abandon-operation", "reconcile-abandonment"}
        or execution["status"] not in {"started", "uncertain"}
    ):
        raise DishRuleError(
            "CONFLICT",
            "abandonment execution does not match the source operation",
            rule="abandonment_execution_binding_invalid",
            details={"execution_id": execution_id},
        )
    if abandonment["status"] not in {"started", "blocked_manual_reconciliation"}:
        raise DishRuleError(
            "WRONG_STATE",
            "abandonment is not resumable by an admin execution",
            rule="abandonment_not_reconcilable",
            details={"status": abandonment["status"]},
        )
    if (
        abandonment["current_execution_id"] is not None
        and abandonment["current_execution_id"] != execution_id
    ):
        raise DishRuleError(
            "CONFLICT",
            "another admin execution is already bound to this abandonment",
            rule="abandonment_execution_conflict",
            details={
                "current_execution_id": abandonment["current_execution_id"],
                "requested_execution_id": execution_id,
            },
        )
    stamp = resumed_at or utc_now()
    was_blocked = abandonment["status"] == "blocked_manual_reconciliation"
    conn.execute(
        """UPDATE abandonment_attempts
              SET status='started', outcome=NULL, current_execution_id=?, updated_at=?
            WHERE abandonment_id=?""",
        (execution_id, stamp, abandonment_id),
    )
    if was_blocked:
        record_audit(
            conn,
            submission_id=None,
            task_gid=abandonment["task_gid"],
            operation_id=abandonment["source_operation_id"],
            event_type="operation.abandonment_resumed",
            actor_agent=None,
            details={
                "abandonment_id": abandonment_id,
                "execution_id": execution_id,
            },
            result_code="OK",
            result_ok=True,
        )
    return get_abandonment_attempt(conn, abandonment_id)


def assert_clean_abandonment_restart_source(
    conn: sqlite3.Connection, *, source_operation_id: str
) -> sqlite3.Row:
    """Require the narrow launch frontier used by restart succession.

    Stage policy still owns live baseline and placement classification.  This
    persistence guard prevents a caller from terminalizing an operation with
    incomplete workflow intent or unresolved external-effect evidence.
    """

    operation = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (source_operation_id,)
    ).fetchone()
    if operation is None:
        raise DishRuleError(
            "NOT_FOUND", "source operation not found", rule="operation_not_found"
        )
    if operation["status"] not in {"open", "uncertain"}:
        raise DishRuleError(
            "WRONG_STATE",
            "only an active operation can be abandoned",
            rule="abandonment_source_not_active",
            details={"status": operation["status"]},
        )
    pending = conn.execute(
        """SELECT step_name FROM operation_steps
             WHERE operation_id=? AND completed_at IS NULL
             ORDER BY rowid""",
        (source_operation_id,),
    ).fetchall()
    if pending:
        raise DishRuleError(
            "CONFLICT",
            "clean abandonment restart requires zero incomplete operation steps",
            rule="abandonment_pending_steps",
            details={"steps": [row["step_name"] for row in pending]},
        )
    unresolved = conn.execute(
        """SELECT 'write' AS effect_kind, attempt_id
             FROM write_attempts
            WHERE operation_id=? AND outcome IN ('started','uncertain')
           UNION ALL
           SELECT 'movement' AS effect_kind, attempt_id
             FROM movement_attempts
            WHERE operation_id=? AND outcome IN ('started','uncertain')
           ORDER BY effect_kind, attempt_id""",
        (source_operation_id, source_operation_id),
    ).fetchall()
    if unresolved:
        raise DishRuleError(
            "BACKEND_UNCERTAIN",
            "abandonment restart cannot cross unresolved external effects",
            rule="abandonment_unresolved_effects",
            retryable=False,
            details={
                "effects": [
                    {"kind": row["effect_kind"], "attempt_id": row["attempt_id"]}
                    for row in unresolved
                ]
            },
        )
    return operation


def _release_abandonment_source_lease_in_transaction(
    conn: sqlite3.Connection,
    *,
    abandonment: Mapping[str, Any],
    released_at: str,
) -> None:
    lease = conn.execute(
        "SELECT * FROM service_leases WHERE lease_id=?",
        (abandonment["source_lease_id"],),
    ).fetchone()
    if lease is None or (
        lease["operation_id"] != abandonment["source_operation_id"]
        or lease["task_gid"] != abandonment["task_gid"]
        or lease["owner_id"] != abandonment["abandoned_owner_id"]
        or lease["run_id"] != abandonment["abandoned_run_id"]
        or lease["lease_kind"] != "actor"
        or lease["context_cycle_id"] != abandonment["attempt_cycle_id"]
    ):
        raise DishRuleError(
            "CONFLICT",
            "abandonment source lease no longer matches its durable authority",
            rule="abandonment_source_lease_mismatch",
        )
    if lease["released_at"] is None:
        cursor = conn.execute(
            """UPDATE service_leases
                  SET released_at=?, release_reason='agent_abandoned'
                WHERE lease_id=? AND released_at IS NULL""",
            (released_at, lease["lease_id"]),
        )
        if cursor.rowcount != 1:
            raise DishRuleError(
                "CONFLICT",
                "abandonment source lease changed before terminalization",
                rule="abandonment_source_lease_conflict",
            )


def mark_abandonment_blocked_in_transaction(
    conn: sqlite3.Connection,
    *,
    abandonment_id: str,
    result: Mapping[str, Any],
    updated_at: str | None = None,
) -> sqlite3.Row:
    _require_writer_transaction(conn, operation="abandonment block")
    row = get_abandonment_attempt(conn, abandonment_id)
    stamp = updated_at or utc_now()
    conn.execute(
        """UPDATE abandonment_attempts
              SET status='blocked_manual_reconciliation',
                  outcome='blocked_manual_reconciliation',
                  current_execution_id=NULL, latest_result_json=?, updated_at=?
            WHERE abandonment_id=?""",
        (
            json.dumps(dict(result), sort_keys=True, separators=(",", ":")),
            stamp,
            abandonment_id,
        ),
    )
    record_audit(
        conn,
        submission_id=None,
        task_gid=row["task_gid"],
        operation_id=row["source_operation_id"],
        event_type="operation.abandonment_blocked",
        actor_agent=None,
        details={"abandonment_id": abandonment_id},
        result_code="BACKEND_UNCERTAIN",
        result_ok=False,
    )
    return get_abandonment_attempt(conn, abandonment_id)


def mark_abandonment_awaiting_hold_in_transaction(
    conn: sqlite3.Connection,
    *,
    abandonment_id: str,
    result: Mapping[str, Any],
    updated_at: str | None = None,
) -> sqlite3.Row:
    _require_writer_transaction(conn, operation="abandonment hold")
    row = get_abandonment_attempt(conn, abandonment_id)
    stamp = updated_at or utc_now()
    conn.execute(
        """UPDATE abandonment_attempts
              SET status='awaiting_hold_resolution', outcome='hold_preserved',
                  current_execution_id=NULL, latest_result_json=?, updated_at=?
            WHERE abandonment_id=?""",
        (
            json.dumps(dict(result), sort_keys=True, separators=(",", ":")),
            stamp,
            abandonment_id,
        ),
    )
    record_audit(
        conn,
        submission_id=None,
        task_gid=row["task_gid"],
        operation_id=row["source_operation_id"],
        event_type="operation.abandonment_blocked",
        actor_agent=None,
        details={"abandonment_id": abandonment_id, "reason": "awaiting_hold_resolution"},
        result_code="OK",
        result_ok=True,
    )
    return get_abandonment_attempt(conn, abandonment_id)



def _validate_abandonment_succession_spec(
    spec: _AbandonmentSuccessionSpec,
) -> None:
    if spec.successor_claim_mode not in {"stage_actor", "verifier"}:
        raise ValueError(
            "abandonment successor must require a stage actor or verifier claim"
        )
    if bool(spec.successor_cycle_id) != bool(
        spec.successor_cycle_number is not None
    ):
        raise ValueError("successor cycle id and number must be supplied together")
    if spec.successor_cycle_id is not None and not spec.successor_protocol_release:
        raise ValueError("successor Verification cycle requires a protocol release")


def _load_abandonment_succession_source(
    conn: sqlite3.Connection,
    spec: _AbandonmentSuccessionSpec,
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    abandonment = get_abandonment_attempt(conn, spec.abandonment_id)
    if abandonment["status"] not in {
        "started",
        "awaiting_hold_resolution",
        "blocked_manual_reconciliation",
    }:
        raise DishRuleError(
            "WRONG_STATE",
            "abandonment is not at a restartable local frontier",
            rule="abandonment_not_restartable",
            details={"status": abandonment["status"]},
        )
    source = assert_clean_abandonment_restart_source(
        conn, source_operation_id=abandonment["source_operation_id"]
    )
    source_version = conn.execute(
        "SELECT * FROM content_versions WHERE content_version_id=?",
        (spec.source_content_version_id,),
    ).fetchone()
    if source_version is None or (
        source_version["task_gid"] != abandonment["task_gid"]
        or source_version["confirmed"] != 1
    ):
        raise DishRuleError(
            "CONFLICT",
            "selected abandonment baseline is not exact confirmed task content",
            rule="abandonment_source_baseline_invalid",
        )
    if conn.execute(
        "SELECT 1 FROM operation_successions WHERE source_operation_id=?",
        (source["operation_id"],),
    ).fetchone() is not None:
        raise DishRuleError(
            "CONFLICT",
            "source operation already has a successor",
            rule="operation_already_superseded",
        )
    if conn.execute(
        "SELECT 1 FROM operations WHERE operation_id=?",
        (spec.successor_operation_id,),
    ).fetchone() is not None:
        raise DishRuleError(
            "CONFLICT",
            "successor operation identity already exists",
            rule="successor_operation_exists",
        )
    return abandonment, source, source_version


def _terminalize_abandonment_source(
    conn: sqlite3.Connection,
    *,
    spec: _AbandonmentSuccessionSpec,
    abandonment: Mapping[str, Any],
    source: Mapping[str, Any],
) -> None:
    cursor = conn.execute(
        """UPDATE operations
              SET status='cancelled', phase='terminal',
                  terminal_outcome='agent_abandoned', completed_at=?
            WHERE operation_id=? AND status IN ('open','uncertain')""",
        (spec.created_at, source["operation_id"]),
    )
    if cursor.rowcount != 1:
        raise DishRuleError(
            "CONFLICT",
            "source operation changed before abandonment terminalization",
            rule="abandonment_source_conflict",
        )
    if not spec.close_source_cycle_as_abandoned:
        return
    if (
        not spec.source_cycle_id
        or spec.source_cycle_id != abandonment["attempt_cycle_id"]
    ):
        raise DishRuleError(
            "CONFLICT",
            "source Verification cycle does not match the abandoned attempt",
            rule="abandonment_cycle_mismatch",
        )
    cursor = conn.execute(
        """UPDATE verification_cycles
              SET outcome='abandoned', completed_at=?
            WHERE cycle_id=? AND operation_id=?
              AND completed_at IS NULL AND outcome IS NULL""",
        (spec.created_at, spec.source_cycle_id, source["operation_id"]),
    )
    if cursor.rowcount != 1:
        raise DishRuleError(
            "CONFLICT",
            "only the exact incomplete Verification cycle can be abandoned",
            rule="abandonment_cycle_not_incomplete",
        )


def _insert_abandonment_successor(
    conn: sqlite3.Connection,
    *,
    spec: _AbandonmentSuccessionSpec,
    abandonment: Mapping[str, Any],
    source_version: Mapping[str, Any],
) -> None:
    conn.execute(
        """INSERT INTO operations(
               operation_id,task_gid,operation_kind,status,editor_agent,
               researcher_agent,verifier_agent,run_id,independence_attestation,
               expected_identity,schema_version,expected_section_gid,phase,
               successor_claim_mode,created_at
           ) VALUES(?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?)""",
        (
            spec.successor_operation_id,
            abandonment["task_gid"],
            spec.successor_operation_kind,
            spec.successor_editor_agent,
            spec.successor_researcher_agent,
            spec.successor_verifier_agent,
            spec.successor_run_id,
            spec.successor_independence_attestation,
            source_version["identity"],
            spec.successor_schema_version,
            spec.successor_expected_section_gid,
            spec.successor_phase,
            spec.successor_claim_mode,
            spec.created_at,
        ),
    )
    conn.execute(
        """INSERT INTO content_versions(
               content_version_id,task_gid,operation_id,boundary,identity,
               title,notes,confirmed,created_at
           ) VALUES(?,?,?,'successor_baseline',?,?,?,1,?)""",
        (
            spec.successor_content_version_id,
            abandonment["task_gid"],
            spec.successor_operation_id,
            source_version["identity"],
            source_version["title"],
            source_version["notes"],
            spec.created_at,
        ),
    )
    for step_name, intended in spec.successor_completed_steps.items():
        declare_operation_step(
            conn, spec.successor_operation_id, step_name, intended
        )
        complete_operation_step(conn, spec.successor_operation_id, step_name)
    for fact in spec.successor_actor_facts:
        record_actor_fact(
            conn,
            operation_id=spec.successor_operation_id,
            task_gid=abandonment["task_gid"],
            role=str(fact["role"]),
            agent=str(fact["agent"]),
            run_id=fact.get("run_id"),
            independence_attestation=fact.get("independence_attestation"),
            candidate_identity=fact.get("candidate_identity"),
            source_cycle_id=fact.get("source_cycle_id"),
        )


def _insert_abandonment_successor_cycle(
    conn: sqlite3.Connection,
    *,
    spec: _AbandonmentSuccessionSpec,
    abandonment: Mapping[str, Any],
) -> None:
    if spec.successor_cycle_id is None:
        return
    conn.execute(
        """INSERT INTO verification_cycles(
               cycle_id,operation_id,task_gid,cycle_number,protocol_release,
               protocol_text,created_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            spec.successor_cycle_id,
            spec.successor_operation_id,
            abandonment["task_gid"],
            spec.successor_cycle_number,
            spec.successor_protocol_release,
            spec.successor_protocol_text,
            spec.created_at,
        ),
    )
    record_audit(
        conn,
        submission_id=None,
        task_gid=abandonment["task_gid"],
        operation_id=spec.successor_operation_id,
        event_type="verification_cycle.created",
        actor_agent=None,
        details={
            "cycle_number": spec.successor_cycle_number,
            "protocol_release": spec.successor_protocol_release,
            "abandonment_id": spec.abandonment_id,
        },
        result_code="OK",
        result_ok=True,
    )


def _publish_abandonment_succession(
    conn: sqlite3.Connection,
    *,
    spec: _AbandonmentSuccessionSpec,
    abandonment: Mapping[str, Any],
    source: Mapping[str, Any],
) -> None:
    result_json = None if spec.result is None else json.dumps(
        dict(spec.result), sort_keys=True, separators=(",", ":")
    )
    conn.execute(
        """UPDATE abandonment_attempts
              SET status='awaiting_successor_claim', outcome='restart_prepared',
                  successor_operation_id=?, successor_cycle_id=?,
                  current_execution_id=NULL, latest_result_json=?, updated_at=?
            WHERE abandonment_id=?""",
        (
            spec.successor_operation_id,
            spec.successor_cycle_id,
            result_json,
            spec.created_at,
            spec.abandonment_id,
        ),
    )
    conn.execute(
        """INSERT INTO operation_successions(
               succession_id,task_gid,source_operation_id,successor_operation_id,
               transition_type,transition_reason,source_cycle_id,successor_cycle_id,
               source_content_version_id,successor_content_version_id,
               candidate_transfer_kind,abandonment_id,created_at
           ) VALUES(?,?,?,?,'agent_abandonment',?,?,?,?,?,?,?,?)""",
        (
            spec.succession_id,
            abandonment["task_gid"],
            source["operation_id"],
            spec.successor_operation_id,
            spec.transition_reason,
            spec.source_cycle_id,
            spec.successor_cycle_id,
            spec.source_content_version_id,
            spec.successor_content_version_id,
            spec.candidate_transfer_kind,
            spec.abandonment_id,
            spec.created_at,
        ),
    )
    _release_abandonment_source_lease_in_transaction(
        conn, abandonment=abandonment, released_at=spec.created_at
    )
    record_audit(
        conn,
        submission_id=None,
        task_gid=abandonment["task_gid"],
        operation_id=source["operation_id"],
        event_type="operation.succession_created",
        actor_agent=None,
        details={
            "abandonment_id": spec.abandonment_id,
            "succession_id": spec.succession_id,
            "successor_operation_id": spec.successor_operation_id,
            "successor_cycle_id": spec.successor_cycle_id,
            "source_content_version_id": spec.source_content_version_id,
            "successor_content_version_id": spec.successor_content_version_id,
            "candidate_transfer_kind": spec.candidate_transfer_kind,
        },
        result_code="OK",
        result_ok=True,
    )


def _abandonment_succession_rows(
    conn: sqlite3.Connection, spec: _AbandonmentSuccessionSpec
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    return (
        get_abandonment_attempt(conn, spec.abandonment_id),
        conn.execute(
            "SELECT * FROM operations WHERE operation_id=?",
            (spec.successor_operation_id,),
        ).fetchone(),
        conn.execute(
            "SELECT * FROM operation_successions WHERE succession_id=?",
            (spec.succession_id,),
        ).fetchone(),
    )


def apply_operation_abandonment_succession_in_transaction(
    conn: sqlite3.Connection,
    *,
    abandonment_id: str,
    succession_id: str,
    successor_operation_id: str,
    source_content_version_id: str,
    successor_content_version_id: str,
    successor_operation_kind: str,
    successor_phase: str,
    successor_expected_section_gid: str,
    successor_schema_version: str,
    successor_claim_mode: str,
    transition_reason: str,
    candidate_transfer_kind: str,
    source_cycle_id: str | None = None,
    close_source_cycle_as_abandoned: bool = False,
    successor_cycle_id: str | None = None,
    successor_cycle_number: int | None = None,
    successor_protocol_release: str | None = None,
    successor_protocol_text: str | None = None,
    successor_editor_agent: str | None = None,
    successor_researcher_agent: str | None = None,
    successor_verifier_agent: str | None = None,
    successor_run_id: str | None = None,
    successor_independence_attestation: str | None = None,
    successor_actor_facts: Sequence[Mapping[str, Any]] = (),
    successor_completed_steps: Mapping[str, Mapping[str, Any]] | None = None,
    result: Mapping[str, Any] | None = None,
    created_at: str | None = None,
) -> tuple[sqlite3.Row, sqlite3.Row, sqlite3.Row]:
    """Atomically terminalize one clean source and create its prepared successor."""
    _require_writer_transaction(conn, operation="operation abandonment succession")
    spec = _AbandonmentSuccessionSpec(
        abandonment_id=abandonment_id,
        succession_id=succession_id,
        successor_operation_id=successor_operation_id,
        source_content_version_id=source_content_version_id,
        successor_content_version_id=successor_content_version_id,
        successor_operation_kind=successor_operation_kind,
        successor_phase=successor_phase,
        successor_expected_section_gid=successor_expected_section_gid,
        successor_schema_version=successor_schema_version,
        successor_claim_mode=successor_claim_mode,
        transition_reason=transition_reason,
        candidate_transfer_kind=candidate_transfer_kind,
        source_cycle_id=source_cycle_id,
        close_source_cycle_as_abandoned=close_source_cycle_as_abandoned,
        successor_cycle_id=successor_cycle_id,
        successor_cycle_number=successor_cycle_number,
        successor_protocol_release=successor_protocol_release,
        successor_protocol_text=successor_protocol_text,
        successor_editor_agent=successor_editor_agent,
        successor_researcher_agent=successor_researcher_agent,
        successor_verifier_agent=successor_verifier_agent,
        successor_run_id=successor_run_id,
        successor_independence_attestation=successor_independence_attestation,
        successor_actor_facts=successor_actor_facts,
        successor_completed_steps=dict(successor_completed_steps or {}),
        result=result,
        created_at=created_at or utc_now(),
    )
    _validate_abandonment_succession_spec(spec)
    abandonment, source, source_version = _load_abandonment_succession_source(
        conn, spec
    )
    _terminalize_abandonment_source(
        conn, spec=spec, abandonment=abandonment, source=source
    )
    _insert_abandonment_successor(
        conn, spec=spec, abandonment=abandonment, source_version=source_version
    )
    _insert_abandonment_successor_cycle(
        conn, spec=spec, abandonment=abandonment
    )
    _publish_abandonment_succession(
        conn, spec=spec, abandonment=abandonment, source=source
    )
    return _abandonment_succession_rows(conn, spec)



def claim_prepared_stage_successor_in_transaction(
    conn: sqlite3.Connection,
    *,
    prepared_operation_id: str,
    task_gid: str,
    operation_kind: str,
    agent: str,
    run_id: str,
    live_identity: str,
    live_section_gid: str,
    schema_version: str,
    expected_change_intent: Mapping[str, Any] | None,
    result: Mapping[str, Any],
    claimed_at: str | None = None,
) -> sqlite3.Row:
    """Claim one exact Planning/Research successor inside the caller transaction."""

    _require_writer_transaction(conn, operation="prepared stage successor claim")
    row = conn.execute(
        """SELECT successor.*, succession.abandonment_id,
                  abandonment.abandoned_run_id, abandonment.status AS abandonment_status
             FROM operations AS successor
             JOIN operation_successions AS succession
               ON succession.successor_operation_id=successor.operation_id
             JOIN abandonment_attempts AS abandonment
               ON abandonment.abandonment_id=succession.abandonment_id
            WHERE successor.operation_id=?""",
        (prepared_operation_id,),
    ).fetchone()
    if row is None:
        raise DishRuleError(
            "NOT_FOUND",
            "prepared successor not found",
            rule="prepared_successor_not_found",
            details={"prepared_operation_id": prepared_operation_id},
        )
    if (
        row["task_gid"] != task_gid
        or row["operation_kind"] != operation_kind
        or row["status"] != "open"
        or row["phase"] != "prepare_required"
        or row["successor_claim_mode"] != "stage_actor"
        or row["abandonment_status"] != "awaiting_successor_claim"
    ):
        raise DishRuleError(
            "WRONG_STATE",
            "prepared successor no longer matches the requested stage action",
            rule="prepared_successor_mismatch",
            details={"prepared_operation_id": prepared_operation_id},
        )
    clean_run = str(run_id or "").strip()
    if not clean_run:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "a connected run ID is required to claim a prepared successor",
            rule="service_run_required",
        )
    if clean_run == str(row["abandoned_run_id"] or "").strip():
        raise DishRuleError(
            "AGENT_MISMATCH",
            "the abandoned run cannot claim its replacement attempt",
            rule="abandoned_run_claim_forbidden",
        )
    if row["expected_identity"] != live_identity or row["expected_section_gid"] != live_section_gid:
        drift = {
            "expected_identity": row["expected_identity"],
            "actual_identity": live_identity,
            "expected_section_gid": row["expected_section_gid"],
            "actual_section_gid": live_section_gid,
        }
        command = f'dish-admin reconcile-abandonment {row["abandonment_id"]}'
        blocked_result = {
            "abandonment_id": row["abandonment_id"],
            "classification": {
                "outcome": "blocked_manual_reconciliation",
                "stage": "planning" if operation_kind == "planning" else "research",
                "reason": "prepared successor baseline or placement drifted before claim",
                "details": drift,
            },
            "required_action": {
                "surface": "private-admin",
                "command": "reconcile-abandonment",
                "arguments": {"abandonment_id": row["abandonment_id"]},
                "admin_command": command,
                "relay_text": (
                    f"Tell the human to run: {command}\n"
                    "Then wait for confirmation it succeeded and refresh the "
                    "authoritative Dish action."
                ),
                "after_success": {
                    "start_new_operation": False,
                    "instruction": (
                        "Refresh the authoritative Dish action, then follow the "
                        "exact continuation returned."
                    ),
                },
            },
        }
        mark_abandonment_blocked_in_transaction(
            conn,
            abandonment_id=row["abandonment_id"],
            result=blocked_result,
        )
        raise DishRuleError(
            "CONFLICT",
            "prepared successor baseline or placement changed",
            rule="prepared_successor_drift",
            details={
                **drift,
                "abandonment_id": row["abandonment_id"],
                "required_admin_action": "reconcile-abandonment",
                "admin_command": command,
            },
        )
    prior_schema_version = row["schema_version"]
    if operation_kind == "change":
        intent = conn.execute(
            """SELECT intended_json, completed_at FROM operation_steps
                 WHERE operation_id=? AND step_name='change_intent'""",
            (prepared_operation_id,),
        ).fetchone()
        try:
            recorded_intent = None if intent is None else json.loads(intent["intended_json"])
        except (TypeError, ValueError):
            recorded_intent = None
        if (
            intent is None
            or intent["completed_at"] is None
            or recorded_intent != dict(expected_change_intent or {})
        ):
            raise DishRuleError(
                "CONFLICT",
                "prepared Change successor intent does not match the request",
                rule="prepared_successor_change_intent_mismatch",
            )
    elif expected_change_intent:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "change intent is valid only for a Change successor",
            rule="change_arguments_forbidden",
        )
    if conn.execute(
        "SELECT 1 FROM service_leases WHERE operation_id=? AND released_at IS NULL",
        (prepared_operation_id,),
    ).fetchone() is not None:
        raise DishRuleError(
            "CONFLICT",
            "prepared successor is already leased",
            rule="prepared_successor_claimed",
        )

    if operation_kind == "planning":
        cursor = conn.execute(
            """UPDATE operations
                  SET editor_agent=?, run_id=?, schema_version=?, successor_claim_mode='none'
                WHERE operation_id=? AND editor_agent IS NULL AND run_id IS NULL
                  AND successor_claim_mode='stage_actor'""",
            (agent, clean_run, schema_version, prepared_operation_id),
        )
        role = "planner"
    elif operation_kind == "initial":
        cursor = conn.execute(
            """UPDATE operations
                  SET researcher_agent=?, run_id=?, schema_version=?, successor_claim_mode='none'
                WHERE operation_id=? AND researcher_agent IS NULL AND run_id IS NULL
                  AND successor_claim_mode='stage_actor'""",
            (agent, clean_run, schema_version, prepared_operation_id),
        )
        role = "constructor"
    elif operation_kind == "change":
        cursor = conn.execute(
            """UPDATE operations
                  SET editor_agent=?, run_id=?, schema_version=?, successor_claim_mode='none'
                WHERE operation_id=? AND editor_agent IS NULL AND run_id IS NULL
                  AND successor_claim_mode='stage_actor'""",
            (agent, clean_run, schema_version, prepared_operation_id),
        )
        role = "material_editor"
    else:
        raise DishRuleError(
            "INVALID_ARGUMENT",
            "prepared stage successor must be Planning or Research",
            rule="prepared_successor_kind_invalid",
        )
    if cursor.rowcount != 1:
        raise DishRuleError(
            "CONFLICT",
            "prepared successor was claimed concurrently",
            rule="prepared_successor_claimed",
        )
    record_actor_fact(
        conn,
        operation_id=prepared_operation_id,
        task_gid=task_gid,
        role=role,
        agent=agent,
        run_id=clean_run,
        candidate_identity=None,
    )
    stamp = claimed_at or utc_now()
    claimed = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?",
        (prepared_operation_id,),
    ).fetchone()
    complete_abandonment_in_transaction(
        conn,
        abandonment_id=row["abandonment_id"],
        outcome="restarted",
        result=result,
        continuation_operation_id=prepared_operation_id,
        completed_at=stamp,
    )
    record_audit(
        conn,
        submission_id=None,
        task_gid=task_gid,
        operation_id=prepared_operation_id,
        event_type="operation.successor_claimed",
        actor_agent=agent,
        actor_run_id=clean_run,
        details={
            "abandonment_id": row["abandonment_id"],
            "operation_kind": operation_kind,
            "previous_schema_version": prior_schema_version,
            "claimed_schema_version": schema_version,
        },
        result_code="OK",
        result_ok=True,
    )
    return claimed


def complete_abandonment_in_transaction(
    conn: sqlite3.Connection,
    *,
    abandonment_id: str,
    outcome: str,
    result: Mapping[str, Any],
    continuation_operation_id: str | None = None,
    continuation_cycle_id: str | None = None,
    completed_at: str | None = None,
) -> sqlite3.Row:
    _require_writer_transaction(conn, operation="abandonment completion")
    if outcome not in {"restarted", "committed_finalized", "route_preserved"}:
        raise ValueError("unsupported completed abandonment outcome")
    abandonment = get_abandonment_attempt(conn, abandonment_id)
    stamp = completed_at or utc_now()
    conn.execute(
        """UPDATE abandonment_attempts
              SET status='completed', outcome=?, continuation_operation_id=?,
                  continuation_cycle_id=?, current_execution_id=NULL,
                  latest_result_json=?, updated_at=?, completed_at=?
            WHERE abandonment_id=?""",
        (
            outcome,
            continuation_operation_id,
            continuation_cycle_id,
            json.dumps(dict(result), sort_keys=True, separators=(",", ":")),
            stamp,
            stamp,
            abandonment_id,
        ),
    )
    _release_abandonment_source_lease_in_transaction(
        conn, abandonment=abandonment, released_at=stamp
    )
    record_audit(
        conn,
        submission_id=None,
        task_gid=abandonment["task_gid"],
        operation_id=abandonment["source_operation_id"],
        event_type="operation.abandonment_completed",
        actor_agent=None,
        details={"abandonment_id": abandonment_id, "outcome": outcome},
        result_code="OK",
        result_ok=True,
    )
    return get_abandonment_attempt(conn, abandonment_id)

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
    with immediate_persistence(conn, "planning_reopen_attempt"):
        active = conn.execute(
            """SELECT operation_id FROM operations
                 WHERE task_gid=? AND status IN ('open','uncertain')
                 LIMIT 1""",
            (task_gid,),
        ).fetchone()
        if active is not None:
            raise DishRuleError(
                "CONFLICT",
                "task already has an active operation",
                rule="active_operation_exists",
                details={"operation_id": active["operation_id"]},
            )
        conn.execute(
            """INSERT INTO planning_reopen_attempts(
                   attempt_id,task_gid,request_id,expected_identity,expected_section_gid,
                   expected_modified_at,reason,actor_run_id,outcome,created_at
               ) VALUES (?,?,?,?,?,?,?,?, 'started', ?)""",
            (
                attempt_id, task_gid, request_id, expected_identity,
                expected_section_gid, expected_modified_at, reason,
                actor_run_id, utc_now(),
            ),
        )
        return conn.execute(
            "SELECT * FROM planning_reopen_attempts WHERE attempt_id=?",
            (attempt_id,),
        ).fetchone()


def planning_reopen_attempt_by_request(
    conn: sqlite3.Connection, *, request_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM planning_reopen_attempts WHERE request_id=?", (request_id,)
    ).fetchone()


def planning_reopen_blocker_for_task(
    conn: sqlite3.Connection, *, task_gid: str
) -> sqlite3.Row | None:
    """Return reopen evidence that must converge before a Planning start."""
    return conn.execute(
        """SELECT attempt.*, request.status AS request_status
             FROM planning_reopen_attempts AS attempt
             LEFT JOIN service_requests AS request
               ON request.request_id=attempt.request_id
            WHERE attempt.task_gid=?
              AND (attempt.outcome IN ('started','uncertain')
                   OR request.status='pending')
            ORDER BY attempt.created_at DESC, attempt.rowid DESC
            LIMIT 1""",
        (task_gid,),
    ).fetchone()


def unresolved_planning_reopen_attempts(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """Return attempts or request journals requiring startup reconciliation."""
    return conn.execute(
        """SELECT attempt.*, request.status AS request_status
             FROM planning_reopen_attempts AS attempt
             LEFT JOIN service_requests AS request
               ON request.request_id=attempt.request_id
            WHERE attempt.outcome IN ('started','uncertain')
               OR request.status='pending'
            ORDER BY attempt.created_at, attempt.rowid"""
    ).fetchall()


def finish_planning_reopen_attempt(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    outcome: str,
    confirmed_modified_at: str | None = None,
) -> sqlite3.Row:
    if outcome not in {"confirmed", "not_applied", "uncertain"}:
        raise ValueError(f"invalid planning reopen outcome: {outcome}")
    current = conn.execute(
        "SELECT * FROM planning_reopen_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if current is None:
        raise DishRuleError(
            "NOT_FOUND", "planning reopen attempt was not found",
            rule="planning_reopen_attempt_not_found",
        )
    if current["outcome"] == outcome:
        return current
    if outcome == "uncertain":
        allowed = ("started",)
    elif outcome == "confirmed":
        allowed = ("started", "uncertain")
    else:
        allowed = ("started",)
    placeholders = ",".join("?" for _ in allowed)
    conn.execute(
        f"""UPDATE planning_reopen_attempts
               SET outcome=?, finished_at=?, confirmed_modified_at=?
             WHERE attempt_id=? AND outcome IN ({placeholders})""",
        (outcome, utc_now(), confirmed_modified_at, attempt_id, *allowed),
    )
    row = conn.execute(
        "SELECT * FROM planning_reopen_attempts WHERE attempt_id=?", (attempt_id,)
    ).fetchone()
    if row is None or row["outcome"] != outcome:
        raise DishRuleError(
            "CONFLICT", "planning reopen attempt is not recoverable",
            rule="planning_reopen_attempt_not_pending",
            details={"attempt_id": attempt_id, "outcome": row["outcome"] if row else None},
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


def _authorization_grant_audit_exists(
    conn: sqlite3.Connection, authorization_id: str
) -> bool:
    return conn.execute(
        """SELECT 1 FROM audit_events
             WHERE event_type='marco.authorization'
               AND json_extract(details, '$.authorization_id')=?
             LIMIT 1""",
        (authorization_id,),
    ).fetchone() is not None


def record_marco_authorization(conn: sqlite3.Connection, *, task_gid: str, operation_id: str | None, field_name: str, before: Any, after: Any, reason: str, actor_run_id: str | None = None) -> sqlite3.Row:
    """Create one audited authorization capability as one SQLite decision.

    The operation lifecycle check, exact semantic deduplication, capability row,
    and authoritative grant audit share one ``BEGIN IMMEDIATE`` transaction.
    An unaudited historical row is never silently reused or duplicated.
    """
    authorization_id = str(uuid.uuid4())
    clean_reason = str(reason or "").strip()
    if not clean_reason:
        raise DishRuleError("INVALID_ARGUMENT", "authorization reason is required", rule="authorization_reason_required")
    before_json = json.dumps(before, sort_keys=True)
    after_json = json.dumps(after, sort_keys=True)
    clean_run_id = str(actor_run_id or "").strip() or None
    with immediate_persistence(conn, "record_marco_authorization"):
        if operation_id is not None:
            operation = conn.execute(
                "SELECT task_gid,status FROM operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            if operation is None:
                raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
            if operation["task_gid"] != task_gid:
                raise DishRuleError(
                    "CONFLICT",
                    "authorization task does not match the bound operation",
                    rule="authorization_operation_task_mismatch",
                )
            if operation["status"] != "open":
                raise DishRuleError(
                    "WRONG_STATE",
                    "governed changes may only be authorized for an open operation",
                    rule="authorization_operation_not_open",
                    details={"actual": operation["status"]},
                )
        existing_rows = conn.execute(
            """SELECT * FROM marco_authorizations
                 WHERE task_gid=? AND operation_id IS ?
                   AND field_name=? AND before_json=? AND after_json=?
                   AND reason=? AND actor_run_id IS ?
                   AND consumed_at IS NULL
                 ORDER BY created_at, authorization_id""",
            (
                task_gid, operation_id, field_name, before_json, after_json,
                clean_reason, clean_run_id,
            ),
        ).fetchall()
        if len(existing_rows) > 1:
            raise DishRuleError(
                "CONFLICT",
                "historical duplicate unused authorizations require operator review",
                rule="governed_authorization_history_ambiguous",
                retryable=False,
                details={"field": field_name, "authorization_count": len(existing_rows)},
            )
        if existing_rows:
            existing = existing_rows[0]
            if not _authorization_grant_audit_exists(conn, existing["authorization_id"]):
                raise DishRuleError(
                    "CONFLICT",
                    "an existing authorization lacks its authoritative grant audit",
                    rule="governed_authorization_grant_audit_missing",
                    retryable=False,
                    details={"authorization_id": existing["authorization_id"]},
                )
            return existing
        conn.execute(
            """INSERT INTO marco_authorizations(authorization_id,task_gid,operation_id,field_name,before_json,after_json,reason,actor_run_id,created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (authorization_id, task_gid, operation_id, field_name, before_json, after_json, clean_reason, clean_run_id, utc_now()),
        )
        record_audit(conn, submission_id=None, task_gid=task_gid, operation_id=operation_id, event_type="marco.authorization", actor_agent=None,
                     details={"authorization_id": authorization_id, "field": field_name, "reason": clean_reason}, result_code="OK", result_ok=True,
                     governed_kind="decision", before_state={field_name: before}, after_state={field_name: after}, actor_run_id=actor_run_id, actor_source="marco-admin")
        row = conn.execute(
            "SELECT * FROM marco_authorizations WHERE authorization_id=?",
            (authorization_id,),
        ).fetchone()
        return row

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
    with immediate_persistence(conn, "reserve_marco_authorizations"):
        operation = conn.execute(
            "SELECT task_gid,status FROM operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if operation is None:
            raise DishRuleError("NOT_FOUND", "operation not found", rule="operation_not_found")
        if operation["task_gid"] != task_gid:
            raise DishRuleError(
                "CONFLICT",
                "authorization task does not match the reserving operation",
                rule="authorization_operation_task_mismatch",
            )
        if operation["status"] != "open":
            raise DishRuleError(
                "WRONG_STATE",
                "governed authorization cannot be reserved for a terminal operation",
                rule="authorization_operation_not_open",
                details={"actual": operation["status"]},
            )
        rows: list[sqlite3.Row] = []
        for change in changes:
            before_json = json.dumps(change["before"], sort_keys=True)
            after_json = json.dumps(change["after"], sort_keys=True)
            candidates = conn.execute(
                """SELECT authorization.*
                     FROM marco_authorizations AS authorization
                    WHERE authorization.task_gid=?
                      AND (authorization.operation_id IS NULL OR authorization.operation_id=?)
                      AND authorization.field_name=?
                      AND authorization.before_json=?
                      AND authorization.after_json=?
                      AND authorization.consumed_at IS NULL
                      AND (authorization.reserved_by_operation_id IS NULL
                           OR authorization.reserved_by_operation_id=?)
                      AND EXISTS (
                          SELECT 1 FROM audit_events AS grant
                           WHERE grant.event_type='marco.authorization'
                             AND json_extract(grant.details, '$.authorization_id')=authorization.authorization_id
                      )
                    ORDER BY authorization.created_at, authorization.authorization_id""",
                (task_gid, operation_id, change["field"], before_json, after_json, operation_id),
            ).fetchall()
            if len(candidates) > 1:
                raise DishRuleError(
                    "CONFLICT",
                    "multiple equivalent unused authorizations require operator review",
                    rule="governed_authorization_history_ambiguous",
                    retryable=False,
                    details={"field": change["field"], "authorization_count": len(candidates)},
                )
            row = candidates[0] if candidates else None
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
        return reserved


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
    """Claim and import emergency JSONL repairs without deleting concurrent appends."""
    imported = 0
    remaining: list[str] = []
    with locked_audit_repair_sidecar(conn) as paths:
        if paths is None:
            return 0

        while paths.claim.exists() or paths.main.exists():
            if not paths.claim.exists():
                paths.main.replace(paths.claim)
                fsync_parent(paths.main)

            lines = paths.claim.read_text(encoding="utf-8").splitlines()
            for line in lines:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                    cursor = conn.execute(
                        """INSERT OR IGNORE INTO command_audit_repairs(
                               repair_id,command,operation_id,submission_id,task_gid,actor_agent,
                               result_json,audit_error,created_at
                           ) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (item["repair_id"], item["command"], item.get("operation_id"),
                         item.get("submission_id"), item.get("task_gid"), item.get("actor_agent"),
                         json.dumps(item["result"], sort_keys=True, separators=(",", ":")),
                         item.get("audit_error", "emergency audit repair"), utc_now()),
                    )
                    imported += int(cursor.rowcount > 0)
                except Exception:
                    remaining.append(line)
            paths.claim.unlink()
            fsync_parent(paths.claim)

        if remaining:
            with paths.main.open("a", encoding="utf-8") as handle:
                handle.write("\n".join(remaining) + "\n")
                handle.flush()
                import os
                os.fsync(handle.fileno())
            fsync_parent(paths.main)
    return imported


def process_command_audit_repairs(conn: sqlite3.Connection, *, limit: int = 100) -> int:
    """Import and replay pending invocation-audit repairs exactly once."""
    _import_command_audit_repair_fallback(conn)
    repaired = 0
    with immediate_persistence(conn, "process_command_audit_repairs"):
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
            try:
                with atomic_persistence(conn, "audit_repair_row"):
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
                repaired += 1
            except Exception:
                break
        return repaired
