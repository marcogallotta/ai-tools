"""SQLite schema, audit, and state transitions."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping

from .constants import DEFAULT_DB_PATH, SUBMISSION_STATES
from .errors import DishRuleError
from .models import agent_family, utc_now

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
MIGRATIONS = {1: _MIGRATION_1}


def initialize_database(
    path: str | os.PathLike[str] = DEFAULT_DB_PATH,
) -> sqlite3.Connection:
    db_path = Path(path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
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
) -> str:
    if actor_agent is not None:
        agent_family(actor_agent)
    event_id = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO audit_events (
            event_id, submission_id, task_gid, event_type,
            actor_agent, details, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            submission_id,
            task_gid,
            event_type,
            actor_agent,
            json.dumps(dict(details), sort_keys=True, separators=(",", ":")),
            created_at or utc_now(),
        ),
    )
    return event_id


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
                    baseline_exemption_tags, editor_agent, editor_family,
                    change_level, change_reason, baseline_verification_line,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'drafting', ?)
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
    "notes_written_at",
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
