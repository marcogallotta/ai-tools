"""SQLite schema, audit, and state transitions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from .constants import DEFAULT_DB_PATH, SUBMISSION_STATES
from .errors import DishRuleError
from .models import ContentIdentity, OperationActors, agent_family, utc_now

_MIGRATION_1 = f"""
CREATE TABLE submissions (
    submission_id TEXT PRIMARY KEY,
    task_gid TEXT NOT NULL,
    submission_kind TEXT NOT NULL CHECK (submission_kind IN ('planning','initial','change')),
    protocol_release TEXT NOT NULL,
    release_commit TEXT NOT NULL,
    protocol_bundle TEXT NOT NULL CHECK (json_valid(protocol_bundle)),
    canonical_manifest TEXT NOT NULL CHECK (json_valid(canonical_manifest)),
    baseline_exemption_tags TEXT CHECK (baseline_exemption_tags IS NULL OR json_valid(baseline_exemption_tags)),
    prepared_exemption_tags TEXT CHECK (prepared_exemption_tags IS NULL OR json_valid(prepared_exemption_tags)),
    destination_section_name TEXT,
    destination_section_gid TEXT,
    exemption_revision TEXT,
    editor_agent TEXT NOT NULL CHECK (editor_agent IN ('claude','gpt','codex')),
    editor_family TEXT NOT NULL CHECK (editor_family IN ('claude','gpt')),
    change_level TEXT CHECK (change_level IS NULL OR change_level IN ('small','large')),
    change_reason TEXT,
    failed_verification_passes INTEGER NOT NULL DEFAULT 0 CHECK (failed_verification_passes >= 0),
    baseline_verification_line TEXT,
    required_verifier_family TEXT CHECK (required_verifier_family IS NULL OR required_verifier_family IN ('claude','gpt')),
    verifier_agent TEXT CHECK (verifier_agent IS NULL OR verifier_agent IN ('claude','gpt','codex')),
    verifier_family TEXT CHECK (verifier_family IS NULL OR verifier_family IN ('claude','gpt')),
    status TEXT NOT NULL CHECK (status IN ({",".join(repr(state) for state in sorted(SUBMISSION_STATES))})),
    write_attempt_id TEXT UNIQUE,
    in_flight_at TEXT,
    in_flight_hostname TEXT,
    in_flight_pid INTEGER,
    in_flight_process_start TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    completed_at TEXT,
    research_queue_moved_at TEXT,
    notes_written_at TEXT,
    destination_moved_at TEXT
);
CREATE UNIQUE INDEX submissions_one_open_per_task
    ON submissions(task_gid)
    WHERE status NOT IN ('consumed', 'discarded');
CREATE INDEX submissions_status_idx ON submissions(status);

CREATE TABLE audit_events (
    event_id TEXT PRIMARY KEY,
    submission_id TEXT REFERENCES submissions(submission_id),
    task_gid TEXT,
    event_type TEXT NOT NULL,
    actor_agent TEXT CHECK (actor_agent IS NULL OR actor_agent IN ('claude','gpt','codex')),
    details TEXT NOT NULL CHECK (json_valid(details)),
    created_at TEXT NOT NULL
);
CREATE INDEX audit_events_submission_idx ON audit_events(submission_id, created_at);
CREATE INDEX audit_events_task_idx ON audit_events(task_gid, created_at);
CREATE INDEX audit_events_type_idx ON audit_events(event_type, created_at);
"""
_MIGRATION_2 = """
ALTER TABLE submissions ADD COLUMN baseline_title TEXT;
ALTER TABLE submissions ADD COLUMN baseline_title_fields TEXT
    CHECK (baseline_title_fields IS NULL OR json_valid(baseline_title_fields));
ALTER TABLE submissions ADD COLUMN prepared_title TEXT;
ALTER TABLE submissions ADD COLUMN prepared_title_fields TEXT
    CHECK (prepared_title_fields IS NULL OR json_valid(prepared_title_fields));
ALTER TABLE submissions ADD COLUMN task_content_written_at TEXT;
UPDATE submissions
   SET task_content_written_at = notes_written_at
 WHERE task_content_written_at IS NULL
   AND notes_written_at IS NOT NULL;
"""
_MIGRATION_3 = """
CREATE TABLE task_content_state (
    task_gid TEXT PRIMARY KEY,
    last_confirmed_identity TEXT NOT NULL,
    last_confirmed_title TEXT NOT NULL,
    last_confirmed_notes TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    confirmed_at TEXT NOT NULL
);

CREATE TABLE operations (
    operation_id TEXT PRIMARY KEY,
    task_gid TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open','completed','cancelled','uncertain')),
    editor_agent TEXT,
    researcher_agent TEXT,
    verifier_agent TEXT,
    run_id TEXT,
    independence_attestation TEXT,
    expected_identity TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    content_write_completed_at TEXT,
    signoff_completed_at TEXT,
    movement_completed_at TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE UNIQUE INDEX operations_one_open_per_task
    ON operations(task_gid) WHERE status = 'open';
CREATE INDEX operations_task_idx ON operations(task_gid, created_at);

CREATE TABLE content_versions (
    content_version_id TEXT PRIMARY KEY,
    task_gid TEXT NOT NULL,
    operation_id TEXT REFERENCES operations(operation_id),
    boundary TEXT NOT NULL,
    identity TEXT NOT NULL,
    title TEXT NOT NULL,
    notes TEXT NOT NULL,
    confirmed INTEGER NOT NULL CHECK (confirmed IN (0,1)),
    created_at TEXT NOT NULL
);
CREATE INDEX content_versions_task_idx ON content_versions(task_gid, created_at);
CREATE INDEX content_versions_operation_idx ON content_versions(operation_id, created_at);

CREATE TABLE verification_cycles (
    cycle_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    task_gid TEXT NOT NULL,
    cycle_number INTEGER NOT NULL CHECK (cycle_number > 0),
    protocol_release TEXT NOT NULL,
    verifier_agent TEXT,
    run_id TEXT,
    independence_attestation TEXT,
    correction_class TEXT,
    outcome TEXT,
    route TEXT CHECK (route IS NULL OR route IN ('evidence','human_review')),
    resume_state TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(task_gid, cycle_number)
);

CREATE TABLE write_attempts (
    attempt_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    expected_identity TEXT NOT NULL,
    intended_identity TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('started','confirmed','not_applied','uncertain')),
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE movement_attempts (
    attempt_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    expected_section_gid TEXT,
    intended_section_gid TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('started','confirmed','not_applied','uncertain')),
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE legacy_submission_quarantine (
    quarantine_id TEXT PRIMARY KEY,
    submission_id TEXT NOT NULL,
    task_gid TEXT NOT NULL,
    legacy_status TEXT NOT NULL,
    row_json TEXT NOT NULL CHECK (json_valid(row_json)),
    quarantined_at TEXT NOT NULL
);
CREATE INDEX legacy_submission_quarantine_task_idx
    ON legacy_submission_quarantine(task_gid, quarantined_at);

ALTER TABLE audit_events ADD COLUMN operation_id TEXT REFERENCES operations(operation_id);
ALTER TABLE audit_events ADD COLUMN result_code TEXT;
ALTER TABLE audit_events ADD COLUMN result_ok INTEGER CHECK (result_ok IS NULL OR result_ok IN (0,1));

INSERT INTO legacy_submission_quarantine (
    quarantine_id, submission_id, task_gid, legacy_status, row_json, quarantined_at
)
SELECT lower(hex(randomblob(16))), submission_id, task_gid, status,
       json_object(
           'submission_id', submission_id,
           'task_gid', task_gid,
           'submission_kind', submission_kind,
           'protocol_release', protocol_release,
           'release_commit', release_commit,
           'status', status,
           'editor_agent', editor_agent,
           'verifier_agent', verifier_agent,
           'created_at', created_at
       ),
       strftime('%Y-%m-%dT%H:%M:%fZ','now')
  FROM submissions
 WHERE status NOT IN ('consumed','discarded');

UPDATE submissions
   SET status = 'discarded', completed_at = COALESCE(completed_at, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
 WHERE status NOT IN ('consumed','discarded');
"""
_MIGRATION_4 = """
ALTER TABLE verification_cycles ADD COLUMN protocol_text TEXT;
ALTER TABLE verification_cycles ADD COLUMN reviewed_content_version_id TEXT REFERENCES content_versions(content_version_id);
ALTER TABLE verification_cycles ADD COLUMN reviewed_identity TEXT;
ALTER TABLE verification_cycles ADD COLUMN signed_content_version_id TEXT REFERENCES content_versions(content_version_id);
ALTER TABLE verification_cycles ADD COLUMN signed_identity TEXT;
CREATE INDEX verification_cycles_reviewed_version_idx ON verification_cycles(reviewed_content_version_id);
CREATE INDEX verification_cycles_signed_version_idx ON verification_cycles(signed_content_version_id);
"""
_MIGRATION_5 = """
ALTER TABLE write_attempts ADD COLUMN purpose TEXT NOT NULL DEFAULT 'content_write';
ALTER TABLE write_attempts ADD COLUMN intended_title TEXT;
ALTER TABLE write_attempts ADD COLUMN intended_notes TEXT;
ALTER TABLE write_attempts ADD COLUMN schema_version TEXT;
ALTER TABLE write_attempts ADD COLUMN context_json TEXT CHECK (context_json IS NULL OR json_valid(context_json));
ALTER TABLE write_attempts ADD COLUMN confirmed_content_version_id TEXT REFERENCES content_versions(content_version_id);
ALTER TABLE movement_attempts ADD COLUMN purpose TEXT NOT NULL DEFAULT 'unspecified';
ALTER TABLE movement_attempts ADD COLUMN confirmed_section_gid TEXT;
ALTER TABLE operations ADD COLUMN destination_movement_attempt_id TEXT REFERENCES movement_attempts(attempt_id);
CREATE INDEX write_attempts_open_idx ON write_attempts(operation_id, outcome, started_at);
CREATE INDEX movement_attempts_open_idx ON movement_attempts(operation_id, outcome, started_at);
"""
MIGRATIONS = {1: _MIGRATION_1, 2: _MIGRATION_2, 3: _MIGRATION_3, 4: _MIGRATION_4, 5: _MIGRATION_5}


def _backup_legacy_database(db_path: Path) -> None:
    """Keep one byte-for-byte backup before the persistence redesign."""

    if not db_path.exists() or str(db_path) == ":memory:":
        return
    backup = db_path.with_suffix(db_path.suffix + ".legacy-v2.bak")
    if backup.exists():
        return
    probe = sqlite3.connect(str(db_path))
    try:
        version = int(probe.execute("PRAGMA user_version").fetchone()[0])
    finally:
        probe.close()
    if version < 3:
        shutil.copy2(db_path, backup)


def initialize_database(
    path: str | os.PathLike[str] = DEFAULT_DB_PATH,
) -> sqlite3.Connection:
    db_path = Path(path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _backup_legacy_database(db_path)
    conn = sqlite3.connect(str(db_path), timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    migrate_database(conn)
    return conn


def migrate_database(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    for version in sorted(MIGRATIONS):
        if version in applied:
            continue
        script = MIGRATIONS[version]
        applied_at = utc_now().replace("'", "''")
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n"
                + script
                + f"\nINSERT INTO schema_migrations(version, applied_at) VALUES ({version}, '{applied_at}');\n"
                + f"PRAGMA user_version = {version};\n"
                + "COMMIT;\n"
            )
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise


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
) -> str:
    if actor_agent is not None:
        agent_family(actor_agent)
    event_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO audit_events (
            event_id, submission_id, task_gid, event_type,
            actor_agent, details, created_at, operation_id, result_code, result_ok
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            submission_id,
            task_gid,
            event_type,
            actor_agent,
            json.dumps(dict(details), sort_keys=True, separators=(",", ":")),
            created_at or utc_now(),
            operation_id,
            result_code,
            None if result_ok is None else int(result_ok),
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
                    expected_identity, schema_version, created_at
                ) VALUES (?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?)
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
            result_code="OK", result_ok=True,
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
