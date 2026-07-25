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

_MIGRATION_6 = """
ALTER TABLE audit_events ADD COLUMN governed_kind TEXT
    CHECK (governed_kind IS NULL OR governed_kind IN ('lock','exemption','decision'));
ALTER TABLE audit_events ADD COLUMN before_state TEXT
    CHECK (before_state IS NULL OR json_valid(before_state));
ALTER TABLE audit_events ADD COLUMN after_state TEXT
    CHECK (after_state IS NULL OR json_valid(after_state));
ALTER TABLE audit_events ADD COLUMN actor_provenance TEXT
    CHECK (actor_provenance IS NULL OR json_valid(actor_provenance));

CREATE TRIGGER verification_cycles_signed_pair_insert
BEFORE INSERT ON verification_cycles
WHEN (NEW.signed_content_version_id IS NULL) != (NEW.signed_identity IS NULL)
BEGIN SELECT RAISE(ABORT, 'verification signed identity/version must be paired'); END;
CREATE TRIGGER verification_cycles_signed_pair_update
BEFORE UPDATE OF signed_content_version_id, signed_identity ON verification_cycles
WHEN (NEW.signed_content_version_id IS NULL) != (NEW.signed_identity IS NULL)
BEGIN SELECT RAISE(ABORT, 'verification signed identity/version must be paired'); END;
CREATE TRIGGER verification_cycles_reviewed_pair_update
BEFORE UPDATE OF reviewed_content_version_id, reviewed_identity ON verification_cycles
WHEN (NEW.reviewed_content_version_id IS NULL) != (NEW.reviewed_identity IS NULL)
BEGIN SELECT RAISE(ABORT, 'verification reviewed identity/version must be paired'); END;
CREATE TRIGGER verification_cycles_approved_complete_update
BEFORE UPDATE OF outcome, completed_at, signed_content_version_id, signed_identity ON verification_cycles
WHEN NEW.outcome = 'approved' AND (NEW.completed_at IS NULL OR NEW.signed_content_version_id IS NULL OR NEW.signed_identity IS NULL)
BEGIN SELECT RAISE(ABORT, 'approved verification requires completed signed content'); END;
CREATE TRIGGER verification_cycles_route_resume_update
BEFORE UPDATE OF route, resume_state, outcome ON verification_cycles
WHEN (NEW.route IS NULL AND COALESCE(NEW.resume_state, 'None') != 'None' AND COALESCE(NEW.outcome, '') != 'two-pass-hold')
   OR (NEW.route IS NOT NULL AND COALESCE(NEW.resume_state, 'None') = 'None')
BEGIN SELECT RAISE(ABORT, 'verification route and resume state must be paired'); END;
CREATE TRIGGER operations_signoff_requires_write_update
BEFORE UPDATE OF signoff_completed_at ON operations
WHEN NEW.signoff_completed_at IS NOT NULL AND NEW.content_write_completed_at IS NULL
BEGIN SELECT RAISE(ABORT, 'signoff completion requires content-write completion'); END;
CREATE TRIGGER operations_destination_move_requires_attempt_update
BEFORE UPDATE OF movement_completed_at, destination_movement_attempt_id ON operations
WHEN NEW.movement_completed_at IS NOT NULL AND NEW.destination_movement_attempt_id IS NULL
BEGIN SELECT RAISE(ABORT, 'destination movement completion requires confirmed attempt'); END;
CREATE TRIGGER operations_completed_requires_timestamp_update
BEFORE UPDATE OF status, completed_at ON operations
WHEN NEW.status = 'completed' AND NEW.completed_at IS NULL
BEGIN SELECT RAISE(ABORT, 'completed operation requires completed_at'); END;
"""

_MIGRATION_7 = """
DROP TRIGGER IF EXISTS verification_cycles_signed_pair_insert;
DROP TRIGGER IF EXISTS verification_cycles_signed_pair_update;
DROP TRIGGER IF EXISTS verification_cycles_reviewed_pair_update;
DROP TRIGGER IF EXISTS verification_cycles_approved_complete_update;
DROP TRIGGER IF EXISTS verification_cycles_route_resume_update;
DROP TRIGGER IF EXISTS operations_signoff_requires_write_update;
DROP TRIGGER IF EXISTS operations_destination_move_requires_attempt_update;
DROP TRIGGER IF EXISTS operations_completed_requires_timestamp_update;

CREATE TRIGGER verification_cycles_binding_insert
BEFORE INSERT ON verification_cycles
WHEN ((NEW.reviewed_content_version_id IS NULL) != (NEW.reviewed_identity IS NULL))
  OR ((NEW.signed_content_version_id IS NULL) != (NEW.signed_identity IS NULL))
  OR (NEW.reviewed_content_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM content_versions cv
         WHERE cv.content_version_id = NEW.reviewed_content_version_id
           AND cv.task_gid = NEW.task_gid
           AND cv.operation_id = NEW.operation_id
           AND cv.identity = NEW.reviewed_identity
           AND cv.confirmed = 1))
  OR (NEW.signed_content_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM content_versions cv
         WHERE cv.content_version_id = NEW.signed_content_version_id
           AND cv.task_gid = NEW.task_gid
           AND cv.operation_id = NEW.operation_id
           AND cv.identity = NEW.signed_identity
           AND cv.confirmed = 1))
BEGIN SELECT RAISE(ABORT, 'verification content binding is invalid'); END;

CREATE TRIGGER verification_cycles_binding_update
BEFORE UPDATE OF reviewed_content_version_id, reviewed_identity, signed_content_version_id, signed_identity, operation_id, task_gid
ON verification_cycles
WHEN ((NEW.reviewed_content_version_id IS NULL) != (NEW.reviewed_identity IS NULL))
  OR ((NEW.signed_content_version_id IS NULL) != (NEW.signed_identity IS NULL))
  OR (NEW.reviewed_content_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM content_versions cv
         WHERE cv.content_version_id = NEW.reviewed_content_version_id
           AND cv.task_gid = NEW.task_gid
           AND cv.operation_id = NEW.operation_id
           AND cv.identity = NEW.reviewed_identity
           AND cv.confirmed = 1))
  OR (NEW.signed_content_version_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM content_versions cv
         WHERE cv.content_version_id = NEW.signed_content_version_id
           AND cv.task_gid = NEW.task_gid
           AND cv.operation_id = NEW.operation_id
           AND cv.identity = NEW.signed_identity
           AND cv.confirmed = 1))
BEGIN SELECT RAISE(ABORT, 'verification content binding is invalid'); END;

CREATE TRIGGER verification_cycles_state_insert
BEFORE INSERT ON verification_cycles
WHEN (NEW.outcome = 'approved' AND (NEW.completed_at IS NULL OR NEW.signed_content_version_id IS NULL OR NEW.signed_identity IS NULL))
   OR (NEW.route IS NULL AND COALESCE(NEW.resume_state, 'None') != 'None' AND COALESCE(NEW.outcome, '') != 'two-pass-hold')
   OR (NEW.route IS NOT NULL AND COALESCE(NEW.resume_state, 'None') = 'None')
BEGIN SELECT RAISE(ABORT, 'verification cycle state is invalid'); END;

CREATE TRIGGER verification_cycles_state_update
BEFORE UPDATE OF outcome, completed_at, signed_content_version_id, signed_identity, route, resume_state
ON verification_cycles
WHEN (NEW.outcome = 'approved' AND (NEW.completed_at IS NULL OR NEW.signed_content_version_id IS NULL OR NEW.signed_identity IS NULL))
   OR (NEW.route IS NULL AND COALESCE(NEW.resume_state, 'None') != 'None' AND COALESCE(NEW.outcome, '') != 'two-pass-hold')
   OR (NEW.route IS NOT NULL AND COALESCE(NEW.resume_state, 'None') = 'None')
BEGIN SELECT RAISE(ABORT, 'verification cycle state is invalid'); END;

CREATE TRIGGER operations_state_insert
BEFORE INSERT ON operations
WHEN (NEW.signoff_completed_at IS NOT NULL AND NEW.content_write_completed_at IS NULL)
   OR (NEW.status = 'completed' AND NEW.completed_at IS NULL)
   OR (NEW.movement_completed_at IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM movement_attempts ma
         WHERE ma.attempt_id = NEW.destination_movement_attempt_id
           AND ma.operation_id = NEW.operation_id
           AND ma.purpose = 'destination_submission'
           AND ma.outcome = 'confirmed'
           AND ma.confirmed_section_gid = ma.intended_section_gid))
BEGIN SELECT RAISE(ABORT, 'operation state is invalid'); END;

CREATE TRIGGER operations_state_update
BEFORE UPDATE OF status, completed_at, content_write_completed_at, signoff_completed_at, movement_completed_at, destination_movement_attempt_id
ON operations
WHEN (NEW.signoff_completed_at IS NOT NULL AND NEW.content_write_completed_at IS NULL)
   OR (NEW.status = 'completed' AND NEW.completed_at IS NULL)
   OR (NEW.movement_completed_at IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM movement_attempts ma
         WHERE ma.attempt_id = NEW.destination_movement_attempt_id
           AND ma.operation_id = NEW.operation_id
           AND ma.purpose = 'destination_submission'
           AND ma.outcome = 'confirmed'
           AND ma.confirmed_section_gid = ma.intended_section_gid))
BEGIN SELECT RAISE(ABORT, 'operation state is invalid'); END;

CREATE TRIGGER movement_attempt_final_evidence_update
BEFORE UPDATE OF operation_id, purpose, outcome, intended_section_gid, confirmed_section_gid
ON movement_attempts
WHEN EXISTS (SELECT 1 FROM operations o WHERE o.destination_movement_attempt_id = OLD.attempt_id AND o.movement_completed_at IS NOT NULL)
 AND (NEW.operation_id != OLD.operation_id
      OR NEW.purpose != 'destination_submission'
      OR NEW.outcome != 'confirmed'
      OR NEW.confirmed_section_gid IS NULL
      OR NEW.confirmed_section_gid != NEW.intended_section_gid)
BEGIN SELECT RAISE(ABORT, 'final destination movement evidence cannot be weakened'); END;
"""

_MIGRATION_8 = """
ALTER TABLE operations ADD COLUMN phase TEXT NOT NULL DEFAULT 'prepare_required';
ALTER TABLE operations ADD COLUMN terminal_outcome TEXT;
ALTER TABLE operations ADD COLUMN inherited_signoff_cycle_id TEXT REFERENCES verification_cycles(cycle_id);
UPDATE operations
   SET phase = CASE
       WHEN status IN ('completed','cancelled') THEN 'terminal'
       WHEN movement_completed_at IS NOT NULL THEN 'terminal'
       WHEN signoff_completed_at IS NOT NULL THEN 'await_submission'
       WHEN content_write_completed_at IS NOT NULL THEN 'await_verification'
       ELSE 'prepare_required'
   END;
CREATE INDEX operations_phase_idx ON operations(status, phase);
CREATE TRIGGER operations_terminal_phase_insert
BEFORE INSERT ON operations
WHEN (NEW.status IN ('completed','cancelled') AND NEW.phase != 'terminal')
  OR (NEW.phase = 'terminal' AND NEW.status NOT IN ('completed','cancelled'))
BEGIN SELECT RAISE(ABORT, 'operation terminal phase/status mismatch'); END;
CREATE TRIGGER operations_terminal_phase_update
BEFORE UPDATE OF status, phase ON operations
WHEN (NEW.status IN ('completed','cancelled') AND NEW.phase != 'terminal')
  OR (NEW.phase = 'terminal' AND NEW.status NOT IN ('completed','cancelled'))
BEGIN SELECT RAISE(ABORT, 'operation terminal phase/status mismatch'); END;
"""

MIGRATIONS = {1: _MIGRATION_1, 2: _MIGRATION_2, 3: _MIGRATION_3, 4: _MIGRATION_4, 5: _MIGRATION_5, 6: _MIGRATION_6, 7: _MIGRATION_7, 8: _MIGRATION_8}


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


def _execute_script_statements(conn: sqlite3.Connection, script: str) -> None:
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            sql = statement.strip()
            if sql:
                conn.execute(sql)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError("incomplete migration SQL statement")


def migrate_database(conn: sqlite3.Connection) -> None:
    # Hold one SQLite write lock across discovery and every migration. This makes
    # concurrent initializers serialize instead of racing on CREATE/ALTER steps.
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS schema_migrations (
                   version INTEGER PRIMARY KEY,
                   applied_at TEXT NOT NULL
               )"""
        )
        applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
        for version in sorted(MIGRATIONS):
            if version in applied:
                continue
            _execute_script_statements(conn, MIGRATIONS[version])
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, utc_now()),
            )
            conn.execute(f"PRAGMA user_version = {version}")
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
