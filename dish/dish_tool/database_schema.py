"""SQLite schema definitions and durable semantic-evidence validation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from .constants import SUBMISSION_STATES
from .errors import DishRuleError
from .models import utc_now

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

_MIGRATION_9 = """
CREATE TABLE operation_steps (
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    step_name TEXT NOT NULL,
    intended_json TEXT NOT NULL CHECK (json_valid(intended_json)),
    completed_at TEXT,
    PRIMARY KEY(operation_id, step_name)
);
CREATE INDEX operation_steps_pending_idx ON operation_steps(operation_id, completed_at);
"""

_MIGRATION_10 = """
CREATE TABLE operation_actor_facts (
    fact_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    task_gid TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('planner','constructor','material_editor','verifier','human')),
    agent TEXT NOT NULL,
    run_id TEXT,
    independence_attestation TEXT,
    candidate_identity TEXT,
    source_cycle_id TEXT REFERENCES verification_cycles(cycle_id),
    created_at TEXT NOT NULL
);
CREATE INDEX operation_actor_facts_operation_idx ON operation_actor_facts(operation_id, created_at);
CREATE INDEX operation_actor_facts_run_idx ON operation_actor_facts(operation_id, run_id);
"""

_MIGRATION_11 = """
CREATE TABLE marco_authorizations (
    authorization_id TEXT PRIMARY KEY,
    task_gid TEXT NOT NULL,
    operation_id TEXT REFERENCES operations(operation_id),
    field_name TEXT NOT NULL,
    before_json TEXT NOT NULL CHECK (json_valid(before_json)),
    after_json TEXT NOT NULL CHECK (json_valid(after_json)),
    reason TEXT NOT NULL,
    actor_run_id TEXT,
    created_at TEXT NOT NULL,
    consumed_at TEXT
);
CREATE INDEX marco_authorizations_lookup_idx
    ON marco_authorizations(task_gid, operation_id, field_name, consumed_at, created_at);
"""

_MIGRATION_12 = """
CREATE TRIGGER write_attempt_confirmed_binding_insert
BEFORE INSERT ON write_attempts
WHEN NEW.outcome = 'confirmed' AND (
    NEW.confirmed_content_version_id IS NULL OR
    NOT EXISTS (
        SELECT 1 FROM content_versions cv
        JOIN operations o ON o.operation_id = NEW.operation_id
        WHERE cv.content_version_id = NEW.confirmed_content_version_id
          AND cv.operation_id = NEW.operation_id
          AND cv.task_gid = o.task_gid
          AND cv.confirmed = 1
          AND cv.identity = NEW.intended_identity
    )
)
BEGIN SELECT RAISE(ABORT, 'confirmed write attempt requires exact content-version evidence'); END;
CREATE TRIGGER write_attempt_confirmed_binding_update
BEFORE UPDATE OF outcome, confirmed_content_version_id, intended_identity, operation_id ON write_attempts
WHEN NEW.outcome = 'confirmed' AND (
    NEW.confirmed_content_version_id IS NULL OR
    NOT EXISTS (
        SELECT 1 FROM content_versions cv
        JOIN operations o ON o.operation_id = NEW.operation_id
        WHERE cv.content_version_id = NEW.confirmed_content_version_id
          AND cv.operation_id = NEW.operation_id
          AND cv.task_gid = o.task_gid
          AND cv.confirmed = 1
          AND cv.identity = NEW.intended_identity
    )
)
BEGIN SELECT RAISE(ABORT, 'confirmed write attempt requires exact content-version evidence'); END;
CREATE TRIGGER movement_attempt_confirmed_binding_insert
BEFORE INSERT ON movement_attempts
WHEN NEW.outcome = 'confirmed' AND (
    NEW.confirmed_section_gid IS NULL OR NEW.confirmed_section_gid != NEW.intended_section_gid
)
BEGIN SELECT RAISE(ABORT, 'confirmed movement attempt requires exact placement evidence'); END;
CREATE TRIGGER movement_attempt_confirmed_binding_update
BEFORE UPDATE OF outcome, confirmed_section_gid, intended_section_gid ON movement_attempts
WHEN NEW.outcome = 'confirmed' AND (
    NEW.confirmed_section_gid IS NULL OR NEW.confirmed_section_gid != NEW.intended_section_gid
)
BEGIN SELECT RAISE(ABORT, 'confirmed movement attempt requires exact placement evidence'); END;
CREATE TRIGGER operations_signoff_requires_approved_cycle_insert
BEFORE INSERT ON operations
WHEN NEW.signoff_completed_at IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM verification_cycles vc
     WHERE vc.operation_id = NEW.operation_id AND vc.outcome='approved'
       AND vc.completed_at IS NOT NULL
       AND vc.signed_content_version_id IS NOT NULL
       AND vc.signed_identity IS NOT NULL
)
BEGIN SELECT RAISE(ABORT, 'signoff completion requires an approved signed cycle'); END;
CREATE TRIGGER operations_signoff_requires_approved_cycle_update
BEFORE UPDATE OF signoff_completed_at ON operations
WHEN NEW.signoff_completed_at IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM verification_cycles vc
     WHERE vc.operation_id = NEW.operation_id AND vc.outcome='approved'
       AND vc.completed_at IS NOT NULL
       AND vc.signed_content_version_id IS NOT NULL
       AND vc.signed_identity IS NOT NULL
)
BEGIN SELECT RAISE(ABORT, 'signoff completion requires an approved signed cycle'); END;
"""

_MIGRATION_13 = """
ALTER TABLE marco_authorizations ADD COLUMN reserved_by_operation_id TEXT REFERENCES operations(operation_id);
ALTER TABLE marco_authorizations ADD COLUMN reserved_at TEXT;
ALTER TABLE marco_authorizations ADD COLUMN consumed_identity TEXT;
CREATE INDEX marco_authorizations_reservation_idx
    ON marco_authorizations(task_gid, operation_id, field_name, consumed_at, reserved_by_operation_id, created_at);
"""


_MIGRATION_14 = """
CREATE TABLE command_audit_repairs (
    repair_id TEXT PRIMARY KEY,
    command TEXT NOT NULL,
    operation_id TEXT REFERENCES operations(operation_id),
    submission_id TEXT REFERENCES submissions(submission_id),
    task_gid TEXT,
    actor_agent TEXT,
    result_json TEXT NOT NULL CHECK (json_valid(result_json)),
    audit_error TEXT NOT NULL,
    created_at TEXT NOT NULL,
    repaired_at TEXT
);
CREATE INDEX command_audit_repairs_pending_idx ON command_audit_repairs(repaired_at, created_at);

CREATE TRIGGER verification_cycles_approved_append_only_update
BEFORE UPDATE ON verification_cycles
WHEN OLD.outcome = 'approved' AND (
    NEW.outcome IS NOT OLD.outcome OR NEW.completed_at IS NOT OLD.completed_at OR
    NEW.signed_content_version_id IS NOT OLD.signed_content_version_id OR
    NEW.signed_identity IS NOT OLD.signed_identity OR
    NEW.reviewed_content_version_id IS NOT OLD.reviewed_content_version_id OR
    NEW.reviewed_identity IS NOT OLD.reviewed_identity OR
    NEW.verifier_agent IS NOT OLD.verifier_agent OR NEW.run_id IS NOT OLD.run_id OR
    NEW.independence_attestation IS NOT OLD.independence_attestation
)
BEGIN SELECT RAISE(ABORT, 'approved verification evidence is append-only'); END;
CREATE TRIGGER verification_cycles_evidence_delete
BEFORE DELETE ON verification_cycles
WHEN OLD.completed_at IS NOT NULL OR OLD.reviewed_identity IS NOT NULL OR OLD.signed_identity IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'verification evidence is append-only'); END;

CREATE TRIGGER write_attempt_confirmed_append_only_update
BEFORE UPDATE ON write_attempts
WHEN OLD.outcome = 'confirmed' AND (
    NEW.operation_id IS NOT OLD.operation_id OR NEW.expected_identity IS NOT OLD.expected_identity OR
    NEW.intended_identity IS NOT OLD.intended_identity OR NEW.outcome IS NOT OLD.outcome OR
    NEW.finished_at IS NOT OLD.finished_at OR NEW.purpose IS NOT OLD.purpose OR
    NEW.intended_title IS NOT OLD.intended_title OR NEW.intended_notes IS NOT OLD.intended_notes OR
    NEW.schema_version IS NOT OLD.schema_version OR NEW.context_json IS NOT OLD.context_json OR
    NEW.confirmed_content_version_id IS NOT OLD.confirmed_content_version_id
)
BEGIN SELECT RAISE(ABORT, 'confirmed write evidence is append-only'); END;
CREATE TRIGGER write_attempt_confirmed_append_only_delete
BEFORE DELETE ON write_attempts WHEN OLD.outcome = 'confirmed'
BEGIN SELECT RAISE(ABORT, 'confirmed write evidence is append-only'); END;

CREATE TRIGGER content_versions_confirmed_append_only_update
BEFORE UPDATE ON content_versions
WHEN OLD.confirmed = 1 AND (
    NEW.task_gid IS NOT OLD.task_gid OR NEW.operation_id IS NOT OLD.operation_id OR
    NEW.boundary IS NOT OLD.boundary OR NEW.identity IS NOT OLD.identity OR
    NEW.title IS NOT OLD.title OR NEW.notes IS NOT OLD.notes OR
    NEW.confirmed IS NOT OLD.confirmed OR NEW.created_at IS NOT OLD.created_at
)
BEGIN SELECT RAISE(ABORT, 'confirmed content evidence is append-only'); END;
CREATE TRIGGER content_versions_confirmed_append_only_delete
BEFORE DELETE ON content_versions WHEN OLD.confirmed = 1
BEGIN SELECT RAISE(ABORT, 'confirmed content evidence is append-only'); END;

CREATE TRIGGER operations_completion_monotonic_update
BEFORE UPDATE ON operations
WHEN (OLD.content_write_completed_at IS NOT NULL AND NEW.content_write_completed_at IS NOT OLD.content_write_completed_at)
  OR (OLD.signoff_completed_at IS NOT NULL AND NEW.signoff_completed_at IS NOT OLD.signoff_completed_at)
  OR (OLD.movement_completed_at IS NOT NULL AND NEW.movement_completed_at IS NOT OLD.movement_completed_at)
  OR (OLD.completed_at IS NOT NULL AND NEW.completed_at IS NOT OLD.completed_at)
  OR (OLD.status IN ('completed','cancelled') AND NEW.status IS NOT OLD.status)
  OR (OLD.phase = 'terminal' AND NEW.phase IS NOT OLD.phase)
BEGIN SELECT RAISE(ABORT, 'operation completion evidence is monotonic'); END;

CREATE TRIGGER operation_actor_facts_append_only_update
BEFORE UPDATE ON operation_actor_facts
BEGIN SELECT RAISE(ABORT, 'actor facts are append-only'); END;
CREATE TRIGGER operation_actor_facts_append_only_delete
BEFORE DELETE ON operation_actor_facts
BEGIN SELECT RAISE(ABORT, 'actor facts are append-only'); END;
CREATE TRIGGER audit_events_append_only_update
BEFORE UPDATE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
CREATE TRIGGER audit_events_append_only_delete
BEFORE DELETE ON audit_events
BEGIN SELECT RAISE(ABORT, 'audit events are append-only'); END;
"""



_MIGRATION_15 = """
CREATE TRIGGER verification_cycles_completed_fully_immutable_update
BEFORE UPDATE ON verification_cycles
WHEN OLD.completed_at IS NOT NULL AND (
    NEW.operation_id IS NOT OLD.operation_id OR NEW.task_gid IS NOT OLD.task_gid OR
    NEW.cycle_number IS NOT OLD.cycle_number OR NEW.protocol_release IS NOT OLD.protocol_release OR
    NEW.protocol_text IS NOT OLD.protocol_text OR NEW.verifier_agent IS NOT OLD.verifier_agent OR
    NEW.run_id IS NOT OLD.run_id OR NEW.independence_attestation IS NOT OLD.independence_attestation OR
    NEW.correction_class IS NOT OLD.correction_class OR NEW.outcome IS NOT OLD.outcome OR
    NEW.route IS NOT OLD.route OR NEW.resume_state IS NOT OLD.resume_state OR
    NEW.created_at IS NOT OLD.created_at OR NEW.completed_at IS NOT OLD.completed_at OR
    NEW.reviewed_content_version_id IS NOT OLD.reviewed_content_version_id OR
    NEW.reviewed_identity IS NOT OLD.reviewed_identity OR
    NEW.signed_content_version_id IS NOT OLD.signed_content_version_id OR
    NEW.signed_identity IS NOT OLD.signed_identity
)
BEGIN SELECT RAISE(ABORT, 'completed verification cycle is immutable'); END;

CREATE TRIGGER operations_completed_fully_immutable_update
BEFORE UPDATE ON operations
WHEN OLD.status='completed' AND (
    NEW.task_gid IS NOT OLD.task_gid OR NEW.operation_kind IS NOT OLD.operation_kind OR
    NEW.status IS NOT OLD.status OR NEW.editor_agent IS NOT OLD.editor_agent OR
    NEW.researcher_agent IS NOT OLD.researcher_agent OR NEW.verifier_agent IS NOT OLD.verifier_agent OR
    NEW.run_id IS NOT OLD.run_id OR NEW.independence_attestation IS NOT OLD.independence_attestation OR
    NEW.expected_identity IS NOT OLD.expected_identity OR NEW.schema_version IS NOT OLD.schema_version OR
    NEW.content_write_completed_at IS NOT OLD.content_write_completed_at OR
    NEW.signoff_completed_at IS NOT OLD.signoff_completed_at OR
    NEW.movement_completed_at IS NOT OLD.movement_completed_at OR
    NEW.created_at IS NOT OLD.created_at OR NEW.completed_at IS NOT OLD.completed_at OR
    NEW.destination_movement_attempt_id IS NOT OLD.destination_movement_attempt_id OR
    NEW.phase IS NOT OLD.phase OR NEW.terminal_outcome IS NOT OLD.terminal_outcome OR
    NEW.inherited_signoff_cycle_id IS NOT OLD.inherited_signoff_cycle_id
)
BEGIN SELECT RAISE(ABORT, 'completed operation is immutable'); END;

CREATE TRIGGER write_attempt_confirmed_started_at_immutable
BEFORE UPDATE OF started_at ON write_attempts
WHEN OLD.outcome='confirmed' AND NEW.started_at IS NOT OLD.started_at
BEGIN SELECT RAISE(ABORT, 'confirmed write start time is immutable'); END;

CREATE TRIGGER marco_authorizations_consumed_immutable_update
BEFORE UPDATE ON marco_authorizations
WHEN OLD.consumed_at IS NOT NULL AND (
    NEW.task_gid IS NOT OLD.task_gid OR NEW.operation_id IS NOT OLD.operation_id OR
    NEW.field_name IS NOT OLD.field_name OR NEW.before_json IS NOT OLD.before_json OR
    NEW.after_json IS NOT OLD.after_json OR NEW.reason IS NOT OLD.reason OR
    NEW.actor_run_id IS NOT OLD.actor_run_id OR NEW.created_at IS NOT OLD.created_at OR
    NEW.consumed_at IS NOT OLD.consumed_at OR
    NEW.reserved_by_operation_id IS NOT OLD.reserved_by_operation_id OR
    NEW.reserved_at IS NOT OLD.reserved_at OR NEW.consumed_identity IS NOT OLD.consumed_identity
)
BEGIN SELECT RAISE(ABORT, 'consumed Marco authorization is immutable'); END;
CREATE TRIGGER marco_authorizations_consumed_immutable_delete
BEFORE DELETE ON marco_authorizations WHEN OLD.consumed_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'consumed Marco authorization is immutable'); END;

CREATE TRIGGER command_audit_repairs_monotonic_update
BEFORE UPDATE ON command_audit_repairs
WHEN NEW.repair_id IS NOT OLD.repair_id OR NEW.command IS NOT OLD.command OR
     NEW.operation_id IS NOT OLD.operation_id OR NEW.submission_id IS NOT OLD.submission_id OR
     NEW.task_gid IS NOT OLD.task_gid OR NEW.actor_agent IS NOT OLD.actor_agent OR
     NEW.result_json IS NOT OLD.result_json OR NEW.audit_error IS NOT OLD.audit_error OR
     NEW.created_at IS NOT OLD.created_at OR
     (OLD.repaired_at IS NOT NULL AND NEW.repaired_at IS NOT OLD.repaired_at)
BEGIN SELECT RAISE(ABORT, 'audit repair facts are append-only'); END;
CREATE TRIGGER command_audit_repairs_delete
BEFORE DELETE ON command_audit_repairs
BEGIN SELECT RAISE(ABORT, 'audit repair facts are append-only'); END;
"""

_MIGRATION_16 = """
ALTER TABLE operations ADD COLUMN expected_section_gid TEXT;
"""


_MIGRATION_17 = """
CREATE TRIGGER operation_steps_intent_immutable_update
BEFORE UPDATE ON operation_steps
WHEN NEW.operation_id IS NOT OLD.operation_id OR NEW.step_name IS NOT OLD.step_name OR
     NEW.intended_json IS NOT OLD.intended_json OR
     (OLD.completed_at IS NOT NULL AND NEW.completed_at IS NOT OLD.completed_at) OR
     (OLD.completed_at IS NULL AND NEW.completed_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'operation step intent is immutable and completion is monotonic'); END;
CREATE TRIGGER operation_steps_append_only_delete
BEFORE DELETE ON operation_steps
BEGIN SELECT RAISE(ABORT, 'operation steps are append-only'); END;

CREATE TRIGGER write_attempt_confirmed_id_immutable
BEFORE UPDATE OF attempt_id ON write_attempts
WHEN OLD.outcome='confirmed' AND NEW.attempt_id IS NOT OLD.attempt_id
BEGIN SELECT RAISE(ABORT, 'confirmed write attempt identity is immutable'); END;

CREATE TRIGGER movement_attempt_confirmed_immutable_update
BEFORE UPDATE ON movement_attempts
WHEN OLD.outcome='confirmed' AND (
    NEW.attempt_id IS NOT OLD.attempt_id OR NEW.operation_id IS NOT OLD.operation_id OR
    NEW.expected_section_gid IS NOT OLD.expected_section_gid OR
    NEW.intended_section_gid IS NOT OLD.intended_section_gid OR
    NEW.outcome IS NOT OLD.outcome OR NEW.started_at IS NOT OLD.started_at OR
    NEW.finished_at IS NOT OLD.finished_at OR NEW.purpose IS NOT OLD.purpose OR
    NEW.confirmed_section_gid IS NOT OLD.confirmed_section_gid
)
BEGIN SELECT RAISE(ABORT, 'confirmed movement evidence is immutable'); END;
CREATE TRIGGER movement_attempt_confirmed_immutable_delete
BEFORE DELETE ON movement_attempts WHEN OLD.outcome='confirmed'
BEGIN SELECT RAISE(ABORT, 'confirmed movement evidence is append-only'); END;

CREATE TRIGGER marco_authorizations_consumed_id_immutable
BEFORE UPDATE OF authorization_id ON marco_authorizations
WHEN OLD.consumed_at IS NOT NULL AND NEW.authorization_id IS NOT OLD.authorization_id
BEGIN SELECT RAISE(ABORT, 'consumed Marco authorization identity is immutable'); END;

CREATE TRIGGER operations_completed_section_immutable
BEFORE UPDATE OF expected_section_gid ON operations
WHEN OLD.status='completed' AND NEW.expected_section_gid IS NOT OLD.expected_section_gid
BEGIN SELECT RAISE(ABORT, 'completed operation placement baseline is immutable'); END;
"""


_MIGRATION_18 = """
ALTER TABLE task_content_state
    ADD COLUMN last_confirmed_content_version_id TEXT
        REFERENCES content_versions(content_version_id);
ALTER TABLE operations
    ADD COLUMN migration_reconciliation_required INTEGER NOT NULL DEFAULT 0
        CHECK (migration_reconciliation_required IN (0,1));
ALTER TABLE operations ADD COLUMN migration_reconciliation_reason TEXT;

-- Migration 8 made old terminal rows structurally terminal but did not record
-- why they were terminal. Temporarily lower the completed-row guard only for
-- this one deterministic backfill, then restore the canonical trigger.
DROP TRIGGER operations_completed_fully_immutable_update;
UPDATE operations
   SET terminal_outcome = CASE
       WHEN status = 'cancelled' THEN 'legacy_cancelled'
       ELSE 'legacy_completed'
   END
 WHERE status IN ('completed','cancelled')
   AND terminal_outcome IS NULL;

CREATE TRIGGER operations_completed_fully_immutable_update
BEFORE UPDATE ON operations
WHEN OLD.status='completed' AND (
    NEW.task_gid IS NOT OLD.task_gid OR NEW.operation_kind IS NOT OLD.operation_kind OR
    NEW.status IS NOT OLD.status OR NEW.editor_agent IS NOT OLD.editor_agent OR
    NEW.researcher_agent IS NOT OLD.researcher_agent OR NEW.verifier_agent IS NOT OLD.verifier_agent OR
    NEW.run_id IS NOT OLD.run_id OR NEW.independence_attestation IS NOT OLD.independence_attestation OR
    NEW.expected_identity IS NOT OLD.expected_identity OR NEW.schema_version IS NOT OLD.schema_version OR
    NEW.content_write_completed_at IS NOT OLD.content_write_completed_at OR
    NEW.signoff_completed_at IS NOT OLD.signoff_completed_at OR
    NEW.movement_completed_at IS NOT OLD.movement_completed_at OR
    NEW.created_at IS NOT OLD.created_at OR NEW.completed_at IS NOT OLD.completed_at OR
    NEW.destination_movement_attempt_id IS NOT OLD.destination_movement_attempt_id OR
    NEW.phase IS NOT OLD.phase OR NEW.terminal_outcome IS NOT OLD.terminal_outcome OR
    NEW.inherited_signoff_cycle_id IS NOT OLD.inherited_signoff_cycle_id
)
BEGIN SELECT RAISE(ABORT, 'completed operation is immutable'); END;

-- Give every persisted task head an exact append-only provenance record.
INSERT INTO content_versions (
    content_version_id, task_gid, operation_id, boundary, identity,
    title, notes, confirmed, created_at
)
SELECT 'migration-head-' || lower(hex(randomblob(16))),
       state.task_gid, NULL, 'migration_task_head',
       state.last_confirmed_identity, state.last_confirmed_title,
       state.last_confirmed_notes, 1, state.confirmed_at
  FROM task_content_state AS state
 WHERE NOT EXISTS (
       SELECT 1 FROM content_versions AS version
        WHERE version.task_gid = state.task_gid
          AND version.confirmed = 1
          AND version.identity = state.last_confirmed_identity
          AND version.title = state.last_confirmed_title
          AND version.notes = state.last_confirmed_notes
 );

UPDATE task_content_state
   SET last_confirmed_content_version_id = (
       SELECT version.content_version_id
         FROM content_versions AS version
        WHERE version.task_gid = task_content_state.task_gid
          AND version.confirmed = 1
          AND version.identity = task_content_state.last_confirmed_identity
          AND version.title = task_content_state.last_confirmed_title
          AND version.notes = task_content_state.last_confirmed_notes
        ORDER BY version.created_at DESC, version.rowid DESC
        LIMIT 1
   );

-- Historical active operations predate actor facts. Backfill the best exact
-- lineage available from the operation row; absence remains a quarantine fact.
INSERT INTO operation_actor_facts (
    fact_id, operation_id, task_gid, role, agent, run_id,
    independence_attestation, candidate_identity, source_cycle_id, created_at
)
SELECT 'migration-actor-' || lower(hex(randomblob(16))),
       operation.operation_id, operation.task_gid,
       CASE operation.operation_kind
           WHEN 'planning' THEN 'planner'
           WHEN 'initial' THEN 'constructor'
           ELSE 'material_editor'
       END,
       COALESCE(operation.researcher_agent, operation.editor_agent),
       operation.run_id, operation.independence_attestation,
       operation.expected_identity, NULL, operation.created_at
  FROM operations AS operation
 WHERE operation.status IN ('open','uncertain')
   AND COALESCE(operation.researcher_agent, operation.editor_agent) IS NOT NULL
   AND NOT EXISTS (
       SELECT 1 FROM operation_actor_facts AS fact
        WHERE fact.operation_id = operation.operation_id
          AND fact.role IN ('planner','constructor','material_editor')
   );

UPDATE operations
   SET migration_reconciliation_required = 1,
       migration_reconciliation_reason = 'missing_expected_section_gid'
 WHERE status IN ('open','uncertain')
   AND expected_section_gid IS NULL;

UPDATE operations
   SET migration_reconciliation_required = 1,
       migration_reconciliation_reason = CASE
           WHEN migration_reconciliation_reason IS NULL
               THEN 'missing_actor_lineage'
           ELSE migration_reconciliation_reason || ',missing_actor_lineage'
       END
 WHERE status IN ('open','uncertain')
   AND NOT EXISTS (
       SELECT 1 FROM operation_actor_facts AS fact
        WHERE fact.operation_id = operations.operation_id
          AND fact.role IN ('planner','constructor','material_editor')
   );

DROP INDEX operations_one_open_per_task;
CREATE UNIQUE INDEX operations_one_active_per_task
    ON operations(task_gid) WHERE status IN ('open','uncertain');

CREATE TRIGGER operations_creation_facts_immutable_update
BEFORE UPDATE ON operations
WHEN NEW.operation_id IS NOT OLD.operation_id
  OR NEW.task_gid IS NOT OLD.task_gid
  OR NEW.operation_kind IS NOT OLD.operation_kind
  OR NEW.expected_identity IS NOT OLD.expected_identity
  OR NEW.schema_version IS NOT OLD.schema_version
  OR NEW.expected_section_gid IS NOT OLD.expected_section_gid
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.migration_reconciliation_required IS NOT OLD.migration_reconciliation_required
  OR NEW.migration_reconciliation_reason IS NOT OLD.migration_reconciliation_reason
BEGIN SELECT RAISE(ABORT, 'operation creation facts are immutable'); END;
CREATE TRIGGER operations_append_only_delete
BEFORE DELETE ON operations
BEGIN SELECT RAISE(ABORT, 'operations are append-only'); END;

CREATE TRIGGER write_attempt_intent_immutable_update
BEFORE UPDATE ON write_attempts
WHEN NEW.attempt_id IS NOT OLD.attempt_id
  OR NEW.operation_id IS NOT OLD.operation_id
  OR NEW.expected_identity IS NOT OLD.expected_identity
  OR NEW.intended_identity IS NOT OLD.intended_identity
  OR NEW.started_at IS NOT OLD.started_at
  OR NEW.purpose IS NOT OLD.purpose
  OR NEW.intended_title IS NOT OLD.intended_title
  OR NEW.intended_notes IS NOT OLD.intended_notes
  OR NEW.schema_version IS NOT OLD.schema_version
  OR NEW.context_json IS NOT OLD.context_json
BEGIN SELECT RAISE(ABORT, 'write attempt intent is immutable'); END;
CREATE TRIGGER write_attempt_outcome_monotonic_update
BEFORE UPDATE ON write_attempts
WHEN NOT (
    NEW.outcome = OLD.outcome
    OR (OLD.outcome = 'started' AND NEW.outcome IN ('confirmed','not_applied','uncertain'))
    OR (OLD.outcome = 'uncertain' AND NEW.outcome IN ('confirmed','not_applied'))
)
OR (OLD.finished_at IS NOT NULL AND NEW.finished_at IS NOT OLD.finished_at)
OR (OLD.confirmed_content_version_id IS NOT NULL
    AND NEW.confirmed_content_version_id IS NOT OLD.confirmed_content_version_id)
OR (NEW.outcome IN ('confirmed','not_applied') AND NEW.finished_at IS NULL)
OR (NEW.outcome != 'confirmed' AND NEW.confirmed_content_version_id IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'write attempt outcome is monotonic'); END;
CREATE TRIGGER write_attempt_append_only_delete
BEFORE DELETE ON write_attempts
BEGIN SELECT RAISE(ABORT, 'write attempts are append-only'); END;

CREATE TRIGGER movement_attempt_intent_immutable_update
BEFORE UPDATE ON movement_attempts
WHEN NEW.attempt_id IS NOT OLD.attempt_id
  OR NEW.operation_id IS NOT OLD.operation_id
  OR NEW.expected_section_gid IS NOT OLD.expected_section_gid
  OR NEW.intended_section_gid IS NOT OLD.intended_section_gid
  OR NEW.started_at IS NOT OLD.started_at
  OR NEW.purpose IS NOT OLD.purpose
BEGIN SELECT RAISE(ABORT, 'movement attempt intent is immutable'); END;
CREATE TRIGGER movement_attempt_outcome_monotonic_update
BEFORE UPDATE ON movement_attempts
WHEN NOT (
    NEW.outcome = OLD.outcome
    OR (OLD.outcome = 'started' AND NEW.outcome IN ('confirmed','not_applied','uncertain'))
    OR (OLD.outcome = 'uncertain' AND NEW.outcome IN ('confirmed','not_applied'))
)
OR (OLD.finished_at IS NOT NULL AND NEW.finished_at IS NOT OLD.finished_at)
OR (OLD.confirmed_section_gid IS NOT NULL
    AND NEW.confirmed_section_gid IS NOT OLD.confirmed_section_gid)
OR (NEW.outcome IN ('confirmed','not_applied') AND NEW.finished_at IS NULL)
OR (NEW.outcome != 'confirmed' AND NEW.confirmed_section_gid IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'movement attempt outcome is monotonic'); END;
CREATE TRIGGER movement_attempt_append_only_delete
BEFORE DELETE ON movement_attempts
BEGIN SELECT RAISE(ABORT, 'movement attempts are append-only'); END;

CREATE TRIGGER marco_authorizations_grant_immutable_update
BEFORE UPDATE ON marco_authorizations
WHEN NEW.authorization_id IS NOT OLD.authorization_id
  OR NEW.task_gid IS NOT OLD.task_gid
  OR NEW.operation_id IS NOT OLD.operation_id
  OR NEW.field_name IS NOT OLD.field_name
  OR NEW.before_json IS NOT OLD.before_json
  OR NEW.after_json IS NOT OLD.after_json
  OR NEW.reason IS NOT OLD.reason
  OR NEW.actor_run_id IS NOT OLD.actor_run_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'Marco authorization grant is immutable'); END;
CREATE TRIGGER marco_authorizations_append_only_delete
BEFORE DELETE ON marco_authorizations
BEGIN SELECT RAISE(ABORT, 'Marco authorizations are append-only'); END;

CREATE TRIGGER verification_cycles_creation_facts_immutable_update
BEFORE UPDATE ON verification_cycles
WHEN NEW.cycle_id IS NOT OLD.cycle_id
  OR NEW.operation_id IS NOT OLD.operation_id
  OR NEW.task_gid IS NOT OLD.task_gid
  OR NEW.cycle_number IS NOT OLD.cycle_number
  OR NEW.protocol_release IS NOT OLD.protocol_release
  OR (OLD.protocol_text IS NOT NULL AND NEW.protocol_text IS NOT OLD.protocol_text)
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'verification cycle creation facts are immutable'); END;
CREATE TRIGGER verification_cycles_review_binding_monotonic_update
BEFORE UPDATE ON verification_cycles
WHEN (OLD.verifier_agent IS NOT NULL AND NEW.verifier_agent IS NOT OLD.verifier_agent)
  OR (OLD.run_id IS NOT NULL AND NEW.run_id IS NOT OLD.run_id)
  OR (OLD.independence_attestation IS NOT NULL
      AND NEW.independence_attestation IS NOT OLD.independence_attestation)
  OR (
      (
          OLD.reviewed_content_version_id IS NOT NULL
          AND NEW.reviewed_content_version_id IS NOT OLD.reviewed_content_version_id
      )
      OR (OLD.reviewed_identity IS NOT NULL AND NEW.reviewed_identity IS NOT OLD.reviewed_identity)
  ) AND NOT (
      OLD.completed_at IS NULL
      AND OLD.outcome IS NULL
      AND OLD.signed_content_version_id IS NULL
      AND OLD.signed_identity IS NULL
      AND OLD.correction_class IS NULL
      AND NEW.correction_class = 'small'
  )
BEGIN SELECT RAISE(ABORT, 'verification review binding is monotonic'); END;
CREATE TRIGGER verification_cycles_outcome_monotonic_update
BEFORE UPDATE ON verification_cycles
WHEN (OLD.completed_at IS NOT NULL AND NEW.completed_at IS NOT OLD.completed_at)
  OR (OLD.outcome IS NOT NULL AND NEW.outcome IS NOT OLD.outcome)
  OR (OLD.route IS NOT NULL AND NEW.route IS NOT OLD.route)
  OR (OLD.resume_state IS NOT NULL AND NEW.resume_state IS NOT OLD.resume_state)
  OR (OLD.signed_content_version_id IS NOT NULL
      AND NEW.signed_content_version_id IS NOT OLD.signed_content_version_id)
  OR (OLD.signed_identity IS NOT NULL AND NEW.signed_identity IS NOT OLD.signed_identity)
BEGIN SELECT RAISE(ABORT, 'verification cycle outcome is monotonic'); END;
CREATE TRIGGER verification_cycles_append_only_delete
BEFORE DELETE ON verification_cycles
BEGIN SELECT RAISE(ABORT, 'verification cycles are append-only'); END;

CREATE TRIGGER task_content_state_exact_binding_insert
BEFORE INSERT ON task_content_state
WHEN NEW.last_confirmed_content_version_id IS NULL
  OR NOT EXISTS (
      SELECT 1 FROM content_versions AS version
       WHERE version.content_version_id = NEW.last_confirmed_content_version_id
         AND version.task_gid = NEW.task_gid
         AND version.confirmed = 1
         AND version.identity = NEW.last_confirmed_identity
         AND version.title = NEW.last_confirmed_title
         AND version.notes = NEW.last_confirmed_notes
  )
BEGIN SELECT RAISE(ABORT, 'task content head requires exact confirmed content evidence'); END;
CREATE TRIGGER task_content_state_exact_binding_update
BEFORE UPDATE ON task_content_state
WHEN NEW.task_gid IS NOT OLD.task_gid
  OR NEW.last_confirmed_content_version_id IS NULL
  OR NOT EXISTS (
      SELECT 1 FROM content_versions AS version
       WHERE version.content_version_id = NEW.last_confirmed_content_version_id
         AND version.task_gid = NEW.task_gid
         AND version.confirmed = 1
         AND version.identity = NEW.last_confirmed_identity
         AND version.title = NEW.last_confirmed_title
         AND version.notes = NEW.last_confirmed_notes
  )
  OR NEW.confirmed_at < OLD.confirmed_at
  OR (
      SELECT created_at FROM content_versions
       WHERE content_version_id = NEW.last_confirmed_content_version_id
  ) < (
      SELECT created_at FROM content_versions
       WHERE content_version_id = OLD.last_confirmed_content_version_id
  )
BEGIN SELECT RAISE(ABORT, 'task content head requires monotonic exact evidence'); END;
CREATE TRIGGER task_content_state_append_only_delete
BEFORE DELETE ON task_content_state
BEGIN SELECT RAISE(ABORT, 'task content heads are append-only'); END;
"""


_MIGRATION_19 = """
ALTER TABLE verification_cycles ADD COLUMN hold_content_version_id TEXT
    REFERENCES content_versions(content_version_id);
ALTER TABLE verification_cycles ADD COLUMN hold_identity TEXT;
ALTER TABLE verification_cycles ADD COLUMN hold_section_gid TEXT;

-- Backfill locally provable inherited signoff before restoring the completed-row guard.
DROP TRIGGER operations_completed_fully_immutable_update;
UPDATE operations
   SET inherited_signoff_cycle_id=(
       SELECT cycle.cycle_id
         FROM verification_cycles AS cycle
         JOIN content_versions AS version
           ON version.content_version_id=cycle.signed_content_version_id
        WHERE cycle.task_gid=operations.task_gid
          AND cycle.outcome='approved'
          AND cycle.completed_at IS NOT NULL
          AND cycle.signed_identity=operations.expected_identity
          AND version.confirmed=1
          AND version.task_gid=operations.task_gid
          AND version.identity=operations.expected_identity
        ORDER BY cycle.completed_at DESC LIMIT 1
   )
 WHERE status='completed' AND terminal_outcome='non_material_checkin'
   AND inherited_signoff_cycle_id IS NULL;
CREATE TRIGGER operations_completed_fully_immutable_update
BEFORE UPDATE ON operations
WHEN OLD.status='completed' AND (
    NEW.task_gid IS NOT OLD.task_gid OR NEW.operation_kind IS NOT OLD.operation_kind OR
    NEW.status IS NOT OLD.status OR NEW.editor_agent IS NOT OLD.editor_agent OR
    NEW.researcher_agent IS NOT OLD.researcher_agent OR NEW.verifier_agent IS NOT OLD.verifier_agent OR
    NEW.run_id IS NOT OLD.run_id OR NEW.independence_attestation IS NOT OLD.independence_attestation OR
    NEW.expected_identity IS NOT OLD.expected_identity OR NEW.schema_version IS NOT OLD.schema_version OR
    NEW.content_write_completed_at IS NOT OLD.content_write_completed_at OR
    NEW.signoff_completed_at IS NOT OLD.signoff_completed_at OR
    NEW.movement_completed_at IS NOT OLD.movement_completed_at OR
    NEW.created_at IS NOT OLD.created_at OR NEW.completed_at IS NOT OLD.completed_at OR
    NEW.destination_movement_attempt_id IS NOT OLD.destination_movement_attempt_id OR
    NEW.phase IS NOT OLD.phase OR NEW.terminal_outcome IS NOT OLD.terminal_outcome OR
    NEW.inherited_signoff_cycle_id IS NOT OLD.inherited_signoff_cycle_id
)
BEGIN SELECT RAISE(ABORT, 'completed operation is immutable'); END;

CREATE TABLE two_pass_resets (
    reset_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    source_cycle_id TEXT NOT NULL REFERENCES verification_cycles(cycle_id),
    candidate_identity TEXT NOT NULL,
    canonical_path TEXT NOT NULL,
    category TEXT NOT NULL CHECK(category IN ('evidence','premise','method','scope')),
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(operation_id, candidate_identity)
);
CREATE INDEX two_pass_resets_operation_idx ON two_pass_resets(operation_id, created_at);
CREATE TRIGGER two_pass_resets_append_only_update
BEFORE UPDATE ON two_pass_resets
BEGIN SELECT RAISE(ABORT, 'two-pass reset evidence is append-only'); END;
CREATE TRIGGER two_pass_resets_append_only_delete
BEFORE DELETE ON two_pass_resets
BEGIN SELECT RAISE(ABORT, 'two-pass reset evidence is append-only'); END;

-- Historical active holds have no independently persisted placement snapshot.
-- They must be reconciled rather than treating the missing baseline as a wildcard.
DROP TRIGGER operations_creation_facts_immutable_update;
UPDATE operations
   SET migration_reconciliation_required=1,
       migration_reconciliation_reason=CASE
           WHEN migration_reconciliation_reason IS NULL THEN 'missing_hold_baseline'
           WHEN instr(migration_reconciliation_reason, 'missing_hold_baseline')=0
               THEN migration_reconciliation_reason || ',missing_hold_baseline'
           ELSE migration_reconciliation_reason
       END
 WHERE status IN ('open','uncertain')
   AND EXISTS (
       SELECT 1 FROM verification_cycles AS cycle
        WHERE cycle.operation_id=operations.operation_id
          AND cycle.completed_at IS NOT NULL
          AND (cycle.route IN ('evidence','human_review') OR cycle.outcome='two-pass-hold')
          AND cycle.hold_content_version_id IS NULL
   );
CREATE TRIGGER operations_creation_facts_immutable_update
BEFORE UPDATE ON operations
WHEN NEW.operation_id IS NOT OLD.operation_id
  OR NEW.task_gid IS NOT OLD.task_gid
  OR NEW.operation_kind IS NOT OLD.operation_kind
  OR NEW.expected_identity IS NOT OLD.expected_identity
  OR NEW.schema_version IS NOT OLD.schema_version
  OR NEW.expected_section_gid IS NOT OLD.expected_section_gid
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.migration_reconciliation_required IS NOT OLD.migration_reconciliation_required
  OR NEW.migration_reconciliation_reason IS NOT OLD.migration_reconciliation_reason
BEGIN SELECT RAISE(ABORT, 'operation creation facts are immutable'); END;

CREATE TRIGGER verification_cycles_hold_binding_required_insert
BEFORE INSERT ON verification_cycles
WHEN NEW.completed_at IS NOT NULL
 AND (NEW.route IN ('evidence','human_review') OR NEW.outcome='two-pass-hold')
 AND (
     NEW.hold_content_version_id IS NULL OR NEW.hold_identity IS NULL OR NEW.hold_section_gid IS NULL
     OR NOT EXISTS (
         SELECT 1 FROM content_versions AS version
          WHERE version.content_version_id=NEW.hold_content_version_id
            AND version.operation_id=NEW.operation_id
            AND version.task_gid=NEW.task_gid
            AND version.confirmed=1
            AND version.identity=NEW.hold_identity
     )
 )
BEGIN SELECT RAISE(ABORT, 'hold outcome requires exact content and placement evidence'); END;
CREATE TRIGGER verification_cycles_hold_binding_required_update
BEFORE UPDATE ON verification_cycles
WHEN NEW.completed_at IS NOT NULL
 AND (NEW.route IN ('evidence','human_review') OR NEW.outcome='two-pass-hold')
 AND (
     NEW.hold_content_version_id IS NULL OR NEW.hold_identity IS NULL OR NEW.hold_section_gid IS NULL
     OR NOT EXISTS (
         SELECT 1 FROM content_versions AS version
          WHERE version.content_version_id=NEW.hold_content_version_id
            AND version.operation_id=NEW.operation_id
            AND version.task_gid=NEW.task_gid
            AND version.confirmed=1
            AND version.identity=NEW.hold_identity
     )
 )
BEGIN SELECT RAISE(ABORT, 'hold outcome requires exact content and placement evidence'); END;
CREATE TRIGGER verification_cycles_hold_binding_immutable_update
BEFORE UPDATE ON verification_cycles
WHEN (OLD.hold_content_version_id IS NOT NULL AND NEW.hold_content_version_id IS NOT OLD.hold_content_version_id)
  OR (OLD.hold_identity IS NOT NULL AND NEW.hold_identity IS NOT OLD.hold_identity)
  OR (OLD.hold_section_gid IS NOT NULL AND NEW.hold_section_gid IS NOT OLD.hold_section_gid)
BEGIN SELECT RAISE(ABORT, 'hold baseline is immutable'); END;

CREATE TRIGGER operations_non_material_signoff_required
BEFORE UPDATE ON operations
WHEN NEW.status='completed' AND NEW.terminal_outcome='non_material_checkin'
 AND (
     NEW.inherited_signoff_cycle_id IS NULL
     OR NOT EXISTS (
         SELECT 1 FROM verification_cycles AS cycle
          JOIN content_versions AS version
            ON version.content_version_id=cycle.signed_content_version_id
          WHERE cycle.cycle_id=NEW.inherited_signoff_cycle_id
            AND cycle.task_gid=NEW.task_gid
            AND cycle.outcome='approved'
            AND cycle.completed_at IS NOT NULL
            AND cycle.signed_identity=OLD.expected_identity
            AND version.confirmed=1
            AND version.identity=OLD.expected_identity
            AND version.task_gid=NEW.task_gid
     )
 )
BEGIN SELECT RAISE(ABORT, 'non-material completion requires exact local signed baseline'); END;
"""

_MIGRATION_20 = """
CREATE TABLE service_leases (
    lease_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    task_gid TEXT NOT NULL,
    owner_id TEXT NOT NULL CHECK(length(trim(owner_id)) > 0),
    run_id TEXT NOT NULL CHECK(length(trim(run_id)) > 0),
    acquired_at TEXT NOT NULL,
    renewed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT,
    CHECK ((released_at IS NULL AND release_reason IS NULL)
        OR (released_at IS NOT NULL AND length(trim(release_reason)) > 0))
);
CREATE UNIQUE INDEX service_leases_one_active_operation
    ON service_leases(operation_id) WHERE released_at IS NULL;
CREATE UNIQUE INDEX service_leases_one_active_task
    ON service_leases(task_gid) WHERE released_at IS NULL;
CREATE INDEX service_leases_operation_history
    ON service_leases(operation_id, acquired_at);

CREATE TRIGGER service_leases_creation_immutable_update
BEFORE UPDATE ON service_leases
WHEN NEW.lease_id IS NOT OLD.lease_id
  OR NEW.operation_id IS NOT OLD.operation_id
  OR NEW.task_gid IS NOT OLD.task_gid
  OR NEW.owner_id IS NOT OLD.owner_id
  OR NEW.run_id IS NOT OLD.run_id
  OR NEW.acquired_at IS NOT OLD.acquired_at
BEGIN SELECT RAISE(ABORT, 'service lease creation facts are immutable'); END;

CREATE TRIGGER service_leases_renewal_monotonic_update
BEFORE UPDATE OF renewed_at, expires_at ON service_leases
WHEN OLD.released_at IS NOT NULL
  OR NEW.renewed_at < OLD.renewed_at
  OR NEW.expires_at <= NEW.renewed_at
  OR NEW.expires_at < OLD.expires_at
BEGIN SELECT RAISE(ABORT, 'service lease renewal must be active and monotonic'); END;

CREATE TRIGGER service_leases_release_monotonic_update
BEFORE UPDATE OF released_at, release_reason ON service_leases
WHEN OLD.released_at IS NOT NULL
  OR NEW.released_at IS NULL
  OR NEW.release_reason IS NULL
  OR length(trim(NEW.release_reason)) = 0
BEGIN SELECT RAISE(ABORT, 'service lease release is one-way and requires a reason'); END;

CREATE TRIGGER service_leases_append_only_delete
BEFORE DELETE ON service_leases
BEGIN SELECT RAISE(ABORT, 'service leases are append-only'); END;
"""

_MIGRATION_21 = """
CREATE TABLE service_requests (
    request_id TEXT PRIMARY KEY,
    owner_id TEXT NOT NULL CHECK(length(trim(owner_id)) > 0),
    run_id TEXT NOT NULL CHECK(length(trim(run_id)) > 0),
    command TEXT NOT NULL CHECK(length(trim(command)) > 0),
    request_hash TEXT NOT NULL CHECK(length(trim(request_hash)) > 0),
    status TEXT NOT NULL CHECK(status IN ('pending','completed','uncertain')),
    operation_id TEXT REFERENCES operations(operation_id),
    task_gid TEXT,
    result_json TEXT CHECK(result_json IS NULL OR json_valid(result_json)),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK ((status='pending' AND result_json IS NULL AND completed_at IS NULL)
        OR (status IN ('completed','uncertain') AND result_json IS NOT NULL AND completed_at IS NOT NULL))
);
CREATE INDEX service_requests_run_idx
    ON service_requests(owner_id, run_id, created_at);
CREATE INDEX service_requests_operation_idx
    ON service_requests(operation_id, created_at);

CREATE TRIGGER service_requests_identity_immutable_update
BEFORE UPDATE ON service_requests
WHEN NEW.request_id IS NOT OLD.request_id
  OR NEW.owner_id IS NOT OLD.owner_id
  OR NEW.run_id IS NOT OLD.run_id
  OR NEW.command IS NOT OLD.command
  OR NEW.request_hash IS NOT OLD.request_hash
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'service request identity is immutable'); END;

CREATE TRIGGER service_requests_status_monotonic_update
BEFORE UPDATE OF status, operation_id, task_gid, result_json, completed_at, resolution_result_json, resolved_at
ON service_requests
WHEN NOT (
    (
        OLD.status='pending'
        AND NEW.status IN ('completed','uncertain')
        AND NEW.result_json IS NOT NULL
        AND NEW.completed_at IS NOT NULL
        AND NEW.resolution_result_json IS NULL
        AND NEW.resolved_at IS NULL
    )
    OR
    (
        OLD.status='uncertain'
        AND NEW.status='completed'
        AND NEW.operation_id IS OLD.operation_id
        AND NEW.task_gid IS OLD.task_gid
        AND NEW.result_json IS OLD.result_json
        AND NEW.completed_at IS OLD.completed_at
        AND NEW.resolution_result_json IS NOT NULL
        AND NEW.resolved_at IS NOT NULL
    )
)
BEGIN SELECT RAISE(ABORT, 'service request completion or resolution is invalid'); END;

CREATE TRIGGER service_requests_append_only_delete
BEFORE DELETE ON service_requests
BEGIN SELECT RAISE(ABORT, 'service requests are append-only'); END;
"""


_MIGRATION_22 = """
CREATE TABLE operation_execution_claims (
    operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
    claim_id TEXT NOT NULL UNIQUE,
    command TEXT NOT NULL CHECK(length(trim(command)) > 0),
    hostname TEXT NOT NULL CHECK(length(trim(hostname)) > 0),
    pid INTEGER NOT NULL CHECK(pid > 0),
    process_start TEXT NOT NULL CHECK(length(trim(process_start)) > 0),
    acquired_at TEXT NOT NULL
);

CREATE UNIQUE INDEX write_attempts_one_unresolved_operation
    ON write_attempts(operation_id)
    WHERE outcome IN ('started','uncertain');
CREATE UNIQUE INDEX movement_attempts_one_unresolved_operation
    ON movement_attempts(operation_id)
    WHERE outcome IN ('started','uncertain');
"""

_MIGRATION_23 = """
CREATE TABLE operation_executions (
    execution_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    request_id TEXT,
    command TEXT NOT NULL CHECK(length(trim(command)) > 0),
    baseline_json TEXT NOT NULL CHECK(json_valid(baseline_json)),
    status TEXT NOT NULL CHECK(status IN ('started','completed','uncertain')),
    evidence_json TEXT CHECK(evidence_json IS NULL OR json_valid(evidence_json)),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK ((status='started' AND evidence_json IS NULL AND completed_at IS NULL)
        OR (status IN ('completed','uncertain') AND evidence_json IS NOT NULL AND completed_at IS NOT NULL))
);
CREATE INDEX operation_executions_operation_idx
    ON operation_executions(operation_id, created_at);
CREATE UNIQUE INDEX operation_executions_request_idx
    ON operation_executions(request_id) WHERE request_id IS NOT NULL;

ALTER TABLE operation_execution_claims
    ADD COLUMN execution_id TEXT REFERENCES operation_executions(execution_id);

CREATE TRIGGER operation_executions_identity_immutable_update
BEFORE UPDATE ON operation_executions
WHEN NEW.execution_id IS NOT OLD.execution_id
  OR NEW.operation_id IS NOT OLD.operation_id
  OR NEW.request_id IS NOT OLD.request_id
  OR NEW.command IS NOT OLD.command
  OR NEW.baseline_json IS NOT OLD.baseline_json
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'operation execution identity is immutable'); END;

CREATE TRIGGER operation_executions_status_monotonic_update
BEFORE UPDATE OF status, evidence_json, completed_at ON operation_executions
WHEN OLD.status <> 'started'
  OR NEW.status NOT IN ('completed','uncertain')
  OR NEW.completed_at IS NULL
  OR NEW.evidence_json IS NULL
BEGIN SELECT RAISE(ABORT, 'operation execution completion is one-way'); END;

CREATE TRIGGER operation_executions_append_only_delete
BEFORE DELETE ON operation_executions
BEGIN SELECT RAISE(ABORT, 'operation executions are append-only'); END;
"""

_MIGRATION_24 = """
DROP TRIGGER IF EXISTS operations_non_material_signoff_required;
CREATE TRIGGER operations_non_material_signoff_required
BEFORE UPDATE ON operations
WHEN NEW.status='completed' AND NEW.terminal_outcome='non_material_checkin'
 AND (
     NEW.inherited_signoff_cycle_id IS NULL
     OR NOT EXISTS (
         SELECT 1
           FROM verification_cycles AS cycle
           JOIN content_versions AS signed
             ON signed.content_version_id=cycle.signed_content_version_id
          WHERE cycle.cycle_id=NEW.inherited_signoff_cycle_id
            AND cycle.task_gid=NEW.task_gid
            AND cycle.outcome='approved'
            AND cycle.completed_at IS NOT NULL
            AND signed.confirmed=1
            AND signed.task_gid=NEW.task_gid
            AND signed.identity=cycle.signed_identity
            AND (
                cycle.signed_identity=OLD.expected_identity
                OR EXISTS (
                    SELECT 1
                      FROM operations AS lineage
                      JOIN write_attempts AS candidate_write
                        ON candidate_write.operation_id=lineage.operation_id
                                AND candidate_write.outcome='confirmed'
                      JOIN content_versions AS candidate
                        ON candidate.content_version_id=candidate_write.confirmed_content_version_id
                     WHERE lineage.task_gid=NEW.task_gid
                       AND lineage.status='completed'
                       AND lineage.terminal_outcome='non_material_checkin'
                       AND lineage.inherited_signoff_cycle_id=NEW.inherited_signoff_cycle_id
                       AND candidate_write.intended_identity=OLD.expected_identity
                       AND candidate.confirmed=1
                       AND candidate.task_gid=NEW.task_gid
                       AND candidate.identity=OLD.expected_identity
                )
            )
     )
 )
BEGIN SELECT RAISE(ABORT, 'non-material completion requires exact local signoff lineage'); END;
"""

_MIGRATION_25 = """
CREATE TABLE IF NOT EXISTS dish_inspect_facts (
    fact_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    cycle_id TEXT NOT NULL REFERENCES verification_cycles(cycle_id),
    task_gid TEXT NOT NULL,
    reviewed_content_version_id TEXT NOT NULL REFERENCES content_versions(content_version_id),
    reviewed_identity TEXT NOT NULL CHECK(length(trim(reviewed_identity)) > 0),
    verifier_agent TEXT NOT NULL CHECK(verifier_agent IN ('claude','gpt','codex')),
    run_id TEXT NOT NULL CHECK(length(trim(run_id)) > 0),
    independence_attestation TEXT,
    section_gid TEXT NOT NULL CHECK(length(trim(section_gid)) > 0),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS dish_inspect_facts_cycle_idx
    ON dish_inspect_facts(cycle_id, created_at);
CREATE INDEX IF NOT EXISTS dish_inspect_facts_operation_idx
    ON dish_inspect_facts(operation_id, created_at);
CREATE TRIGGER IF NOT EXISTS dish_inspect_facts_append_only_update
BEFORE UPDATE ON dish_inspect_facts
BEGIN SELECT RAISE(ABORT, 'dish inspect facts are append-only'); END;
CREATE TRIGGER IF NOT EXISTS dish_inspect_facts_append_only_delete
BEFORE DELETE ON dish_inspect_facts
BEGIN SELECT RAISE(ABORT, 'dish inspect facts are append-only'); END;
"""


_MIGRATION_26 = """
CREATE TABLE IF NOT EXISTS planning_reopen_attempts (
    attempt_id TEXT PRIMARY KEY,
    task_gid TEXT NOT NULL CHECK(length(trim(task_gid)) > 0),
    request_id TEXT,
    expected_identity TEXT NOT NULL CHECK(length(trim(expected_identity)) > 0),
    expected_section_gid TEXT,
    expected_modified_at TEXT,
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    actor_run_id TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('started','confirmed','not_applied','uncertain')),
    created_at TEXT NOT NULL,
    finished_at TEXT,
    confirmed_modified_at TEXT,
    CHECK ((outcome='started' AND finished_at IS NULL AND confirmed_modified_at IS NULL)
        OR (outcome IN ('confirmed','not_applied','uncertain') AND finished_at IS NOT NULL)),
    UNIQUE(request_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS planning_reopen_attempts_one_unresolved_task
    ON planning_reopen_attempts(task_gid) WHERE outcome IN ('started','uncertain');
CREATE INDEX IF NOT EXISTS planning_reopen_attempts_task_history
    ON planning_reopen_attempts(task_gid, created_at);
CREATE TRIGGER IF NOT EXISTS planning_reopen_attempts_identity_immutable_update
BEFORE UPDATE ON planning_reopen_attempts
WHEN NEW.attempt_id IS NOT OLD.attempt_id
  OR NEW.task_gid IS NOT OLD.task_gid
  OR NEW.request_id IS NOT OLD.request_id
  OR NEW.expected_identity IS NOT OLD.expected_identity
  OR NEW.expected_section_gid IS NOT OLD.expected_section_gid
  OR NEW.expected_modified_at IS NOT OLD.expected_modified_at
  OR NEW.reason IS NOT OLD.reason
  OR NEW.actor_run_id IS NOT OLD.actor_run_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'planning reopen attempt identity is immutable'); END;
CREATE TRIGGER IF NOT EXISTS planning_reopen_attempts_status_monotonic_update
BEFORE UPDATE OF outcome, finished_at, confirmed_modified_at ON planning_reopen_attempts
WHEN OLD.outcome <> 'started'
  OR NEW.outcome NOT IN ('confirmed','not_applied','uncertain')
  OR NEW.finished_at IS NULL
BEGIN SELECT RAISE(ABORT, 'planning reopen attempt completion is one-way'); END;
CREATE TRIGGER IF NOT EXISTS planning_reopen_attempts_append_only_delete
BEFORE DELETE ON planning_reopen_attempts
BEGIN SELECT RAISE(ABORT, 'planning reopen attempts are append-only'); END;
"""

_MIGRATION_27 = """
ALTER TABLE operation_executions ADD COLUMN resolution_evidence_json TEXT
    CHECK(resolution_evidence_json IS NULL OR json_valid(resolution_evidence_json));
ALTER TABLE operation_executions ADD COLUMN resolved_at TEXT;
ALTER TABLE service_requests ADD COLUMN resolution_result_json TEXT
    CHECK(resolution_result_json IS NULL OR json_valid(resolution_result_json));
ALTER TABLE service_requests ADD COLUMN resolved_at TEXT;

DROP TRIGGER operation_executions_status_monotonic_update;
CREATE TRIGGER operation_executions_status_monotonic_update
BEFORE UPDATE OF status, evidence_json, completed_at, resolution_evidence_json, resolved_at
ON operation_executions
WHEN NOT (
    (
        OLD.status='started'
        AND NEW.status IN ('completed','uncertain')
        AND NEW.completed_at IS NOT NULL
        AND NEW.evidence_json IS NOT NULL
        AND NEW.resolution_evidence_json IS NULL
        AND NEW.resolved_at IS NULL
    )
    OR
    (
        OLD.status='uncertain'
        AND NEW.status='completed'
        AND NEW.evidence_json IS OLD.evidence_json
        AND NEW.completed_at IS OLD.completed_at
        AND NEW.resolution_evidence_json IS NOT NULL
        AND NEW.resolved_at IS NOT NULL
    )
)
BEGIN SELECT RAISE(ABORT, 'operation execution completion or resolution is invalid'); END;

DROP TRIGGER service_requests_status_monotonic_update;
CREATE TRIGGER service_requests_status_monotonic_update
BEFORE UPDATE OF status, operation_id, task_gid, result_json, completed_at, resolution_result_json, resolved_at
ON service_requests
WHEN NOT (
    (
        OLD.status='pending'
        AND NEW.status IN ('completed','uncertain')
        AND NEW.result_json IS NOT NULL
        AND NEW.completed_at IS NOT NULL
        AND NEW.resolution_result_json IS NULL
        AND NEW.resolved_at IS NULL
    )
    OR
    (
        OLD.status='uncertain'
        AND NEW.status='completed'
        AND NEW.operation_id IS OLD.operation_id
        AND NEW.task_gid IS OLD.task_gid
        AND NEW.result_json IS OLD.result_json
        AND NEW.completed_at IS OLD.completed_at
        AND NEW.resolution_result_json IS NOT NULL
        AND NEW.resolved_at IS NOT NULL
    )
)
BEGIN SELECT RAISE(ABORT, 'service request completion or resolution is invalid'); END;
"""


_MIGRATION_28 = """
DROP TRIGGER operations_creation_facts_immutable_update;
UPDATE operations
   SET migration_reconciliation_required=1,
       migration_reconciliation_reason=(
           CASE
             WHEN length(trim(COALESCE(migration_reconciliation_reason, ''))) > 0
             THEN migration_reconciliation_reason
             ELSE 'legacy change operation predates atomic change_intent evidence'
           END
       )
 WHERE operation_kind='change'
   AND migration_reconciliation_required != 1
   AND NOT EXISTS (
       SELECT 1
         FROM operation_steps AS step
        WHERE step.operation_id=operations.operation_id
          AND step.step_name='change_intent'
          AND step.completed_at IS NOT NULL
          AND json_valid(step.intended_json)
          AND json_extract(step.intended_json, '$.level') IN ('small','large')
          AND length(trim(json_extract(step.intended_json, '$.reason'))) > 0
   );
CREATE TRIGGER operations_creation_facts_immutable_update
BEFORE UPDATE ON operations
WHEN NEW.operation_id IS NOT OLD.operation_id
  OR NEW.task_gid IS NOT OLD.task_gid
  OR NEW.operation_kind IS NOT OLD.operation_kind
  OR NEW.expected_identity IS NOT OLD.expected_identity
  OR NEW.schema_version IS NOT OLD.schema_version
  OR NEW.expected_section_gid IS NOT OLD.expected_section_gid
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.migration_reconciliation_required IS NOT OLD.migration_reconciliation_required
  OR NEW.migration_reconciliation_reason IS NOT OLD.migration_reconciliation_reason
BEGIN SELECT RAISE(ABORT, 'operation creation facts are immutable'); END;
"""


_MIGRATION_29 = """
CREATE TABLE IF NOT EXISTS backup_creations (
    request_id TEXT PRIMARY KEY REFERENCES service_requests(request_id),
    backup_id TEXT NOT NULL UNIQUE CHECK(length(trim(backup_id)) > 0),
    status TEXT NOT NULL CHECK(status IN ('reserved','completed')),
    sha256 TEXT,
    size_bytes INTEGER,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    CHECK ((status='reserved' AND sha256 IS NULL AND size_bytes IS NULL AND completed_at IS NULL)
        OR (status='completed' AND length(trim(sha256)) > 0 AND size_bytes >= 0 AND completed_at IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS backup_creations_status_idx
    ON backup_creations(status, created_at);

CREATE TRIGGER IF NOT EXISTS backup_creations_identity_immutable_update
BEFORE UPDATE ON backup_creations
WHEN NEW.request_id IS NOT OLD.request_id
  OR NEW.backup_id IS NOT OLD.backup_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'backup creation identity is immutable'); END;

CREATE TRIGGER IF NOT EXISTS backup_creations_status_monotonic_update
BEFORE UPDATE OF status, sha256, size_bytes, completed_at ON backup_creations
WHEN OLD.status <> 'reserved'
  OR NEW.status <> 'completed'
  OR NEW.sha256 IS NULL
  OR length(trim(NEW.sha256)) = 0
  OR NEW.size_bytes IS NULL
  OR NEW.size_bytes < 0
  OR NEW.completed_at IS NULL
BEGIN SELECT RAISE(ABORT, 'backup creation completion is one-way'); END;

CREATE TRIGGER IF NOT EXISTS backup_creations_append_only_delete
BEFORE DELETE ON backup_creations
BEGIN SELECT RAISE(ABORT, 'backup creations are append-only'); END;

INSERT OR IGNORE INTO backup_creations(
    request_id, backup_id, status, sha256, size_bytes, created_at, completed_at
)
SELECT request_id,
       json_extract(result_json, '$.data.backup.backup_id'),
       'completed',
       json_extract(result_json, '$.data.backup.sha256'),
       json_extract(result_json, '$.data.backup.size_bytes'),
       created_at,
       completed_at
  FROM service_requests
 WHERE command='backup-create'
   AND status='completed'
   AND json_extract(result_json, '$.ok')=1
   AND length(trim(json_extract(result_json, '$.data.backup.backup_id'))) > 0
   AND length(trim(json_extract(result_json, '$.data.backup.sha256'))) > 0
   AND json_type(result_json, '$.data.backup.size_bytes')='integer';
"""


_MIGRATION_30 = """
DROP TRIGGER planning_reopen_attempts_status_monotonic_update;
CREATE TRIGGER planning_reopen_attempts_status_monotonic_update
BEFORE UPDATE OF outcome, finished_at, confirmed_modified_at ON planning_reopen_attempts
WHEN NOT (
       (OLD.outcome='started' AND NEW.outcome IN ('confirmed','not_applied','uncertain'))
    OR (OLD.outcome='uncertain' AND NEW.outcome='confirmed')
)
 OR NEW.finished_at IS NULL
BEGIN SELECT RAISE(ABORT, 'planning reopen attempt completion is one-way'); END;

-- A Planning reopen request remains recoverable while its external outcome is
-- unresolved. Older databases may have persisted BACKEND_UNCERTAIN as a
-- terminal request result; return only those exact rows to pending so startup
-- or exact replay can converge them from the linked attempt.
DROP TRIGGER service_requests_status_monotonic_update;
UPDATE service_requests
   SET status='pending', result_json=NULL, completed_at=NULL
 WHERE command='reopen-planning'
   AND status='uncertain'
   AND EXISTS (
       SELECT 1 FROM planning_reopen_attempts AS attempt
        WHERE attempt.request_id=service_requests.request_id
          AND attempt.outcome IN ('started','uncertain')
   );
CREATE TRIGGER service_requests_status_monotonic_update
BEFORE UPDATE OF status, operation_id, task_gid, result_json, completed_at, resolution_result_json, resolved_at
ON service_requests
WHEN NOT (
    (
        OLD.status='pending'
        AND NEW.status IN ('completed','uncertain')
        AND NEW.result_json IS NOT NULL
        AND NEW.completed_at IS NOT NULL
        AND NEW.resolution_result_json IS NULL
        AND NEW.resolved_at IS NULL
    )
    OR
    (
        OLD.status='uncertain'
        AND NEW.status='completed'
        AND NEW.operation_id IS OLD.operation_id
        AND NEW.task_gid IS OLD.task_gid
        AND NEW.result_json IS OLD.result_json
        AND NEW.completed_at IS OLD.completed_at
        AND NEW.resolution_result_json IS NOT NULL
        AND NEW.resolved_at IS NOT NULL
    )
)
BEGIN SELECT RAISE(ABORT, 'service request completion or resolution is invalid'); END;

"""


_MIGRATION_31 = """
ALTER TABLE service_leases ADD COLUMN lease_kind TEXT
    CHECK(lease_kind IS NULL OR lease_kind IN ('actor','admin_request'));
ALTER TABLE service_leases ADD COLUMN actor_attempt_seq INTEGER
    CHECK(actor_attempt_seq IS NULL OR actor_attempt_seq > 0);
ALTER TABLE service_leases ADD COLUMN context_cycle_id TEXT
    REFERENCES verification_cycles(cycle_id);

CREATE UNIQUE INDEX service_leases_actor_attempt_sequence_unique
    ON service_leases(task_gid, actor_attempt_seq)
    WHERE lease_kind='actor';

CREATE TRIGGER service_leases_attempt_context_insert
BEFORE INSERT ON service_leases
WHEN NEW.lease_kind IS NULL
  OR (NEW.lease_kind='actor' AND NEW.actor_attempt_seq IS NULL)
  OR (NEW.lease_kind='admin_request' AND NEW.actor_attempt_seq IS NOT NULL)
  OR (NEW.lease_kind='admin_request' AND NEW.context_cycle_id IS NOT NULL)
  OR (NEW.lease_kind='actor' AND NEW.actor_attempt_seq <> (
        SELECT COALESCE(MAX(actor_attempt_seq), 0) + 1
          FROM service_leases
         WHERE task_gid=NEW.task_gid AND lease_kind='actor'
     ))
  OR (NEW.context_cycle_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM verification_cycles AS cycle
         WHERE cycle.cycle_id=NEW.context_cycle_id
           AND cycle.operation_id=NEW.operation_id
           AND cycle.task_gid=NEW.task_gid
     ))
BEGIN SELECT RAISE(ABORT, 'service lease attempt context is invalid'); END;

CREATE TRIGGER service_leases_attempt_context_immutable_update
BEFORE UPDATE ON service_leases
WHEN NEW.lease_kind IS NOT OLD.lease_kind
  OR NEW.actor_attempt_seq IS NOT OLD.actor_attempt_seq
  OR NEW.context_cycle_id IS NOT OLD.context_cycle_id
BEGIN SELECT RAISE(ABORT, 'service lease attempt context is immutable'); END;
"""


_MIGRATION_32 = """
ALTER TABLE operations ADD COLUMN successor_claim_mode TEXT NOT NULL DEFAULT 'none'
    CHECK(successor_claim_mode IN ('none','stage_actor','verifier'));

CREATE TABLE abandonment_attempts (
    abandonment_id TEXT PRIMARY KEY,
    task_gid TEXT NOT NULL,
    source_operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    source_lease_id TEXT NOT NULL REFERENCES service_leases(lease_id),
    abandoned_owner_id TEXT NOT NULL CHECK(length(trim(abandoned_owner_id)) > 0),
    abandoned_run_id TEXT NOT NULL CHECK(length(trim(abandoned_run_id)) > 0),
    attempt_cycle_id TEXT REFERENCES verification_cycles(cycle_id),
    status TEXT NOT NULL CHECK(status IN (
        'started','awaiting_hold_resolution','blocked_manual_reconciliation',
        'awaiting_successor_claim','completed'
    )),
    outcome TEXT CHECK(outcome IS NULL OR outcome IN (
        'restart_prepared','hold_preserved','restarted',
        'committed_finalized','route_preserved','blocked_manual_reconciliation'
    )),
    successor_operation_id TEXT REFERENCES operations(operation_id),
    successor_cycle_id TEXT REFERENCES verification_cycles(cycle_id),
    continuation_operation_id TEXT REFERENCES operations(operation_id),
    continuation_cycle_id TEXT REFERENCES verification_cycles(cycle_id),
    current_execution_id TEXT REFERENCES operation_executions(execution_id),
    reason TEXT NOT NULL CHECK(length(trim(reason)) > 0),
    latest_result_json TEXT CHECK(latest_result_json IS NULL OR json_valid(latest_result_json)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);
CREATE UNIQUE INDEX abandonment_attempts_one_active_per_task
    ON abandonment_attempts(task_gid)
    WHERE status != 'completed';
CREATE UNIQUE INDEX abandonment_attempts_exact_attempt_unique
    ON abandonment_attempts(
        source_operation_id, source_lease_id, abandoned_owner_id,
        abandoned_run_id, COALESCE(attempt_cycle_id, '')
    );
CREATE INDEX abandonment_attempts_source_idx
    ON abandonment_attempts(source_operation_id, created_at);
CREATE INDEX abandonment_attempts_status_idx
    ON abandonment_attempts(status, updated_at);

CREATE TABLE operation_successions (
    succession_id TEXT PRIMARY KEY,
    task_gid TEXT NOT NULL,
    source_operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    successor_operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    transition_type TEXT NOT NULL CHECK(transition_type='agent_abandonment'),
    transition_reason TEXT NOT NULL CHECK(length(trim(transition_reason)) > 0),
    source_cycle_id TEXT REFERENCES verification_cycles(cycle_id),
    successor_cycle_id TEXT REFERENCES verification_cycles(cycle_id),
    source_content_version_id TEXT NOT NULL REFERENCES content_versions(content_version_id),
    successor_content_version_id TEXT NOT NULL REFERENCES content_versions(content_version_id),
    candidate_transfer_kind TEXT NOT NULL CHECK(candidate_transfer_kind IN (
        'restored_stage_baseline','inherited_confirmed_candidate',
        'recovered_pre_signoff_candidate','confirmed_small_correction'
    )),
    abandonment_id TEXT NOT NULL UNIQUE REFERENCES abandonment_attempts(abandonment_id),
    created_at TEXT NOT NULL,
    UNIQUE(source_operation_id),
    UNIQUE(successor_operation_id),
    CHECK(source_operation_id != successor_operation_id)
);
CREATE INDEX operation_successions_task_idx
    ON operation_successions(task_gid, created_at);

CREATE TRIGGER operations_successor_claim_mode_transition
BEFORE UPDATE OF successor_claim_mode ON operations
WHEN NEW.successor_claim_mode IS NOT OLD.successor_claim_mode
 AND NOT (
     OLD.status='open'
     AND NEW.status='open'
     AND OLD.successor_claim_mode IN ('stage_actor','verifier')
     AND NEW.successor_claim_mode='none'
 )
BEGIN SELECT RAISE(ABORT, 'operation successor claim mode transition is invalid'); END;

CREATE TRIGGER abandonment_attempts_authority_insert
BEFORE INSERT ON abandonment_attempts
WHEN NOT EXISTS (
        SELECT 1
          FROM operations AS operation
         WHERE operation.operation_id=NEW.source_operation_id
           AND operation.task_gid=NEW.task_gid
           AND operation.status IN ('open','uncertain')
           AND operation.phase != 'terminal'
     )
  OR NOT EXISTS (
        SELECT 1
          FROM service_leases AS lease
         WHERE lease.lease_id=NEW.source_lease_id
           AND lease.operation_id=NEW.source_operation_id
           AND lease.task_gid=NEW.task_gid
           AND lease.owner_id=NEW.abandoned_owner_id
           AND lease.run_id=NEW.abandoned_run_id
           AND lease.lease_kind='actor'
           AND lease.actor_attempt_seq IS NOT NULL
           AND lease.context_cycle_id IS NEW.attempt_cycle_id
           AND (
               lease.released_at IS NOT NULL
               OR julianday(lease.expires_at) <= julianday(NEW.created_at)
           )
     )
  OR EXISTS (
        SELECT 1
          FROM service_leases AS selected
          JOIN service_leases AS later
            ON later.task_gid=selected.task_gid
           AND later.lease_kind='actor'
           AND later.actor_attempt_seq > selected.actor_attempt_seq
         WHERE selected.lease_id=NEW.source_lease_id
     )
  OR EXISTS (
        SELECT 1 FROM operation_successions
         WHERE source_operation_id=NEW.source_operation_id
     )
  OR (
        NEW.attempt_cycle_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM verification_cycles AS cycle
             WHERE cycle.cycle_id=NEW.attempt_cycle_id
               AND cycle.operation_id=NEW.source_operation_id
               AND cycle.task_gid=NEW.task_gid
               AND cycle.run_id=NEW.abandoned_run_id
        )
     )
  OR (
        NEW.current_execution_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM operation_executions AS execution
             WHERE execution.execution_id=NEW.current_execution_id
               AND execution.operation_id=NEW.source_operation_id
        )
     )
BEGIN SELECT RAISE(ABORT, 'abandonment attempt authority binding is invalid'); END;

CREATE TRIGGER abandonment_attempts_initial_state_insert
BEFORE INSERT ON abandonment_attempts
WHEN NEW.status != 'started'
  OR NEW.outcome IS NOT NULL
  OR NEW.successor_operation_id IS NOT NULL
  OR NEW.successor_cycle_id IS NOT NULL
  OR NEW.continuation_operation_id IS NOT NULL
  OR NEW.continuation_cycle_id IS NOT NULL
  OR NEW.completed_at IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'abandonment attempt initial state is invalid'); END;

CREATE TRIGGER abandonment_attempts_identity_immutable_update
BEFORE UPDATE ON abandonment_attempts
WHEN NEW.abandonment_id IS NOT OLD.abandonment_id
  OR NEW.task_gid IS NOT OLD.task_gid
  OR NEW.source_operation_id IS NOT OLD.source_operation_id
  OR NEW.source_lease_id IS NOT OLD.source_lease_id
  OR NEW.abandoned_owner_id IS NOT OLD.abandoned_owner_id
  OR NEW.abandoned_run_id IS NOT OLD.abandoned_run_id
  OR NEW.attempt_cycle_id IS NOT OLD.attempt_cycle_id
  OR NEW.reason IS NOT OLD.reason
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'abandonment attempt identity is immutable'); END;

CREATE TRIGGER abandonment_attempts_status_transition_update
BEFORE UPDATE OF status ON abandonment_attempts
WHEN NEW.status IS NOT OLD.status
 AND NOT (
     (OLD.status='started' AND NEW.status IN (
         'awaiting_hold_resolution','blocked_manual_reconciliation',
         'awaiting_successor_claim','completed'
     ))
     OR (OLD.status='awaiting_hold_resolution' AND NEW.status IN (
         'blocked_manual_reconciliation','awaiting_successor_claim','completed'
     ))
     OR (OLD.status='blocked_manual_reconciliation' AND NEW.status IN (
         'started','awaiting_hold_resolution','awaiting_successor_claim','completed'
     ))
     OR (OLD.status='awaiting_successor_claim' AND NEW.status IN (
         'blocked_manual_reconciliation','completed'
     ))
 )
BEGIN SELECT RAISE(ABORT, 'abandonment attempt status transition is invalid'); END;

CREATE TRIGGER abandonment_attempts_state_update
BEFORE UPDATE ON abandonment_attempts
WHEN NEW.updated_at < OLD.updated_at
  OR (OLD.completed_at IS NOT NULL AND NEW.completed_at IS NOT OLD.completed_at)
  OR (NEW.status='completed' AND (NEW.completed_at IS NULL OR NEW.outcome IS NULL))
  OR (NEW.status!='completed' AND NEW.completed_at IS NOT NULL)
  OR (NEW.status='awaiting_successor_claim' AND NEW.successor_operation_id IS NULL)
  OR (NEW.successor_cycle_id IS NOT NULL AND NEW.successor_operation_id IS NULL)
  OR (NEW.continuation_cycle_id IS NOT NULL AND NEW.continuation_operation_id IS NULL)
  OR (
        NEW.successor_operation_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM operations AS successor
             WHERE successor.operation_id=NEW.successor_operation_id
               AND successor.task_gid=NEW.task_gid
               AND successor.operation_id != NEW.source_operation_id
        )
     )
  OR (
        NEW.successor_cycle_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM verification_cycles AS cycle
             WHERE cycle.cycle_id=NEW.successor_cycle_id
               AND cycle.operation_id=NEW.successor_operation_id
               AND cycle.task_gid=NEW.task_gid
        )
     )
  OR (
        NEW.continuation_operation_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM operations AS continuation
             WHERE continuation.operation_id=NEW.continuation_operation_id
               AND continuation.task_gid=NEW.task_gid
        )
     )
  OR (
        NEW.continuation_cycle_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM verification_cycles AS cycle
             WHERE cycle.cycle_id=NEW.continuation_cycle_id
               AND cycle.operation_id=NEW.continuation_operation_id
               AND cycle.task_gid=NEW.task_gid
        )
     )
  OR (
        NEW.current_execution_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM operation_executions AS execution
             WHERE execution.execution_id=NEW.current_execution_id
               AND execution.operation_id=NEW.source_operation_id
        )
     )
BEGIN SELECT RAISE(ABORT, 'abandonment attempt state is invalid'); END;

CREATE TRIGGER abandonment_attempts_completed_immutable_update
BEFORE UPDATE ON abandonment_attempts
WHEN OLD.status='completed'
BEGIN SELECT RAISE(ABORT, 'completed abandonment attempt is immutable'); END;
CREATE TRIGGER abandonment_attempts_append_only_delete
BEFORE DELETE ON abandonment_attempts
BEGIN SELECT RAISE(ABORT, 'abandonment attempts are append-only'); END;

CREATE TRIGGER operation_successions_binding_insert
BEFORE INSERT ON operation_successions
WHEN NOT EXISTS (
        SELECT 1 FROM operations AS source
         WHERE source.operation_id=NEW.source_operation_id
           AND source.task_gid=NEW.task_gid
           AND source.status='cancelled'
           AND source.phase='terminal'
           AND source.terminal_outcome='agent_abandoned'
     )
  OR NOT EXISTS (
        SELECT 1 FROM operations AS successor
         WHERE successor.operation_id=NEW.successor_operation_id
           AND successor.task_gid=NEW.task_gid
           AND successor.status='open'
           AND successor.phase != 'terminal'
           AND successor.successor_claim_mode IN ('stage_actor','verifier')
           AND successor.content_write_completed_at IS NULL
     )
  OR NOT EXISTS (
        SELECT 1 FROM abandonment_attempts AS abandonment
         WHERE abandonment.abandonment_id=NEW.abandonment_id
           AND abandonment.task_gid=NEW.task_gid
           AND abandonment.source_operation_id=NEW.source_operation_id
           AND abandonment.successor_operation_id=NEW.successor_operation_id
           AND abandonment.successor_cycle_id IS NEW.successor_cycle_id
           AND abandonment.status='awaiting_successor_claim'
     )
  OR NOT EXISTS (
        SELECT 1 FROM content_versions AS source_version
         WHERE source_version.content_version_id=NEW.source_content_version_id
           AND source_version.task_gid=NEW.task_gid
           AND source_version.confirmed=1
     )
  OR NOT EXISTS (
        SELECT 1
          FROM content_versions AS source_version
          JOIN content_versions AS successor_version
            ON successor_version.content_version_id=NEW.successor_content_version_id
          JOIN operations AS successor
            ON successor.operation_id=NEW.successor_operation_id
         WHERE source_version.content_version_id=NEW.source_content_version_id
           AND source_version.task_gid=NEW.task_gid
           AND source_version.confirmed=1
           AND successor_version.task_gid=NEW.task_gid
           AND successor_version.operation_id=NEW.successor_operation_id
           AND successor_version.boundary='successor_baseline'
           AND successor_version.confirmed=1
           AND successor_version.identity=source_version.identity
           AND successor_version.title=source_version.title
           AND successor_version.notes=source_version.notes
           AND successor.expected_identity=successor_version.identity
     )
  OR (
        NEW.source_cycle_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM verification_cycles AS cycle
             WHERE cycle.cycle_id=NEW.source_cycle_id
               AND cycle.operation_id=NEW.source_operation_id
               AND cycle.task_gid=NEW.task_gid
        )
     )
  OR (
        NEW.successor_cycle_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM verification_cycles AS cycle
             WHERE cycle.cycle_id=NEW.successor_cycle_id
               AND cycle.operation_id=NEW.successor_operation_id
               AND cycle.task_gid=NEW.task_gid
        )
     )
BEGIN SELECT RAISE(ABORT, 'operation succession binding is invalid'); END;

CREATE TRIGGER operation_successions_acyclic_insert
BEFORE INSERT ON operation_successions
BEGIN
    WITH RECURSIVE ancestors(operation_id) AS (
        SELECT NEW.source_operation_id
        UNION ALL
        SELECT succession.source_operation_id
          FROM operation_successions AS succession
          JOIN ancestors
            ON succession.successor_operation_id=ancestors.operation_id
    )
    SELECT RAISE(ABORT, 'operation succession graph must be acyclic')
     WHERE EXISTS (
         SELECT 1 FROM ancestors
          WHERE operation_id=NEW.successor_operation_id
     );
END;

CREATE TRIGGER operation_successions_append_only_update
BEFORE UPDATE ON operation_successions
BEGIN SELECT RAISE(ABORT, 'operation successions are append-only'); END;
CREATE TRIGGER operation_successions_append_only_delete
BEFORE DELETE ON operation_successions
BEGIN SELECT RAISE(ABORT, 'operation successions are append-only'); END;

CREATE TRIGGER verification_cycles_abandoned_insert
BEFORE INSERT ON verification_cycles
WHEN NEW.outcome='abandoned'
BEGIN SELECT RAISE(ABORT, 'abandoned Verification outcome must close an existing incomplete cycle'); END;

CREATE TRIGGER verification_cycles_abandoned_update
BEFORE UPDATE OF outcome, completed_at, signed_content_version_id, signed_identity
ON verification_cycles
WHEN NEW.outcome='abandoned'
 AND (
     OLD.completed_at IS NOT NULL
     OR OLD.outcome IS NOT NULL
     OR NEW.completed_at IS NULL
     OR NEW.signed_content_version_id IS NOT NULL
     OR NEW.signed_identity IS NOT NULL
     OR NOT EXISTS (
         SELECT 1 FROM operations AS operation
          WHERE operation.operation_id=NEW.operation_id
            AND operation.status='cancelled'
            AND operation.terminal_outcome='agent_abandoned'
     )
 )
BEGIN SELECT RAISE(ABORT, 'abandoned Verification cycle binding is invalid'); END;

CREATE TRIGGER operations_agent_abandoned_immutable_update
BEFORE UPDATE ON operations
WHEN OLD.status='cancelled' AND OLD.terminal_outcome='agent_abandoned'
BEGIN SELECT RAISE(ABORT, 'agent-abandoned operation is immutable'); END;

CREATE TRIGGER agent_abandoned_operation_steps_insert
BEFORE INSERT ON operation_steps
WHEN EXISTS (
    SELECT 1 FROM operations
     WHERE operation_id=NEW.operation_id
       AND status='cancelled' AND terminal_outcome='agent_abandoned'
)
BEGIN SELECT RAISE(ABORT, 'agent-abandoned operation cannot receive workflow steps'); END;
CREATE TRIGGER agent_abandoned_actor_facts_insert
BEFORE INSERT ON operation_actor_facts
WHEN EXISTS (
    SELECT 1 FROM operations
     WHERE operation_id=NEW.operation_id
       AND status='cancelled' AND terminal_outcome='agent_abandoned'
)
BEGIN SELECT RAISE(ABORT, 'agent-abandoned operation cannot receive actor facts'); END;
CREATE TRIGGER agent_abandoned_write_attempts_insert
BEFORE INSERT ON write_attempts
WHEN EXISTS (
    SELECT 1 FROM operations
     WHERE operation_id=NEW.operation_id
       AND status='cancelled' AND terminal_outcome='agent_abandoned'
)
BEGIN SELECT RAISE(ABORT, 'agent-abandoned operation cannot receive write attempts'); END;
CREATE TRIGGER agent_abandoned_write_attempts_update
BEFORE UPDATE ON write_attempts
WHEN EXISTS (
    SELECT 1 FROM operations
     WHERE operation_id=OLD.operation_id
       AND status='cancelled' AND terminal_outcome='agent_abandoned'
)
BEGIN SELECT RAISE(ABORT, 'agent-abandoned operation write evidence is immutable'); END;
CREATE TRIGGER agent_abandoned_movement_attempts_insert
BEFORE INSERT ON movement_attempts
WHEN EXISTS (
    SELECT 1 FROM operations
     WHERE operation_id=NEW.operation_id
       AND status='cancelled' AND terminal_outcome='agent_abandoned'
)
BEGIN SELECT RAISE(ABORT, 'agent-abandoned operation cannot receive movement attempts'); END;
CREATE TRIGGER agent_abandoned_movement_attempts_update
BEFORE UPDATE ON movement_attempts
WHEN EXISTS (
    SELECT 1 FROM operations
     WHERE operation_id=OLD.operation_id
       AND status='cancelled' AND terminal_outcome='agent_abandoned'
)
BEGIN SELECT RAISE(ABORT, 'agent-abandoned operation movement evidence is immutable'); END;
CREATE TRIGGER agent_abandoned_content_versions_insert
BEFORE INSERT ON content_versions
WHEN NEW.operation_id IS NOT NULL
 AND EXISTS (
    SELECT 1 FROM operations
     WHERE operation_id=NEW.operation_id
       AND status='cancelled' AND terminal_outcome='agent_abandoned'
 )
BEGIN SELECT RAISE(ABORT, 'agent-abandoned operation cannot receive content versions'); END;
"""

_MIGRATION_33 = """
DROP TRIGGER operations_creation_facts_immutable_update;
CREATE TRIGGER operations_creation_facts_immutable_update
BEFORE UPDATE ON operations
WHEN NEW.operation_id IS NOT OLD.operation_id
  OR NEW.task_gid IS NOT OLD.task_gid
  OR NEW.operation_kind IS NOT OLD.operation_kind
  OR NEW.expected_identity IS NOT OLD.expected_identity
  OR (
       NEW.schema_version IS NOT OLD.schema_version
       AND NOT (
           OLD.status='open' AND NEW.status='open'
           AND OLD.phase='prepare_required' AND NEW.phase='prepare_required'
           AND OLD.successor_claim_mode='stage_actor'
           AND NEW.successor_claim_mode='none'
           AND OLD.run_id IS NULL AND NEW.run_id IS NOT NULL
           AND OLD.operation_kind IN ('planning','initial','change')
       )
     )
  OR NEW.expected_section_gid IS NOT OLD.expected_section_gid
  OR NEW.created_at IS NOT OLD.created_at
  OR NEW.migration_reconciliation_required IS NOT OLD.migration_reconciliation_required
  OR NEW.migration_reconciliation_reason IS NOT OLD.migration_reconciliation_reason
BEGIN SELECT RAISE(ABORT, 'operation creation facts are immutable'); END;
"""

_MIGRATION_34 = """
CREATE TABLE planning_intent_challenges (
    challenge_id TEXT PRIMARY KEY,
    created_request_id TEXT NOT NULL UNIQUE REFERENCES service_requests(request_id),
    owner_id TEXT NOT NULL CHECK(length(trim(owner_id)) > 0),
    run_id TEXT NOT NULL CHECK(length(trim(run_id)) > 0),
    task_gid TEXT NOT NULL CHECK(length(trim(task_gid)) > 0),
    agent TEXT NOT NULL CHECK(length(trim(agent)) > 0),
    target_hash TEXT NOT NULL CHECK(length(trim(target_hash)) > 0),
    status TEXT NOT NULL CHECK(status IN ('issued','claimed','consumed')),
    claimed_request_id TEXT UNIQUE REFERENCES service_requests(request_id),
    intent_basis TEXT CHECK(intent_basis IN ('user_requested','agent_override')),
    override_reason TEXT,
    operation_id TEXT UNIQUE REFERENCES operations(operation_id),
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    consumed_at TEXT,
    CHECK (
        (status='issued'
         AND claimed_request_id IS NULL
         AND intent_basis IS NULL
         AND override_reason IS NULL
         AND operation_id IS NULL
         AND claimed_at IS NULL
         AND consumed_at IS NULL)
        OR
        (status='claimed'
         AND claimed_request_id IS NOT NULL
         AND intent_basis IS NOT NULL
         AND operation_id IS NULL
         AND claimed_at IS NOT NULL
         AND consumed_at IS NULL
         AND ((intent_basis='user_requested' AND override_reason IS NULL)
              OR (intent_basis='agent_override'
                  AND length(trim(COALESCE(override_reason,''))) > 0)))
        OR
        (status='consumed'
         AND claimed_request_id IS NOT NULL
         AND intent_basis IS NOT NULL
         AND operation_id IS NOT NULL
         AND claimed_at IS NOT NULL
         AND consumed_at IS NOT NULL
         AND ((intent_basis='user_requested' AND override_reason IS NULL)
              OR (intent_basis='agent_override'
                  AND length(trim(COALESCE(override_reason,''))) > 0)))
    )
);
CREATE INDEX planning_intent_challenges_principal_idx
    ON planning_intent_challenges(owner_id, run_id, task_gid, created_at);

CREATE TRIGGER planning_intent_challenges_identity_immutable_update
BEFORE UPDATE ON planning_intent_challenges
WHEN NEW.challenge_id IS NOT OLD.challenge_id
  OR NEW.created_request_id IS NOT OLD.created_request_id
  OR NEW.owner_id IS NOT OLD.owner_id
  OR NEW.run_id IS NOT OLD.run_id
  OR NEW.task_gid IS NOT OLD.task_gid
  OR NEW.agent IS NOT OLD.agent
  OR NEW.target_hash IS NOT OLD.target_hash
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'Planning intent challenge identity is immutable'); END;

CREATE TRIGGER planning_intent_challenges_transition_monotonic_update
BEFORE UPDATE OF status, claimed_request_id, intent_basis, override_reason,
                 operation_id, claimed_at, consumed_at
ON planning_intent_challenges
WHEN NOT (
    (OLD.status='issued' AND NEW.status='claimed'
     AND NEW.claimed_request_id IS NOT NULL
     AND NEW.intent_basis IS NOT NULL
     AND NEW.operation_id IS NULL
     AND NEW.claimed_at IS NOT NULL
     AND NEW.consumed_at IS NULL)
    OR
    (OLD.status='claimed' AND NEW.status='consumed'
     AND NEW.claimed_request_id IS OLD.claimed_request_id
     AND NEW.intent_basis IS OLD.intent_basis
     AND NEW.override_reason IS OLD.override_reason
     AND NEW.operation_id IS NOT NULL
     AND NEW.claimed_at IS OLD.claimed_at
     AND NEW.consumed_at IS NOT NULL)
)
BEGIN SELECT RAISE(ABORT, 'Planning intent challenge transition is invalid'); END;

CREATE TRIGGER planning_intent_challenges_append_only_delete
BEFORE DELETE ON planning_intent_challenges
BEGIN SELECT RAISE(ABORT, 'Planning intent challenges are append-only'); END;
"""

_MIGRATION_35 = """
ALTER TABLE audit_events ADD COLUMN operation_execution_id TEXT
    REFERENCES operation_executions(execution_id);
CREATE INDEX audit_events_operation_execution_idx
    ON audit_events(operation_execution_id);

CREATE TRIGGER audit_events_execution_binding_insert
BEFORE INSERT ON audit_events
WHEN NEW.operation_execution_id IS NOT NULL
 AND (
      NEW.operation_id IS NULL
      OR NOT EXISTS (
          SELECT 1 FROM operation_executions
           WHERE execution_id=NEW.operation_execution_id
             AND operation_id=NEW.operation_id
      )
 )
BEGIN SELECT RAISE(ABORT, 'audit execution binding is invalid'); END;
"""


_MIGRATION_36 = """
DROP TRIGGER IF EXISTS verification_cycles_state_insert;
DROP TRIGGER IF EXISTS verification_cycles_state_update;
DROP TRIGGER IF EXISTS verification_cycles_hold_binding_required_insert;
DROP TRIGGER IF EXISTS verification_cycles_hold_binding_required_update;
DROP TRIGGER IF EXISTS verification_cycles_outcome_monotonic_update;
DROP TRIGGER IF EXISTS verification_cycles_completed_fully_immutable_update;
UPDATE verification_cycles SET outcome='verification-hold' WHERE outcome='two-pass-hold';
ALTER TABLE two_pass_resets RENAME TO verification_hold_resets;
DROP INDEX IF EXISTS two_pass_resets_operation_idx;
DROP TRIGGER IF EXISTS two_pass_resets_append_only_update;
DROP TRIGGER IF EXISTS two_pass_resets_append_only_delete;
CREATE INDEX verification_hold_resets_operation_idx ON verification_hold_resets(operation_id, created_at);
CREATE TRIGGER verification_hold_resets_append_only_update
BEFORE UPDATE ON verification_hold_resets
BEGIN SELECT RAISE(ABORT, 'Verification hold reset evidence is append-only'); END;
CREATE TRIGGER verification_hold_resets_append_only_delete
BEFORE DELETE ON verification_hold_resets
BEGIN SELECT RAISE(ABORT, 'Verification hold reset evidence is append-only'); END;
CREATE TRIGGER verification_cycles_state_insert
BEFORE INSERT ON verification_cycles
WHEN (NEW.outcome = 'approved' AND (NEW.completed_at IS NULL OR NEW.signed_content_version_id IS NULL OR NEW.signed_identity IS NULL))
   OR (NEW.route IS NULL AND COALESCE(NEW.resume_state, 'None') != 'None' AND COALESCE(NEW.outcome, '') != 'verification-hold')
   OR (NEW.route IS NOT NULL AND COALESCE(NEW.resume_state, 'None') = 'None')
BEGIN SELECT RAISE(ABORT, 'verification cycle state is invalid'); END;
CREATE TRIGGER verification_cycles_state_update
BEFORE UPDATE OF outcome, completed_at, signed_content_version_id, signed_identity, route, resume_state
ON verification_cycles
WHEN (NEW.outcome = 'approved' AND (NEW.completed_at IS NULL OR NEW.signed_content_version_id IS NULL OR NEW.signed_identity IS NULL))
   OR (NEW.route IS NULL AND COALESCE(NEW.resume_state, 'None') != 'None' AND COALESCE(NEW.outcome, '') != 'verification-hold')
   OR (NEW.route IS NOT NULL AND COALESCE(NEW.resume_state, 'None') = 'None')
BEGIN SELECT RAISE(ABORT, 'verification cycle state is invalid'); END;
CREATE TRIGGER verification_cycles_hold_binding_required_insert
BEFORE INSERT ON verification_cycles
WHEN NEW.completed_at IS NOT NULL
 AND (NEW.route IN ('evidence','human_review') OR NEW.outcome='verification-hold')
 AND (NEW.hold_content_version_id IS NULL OR NEW.hold_identity IS NULL OR NEW.hold_section_gid IS NULL
      OR NOT EXISTS (SELECT 1 FROM content_versions AS version WHERE version.content_version_id=NEW.hold_content_version_id AND version.operation_id=NEW.operation_id AND version.task_gid=NEW.task_gid AND version.confirmed=1 AND version.identity=NEW.hold_identity))
BEGIN SELECT RAISE(ABORT, 'hold outcome requires exact content and placement evidence'); END;
CREATE TRIGGER verification_cycles_hold_binding_required_update
BEFORE UPDATE ON verification_cycles
WHEN NEW.completed_at IS NOT NULL
 AND (NEW.route IN ('evidence','human_review') OR NEW.outcome='verification-hold')
 AND (NEW.hold_content_version_id IS NULL OR NEW.hold_identity IS NULL OR NEW.hold_section_gid IS NULL
      OR NOT EXISTS (SELECT 1 FROM content_versions AS version WHERE version.content_version_id=NEW.hold_content_version_id AND version.operation_id=NEW.operation_id AND version.task_gid=NEW.task_gid AND version.confirmed=1 AND version.identity=NEW.hold_identity))
BEGIN SELECT RAISE(ABORT, 'hold outcome requires exact content and placement evidence'); END;
CREATE TRIGGER verification_cycles_outcome_monotonic_update
BEFORE UPDATE ON verification_cycles
WHEN (OLD.completed_at IS NOT NULL AND NEW.completed_at IS NOT OLD.completed_at)
  OR (OLD.outcome IS NOT NULL AND NEW.outcome IS NOT OLD.outcome)
  OR (OLD.route IS NOT NULL AND NEW.route IS NOT OLD.route)
  OR (OLD.resume_state IS NOT NULL AND NEW.resume_state IS NOT OLD.resume_state)
  OR (OLD.signed_content_version_id IS NOT NULL
      AND NEW.signed_content_version_id IS NOT OLD.signed_content_version_id)
  OR (OLD.signed_identity IS NOT NULL AND NEW.signed_identity IS NOT OLD.signed_identity)
BEGIN SELECT RAISE(ABORT, 'verification cycle outcome is monotonic'); END;
CREATE TRIGGER verification_cycles_completed_fully_immutable_update
BEFORE UPDATE ON verification_cycles
WHEN OLD.completed_at IS NOT NULL AND (
    NEW.operation_id IS NOT OLD.operation_id OR NEW.task_gid IS NOT OLD.task_gid OR
    NEW.cycle_number IS NOT OLD.cycle_number OR NEW.protocol_release IS NOT OLD.protocol_release OR
    NEW.protocol_text IS NOT OLD.protocol_text OR NEW.verifier_agent IS NOT OLD.verifier_agent OR
    NEW.run_id IS NOT OLD.run_id OR NEW.independence_attestation IS NOT OLD.independence_attestation OR
    NEW.correction_class IS NOT OLD.correction_class OR NEW.outcome IS NOT OLD.outcome OR
    NEW.route IS NOT OLD.route OR NEW.resume_state IS NOT OLD.resume_state OR
    NEW.created_at IS NOT OLD.created_at OR NEW.completed_at IS NOT OLD.completed_at OR
    NEW.reviewed_content_version_id IS NOT OLD.reviewed_content_version_id OR
    NEW.reviewed_identity IS NOT OLD.reviewed_identity OR
    NEW.signed_content_version_id IS NOT OLD.signed_content_version_id OR
    NEW.signed_identity IS NOT OLD.signed_identity
)
BEGIN SELECT RAISE(ABORT, 'completed verification cycle is immutable'); END;
"""

_MIGRATION_37 = """
DROP TRIGGER abandonment_attempts_authority_insert;

CREATE TRIGGER abandonment_attempts_authority_insert
BEFORE INSERT ON abandonment_attempts
WHEN NOT EXISTS (
        SELECT 1
          FROM operations AS operation
         WHERE operation.operation_id=NEW.source_operation_id
           AND operation.task_gid=NEW.task_gid
           AND operation.status IN ('open','uncertain')
           AND operation.phase != 'terminal'
     )
  OR NOT EXISTS (
        SELECT 1
          FROM service_leases AS lease
         WHERE lease.lease_id=NEW.source_lease_id
           AND lease.operation_id=NEW.source_operation_id
           AND lease.task_gid=NEW.task_gid
           AND lease.owner_id=NEW.abandoned_owner_id
           AND lease.run_id=NEW.abandoned_run_id
           AND lease.lease_kind='actor'
           AND lease.actor_attempt_seq IS NOT NULL
           AND lease.context_cycle_id IS NEW.attempt_cycle_id
           AND (
               lease.released_at IS NOT NULL
               OR julianday(lease.expires_at) <= julianday(NEW.created_at)
           )
     )
  OR EXISTS (
        SELECT 1
          FROM service_leases AS selected
          JOIN service_leases AS later
            ON later.task_gid=selected.task_gid
           AND later.lease_kind='actor'
           AND later.actor_attempt_seq > selected.actor_attempt_seq
         WHERE selected.lease_id=NEW.source_lease_id
           AND EXISTS (
               SELECT 1 FROM operation_actor_facts AS fact
                WHERE fact.operation_id=NEW.source_operation_id
                  AND fact.run_id=later.run_id
           )
     )
  OR EXISTS (
        SELECT 1 FROM operation_successions
         WHERE source_operation_id=NEW.source_operation_id
     )
  OR (
        NEW.attempt_cycle_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM verification_cycles AS cycle
             WHERE cycle.cycle_id=NEW.attempt_cycle_id
               AND cycle.operation_id=NEW.source_operation_id
               AND cycle.task_gid=NEW.task_gid
               AND cycle.run_id=NEW.abandoned_run_id
        )
     )
  OR (
        NEW.current_execution_id IS NOT NULL
        AND NOT EXISTS (
            SELECT 1 FROM operation_executions AS execution
             WHERE execution.execution_id=NEW.current_execution_id
               AND execution.operation_id=NEW.source_operation_id
        )
     )
BEGIN SELECT RAISE(ABORT, 'abandonment attempt authority binding is invalid'); END;
"""

_MIGRATION_38 = """
CREATE TABLE semantic_proposals (
    proposal_id TEXT PRIMARY KEY,
    task_gid TEXT NOT NULL,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    cycle_id TEXT NOT NULL REFERENCES verification_cycles(cycle_id),
    baseline_identity TEXT NOT NULL,
    candidate_identity TEXT NOT NULL,
    candidate_title TEXT NOT NULL,
    candidate_notes TEXT NOT NULL,
    proposal_reason TEXT NOT NULL,
    explanation_json TEXT NOT NULL CHECK (json_valid(explanation_json)),
    linked_changes_json TEXT NOT NULL CHECK (json_valid(linked_changes_json)),
    protocol_release TEXT NOT NULL,
    protocol_text TEXT NOT NULL,
    correction_class TEXT NOT NULL CHECK (correction_class IN ('large')),
    proposer_agent TEXT NOT NULL CHECK (proposer_agent IN ('claude','gpt','codex')),
    proposer_run_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','claimed','applied','stale')),
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    review_reason TEXT,
    approved_by TEXT,
    claimed_at TEXT,
    claimed_agent TEXT CHECK (claimed_agent IS NULL OR claimed_agent IN ('claude','gpt','codex')),
    claimed_run_id TEXT,
    claim_request_id TEXT,
    applied_at TEXT,
    applied_identity TEXT,
    CHECK (length(baseline_identity)=64),
    CHECK (length(candidate_identity)=64),
    CHECK (length(trim(candidate_title))>0),
    CHECK (length(trim(proposal_reason))>0),
    CHECK (length(trim(proposer_run_id))>0),
    CHECK (
        (status='pending'
         AND reviewed_at IS NULL AND review_reason IS NULL AND approved_by IS NULL
         AND claimed_at IS NULL AND claimed_agent IS NULL AND claimed_run_id IS NULL
         AND claim_request_id IS NULL AND applied_at IS NULL AND applied_identity IS NULL)
     OR (status='approved'
         AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL AND approved_by IS NOT NULL
         AND claimed_at IS NULL AND claimed_agent IS NULL AND claimed_run_id IS NULL
         AND claim_request_id IS NULL AND applied_at IS NULL AND applied_identity IS NULL)
     OR (status='rejected'
         AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL AND approved_by IS NOT NULL
         AND claimed_at IS NULL AND claimed_agent IS NULL AND claimed_run_id IS NULL
         AND claim_request_id IS NULL AND applied_at IS NULL AND applied_identity IS NULL)
     OR (status='stale'
         AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL
         AND claimed_at IS NULL AND claimed_agent IS NULL AND claimed_run_id IS NULL
         AND claim_request_id IS NULL AND applied_at IS NULL AND applied_identity IS NULL)
     OR (status='claimed'
         AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL AND approved_by IS NOT NULL
         AND claimed_at IS NOT NULL AND claimed_agent IS NOT NULL AND claimed_run_id IS NOT NULL
         AND applied_at IS NULL AND applied_identity IS NULL)
     OR (status='applied'
         AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL AND approved_by IS NOT NULL
         AND claimed_at IS NOT NULL AND claimed_agent IS NOT NULL AND claimed_run_id IS NOT NULL
         AND applied_at IS NOT NULL AND length(applied_identity)=64)
    )
);
CREATE INDEX semantic_proposals_queue_idx
    ON semantic_proposals(status, created_at, task_gid);
CREATE INDEX semantic_proposals_operation_idx
    ON semantic_proposals(operation_id, status, created_at);
CREATE UNIQUE INDEX semantic_proposals_active_operation_idx
    ON semantic_proposals(operation_id)
    WHERE status IN ('pending','approved','claimed');

CREATE TABLE semantic_proposal_changes (
    proposal_id TEXT NOT NULL REFERENCES semantic_proposals(proposal_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    field_name TEXT NOT NULL,
    before_json TEXT NOT NULL CHECK (json_valid(before_json)),
    after_json TEXT NOT NULL CHECK (json_valid(after_json)),
    CHECK (length(trim(field_name))>0),
    CHECK (before_json<>after_json),
    PRIMARY KEY(proposal_id, ordinal)
);
CREATE INDEX semantic_proposal_changes_field_idx
    ON semantic_proposal_changes(field_name, proposal_id);

CREATE TRIGGER semantic_proposals_core_immutable_update
BEFORE UPDATE ON semantic_proposals
WHEN NEW.proposal_id IS NOT OLD.proposal_id
  OR NEW.task_gid IS NOT OLD.task_gid
  OR NEW.operation_id IS NOT OLD.operation_id
  OR NEW.cycle_id IS NOT OLD.cycle_id
  OR NEW.baseline_identity IS NOT OLD.baseline_identity
  OR NEW.candidate_identity IS NOT OLD.candidate_identity
  OR NEW.candidate_title IS NOT OLD.candidate_title
  OR NEW.candidate_notes IS NOT OLD.candidate_notes
  OR NEW.proposal_reason IS NOT OLD.proposal_reason
  OR NEW.explanation_json IS NOT OLD.explanation_json
  OR NEW.linked_changes_json IS NOT OLD.linked_changes_json
  OR NEW.protocol_release IS NOT OLD.protocol_release
  OR NEW.protocol_text IS NOT OLD.protocol_text
  OR NEW.correction_class IS NOT OLD.correction_class
  OR NEW.proposer_agent IS NOT OLD.proposer_agent
  OR NEW.proposer_run_id IS NOT OLD.proposer_run_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'semantic proposal core is immutable'); END;

CREATE TRIGGER semantic_proposals_status_transition_update
BEFORE UPDATE OF status ON semantic_proposals
WHEN NOT (
    (OLD.status='pending' AND NEW.status IN ('approved','rejected','stale'))
 OR (OLD.status='approved' AND NEW.status IN ('claimed','stale'))
 OR (OLD.status='claimed' AND NEW.status IN ('approved','applied','stale'))
 OR (OLD.status=NEW.status)
)
BEGIN SELECT RAISE(ABORT, 'semantic proposal status transition is invalid'); END;

CREATE TRIGGER semantic_proposals_terminal_immutable_update
BEFORE UPDATE ON semantic_proposals
WHEN OLD.status IN ('rejected','applied','stale')
BEGIN SELECT RAISE(ABORT, 'terminal semantic proposal is immutable'); END;

CREATE TRIGGER semantic_proposals_delete
BEFORE DELETE ON semantic_proposals
BEGIN SELECT RAISE(ABORT, 'semantic proposals are append-only'); END;

CREATE TRIGGER semantic_proposal_changes_update
BEFORE UPDATE ON semantic_proposal_changes
BEGIN SELECT RAISE(ABORT, 'semantic proposal changes are immutable'); END;
CREATE TRIGGER semantic_proposal_changes_delete
BEFORE DELETE ON semantic_proposal_changes
BEGIN SELECT RAISE(ABORT, 'semantic proposal changes are append-only'); END;
"""

_MIGRATION_39 = """
ALTER TABLE write_attempts ADD COLUMN expected_modified_at TEXT;
ALTER TABLE write_attempts ADD COLUMN version_source TEXT;
ALTER TABLE write_attempts ADD COLUMN version_reliable INTEGER NOT NULL DEFAULT 0
    CHECK(version_reliable IN (0,1));
ALTER TABLE movement_attempts ADD COLUMN expected_modified_at TEXT;
ALTER TABLE movement_attempts ADD COLUMN version_source TEXT;
ALTER TABLE movement_attempts ADD COLUMN version_reliable INTEGER NOT NULL DEFAULT 0
    CHECK(version_reliable IN (0,1));
ALTER TABLE planning_reopen_attempts ADD COLUMN expected_version_source TEXT;
ALTER TABLE planning_reopen_attempts ADD COLUMN expected_version_reliable INTEGER NOT NULL DEFAULT 0
    CHECK(expected_version_reliable IN (0,1));

DROP TRIGGER write_attempt_intent_immutable_update;
CREATE TRIGGER write_attempt_intent_immutable_update
BEFORE UPDATE ON write_attempts
WHEN NEW.attempt_id IS NOT OLD.attempt_id
  OR NEW.operation_id IS NOT OLD.operation_id
  OR NEW.expected_identity IS NOT OLD.expected_identity
  OR NEW.intended_identity IS NOT OLD.intended_identity
  OR NEW.started_at IS NOT OLD.started_at
  OR NEW.purpose IS NOT OLD.purpose
  OR NEW.intended_title IS NOT OLD.intended_title
  OR NEW.intended_notes IS NOT OLD.intended_notes
  OR NEW.schema_version IS NOT OLD.schema_version
  OR NEW.context_json IS NOT OLD.context_json
  OR NEW.expected_modified_at IS NOT OLD.expected_modified_at
  OR NEW.version_source IS NOT OLD.version_source
  OR NEW.version_reliable IS NOT OLD.version_reliable
BEGIN SELECT RAISE(ABORT, 'write attempt intent is immutable'); END;

DROP TRIGGER movement_attempt_intent_immutable_update;
CREATE TRIGGER movement_attempt_intent_immutable_update
BEFORE UPDATE ON movement_attempts
WHEN NEW.attempt_id IS NOT OLD.attempt_id
  OR NEW.operation_id IS NOT OLD.operation_id
  OR NEW.expected_section_gid IS NOT OLD.expected_section_gid
  OR NEW.intended_section_gid IS NOT OLD.intended_section_gid
  OR NEW.started_at IS NOT OLD.started_at
  OR NEW.purpose IS NOT OLD.purpose
  OR NEW.expected_modified_at IS NOT OLD.expected_modified_at
  OR NEW.version_source IS NOT OLD.version_source
  OR NEW.version_reliable IS NOT OLD.version_reliable
BEGIN SELECT RAISE(ABORT, 'movement attempt intent is immutable'); END;

DROP TRIGGER planning_reopen_attempts_identity_immutable_update;
CREATE TRIGGER planning_reopen_attempts_identity_immutable_update
BEFORE UPDATE ON planning_reopen_attempts
WHEN NEW.attempt_id IS NOT OLD.attempt_id
  OR NEW.task_gid IS NOT OLD.task_gid
  OR NEW.request_id IS NOT OLD.request_id
  OR NEW.expected_identity IS NOT OLD.expected_identity
  OR NEW.expected_section_gid IS NOT OLD.expected_section_gid
  OR NEW.expected_modified_at IS NOT OLD.expected_modified_at
  OR NEW.expected_version_source IS NOT OLD.expected_version_source
  OR NEW.expected_version_reliable IS NOT OLD.expected_version_reliable
  OR NEW.reason IS NOT OLD.reason
  OR NEW.actor_run_id IS NOT OLD.actor_run_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'planning reopen attempt identity is immutable'); END;

DROP TRIGGER planning_reopen_attempts_status_monotonic_update;
CREATE TRIGGER planning_reopen_attempts_status_monotonic_update
BEFORE UPDATE OF outcome, finished_at, confirmed_modified_at ON planning_reopen_attempts
WHEN NOT (
       (OLD.outcome='started' AND NEW.outcome IN ('confirmed','not_applied','uncertain'))
    OR (OLD.outcome='uncertain' AND NEW.outcome IN ('confirmed','not_applied'))
)
 OR NEW.finished_at IS NULL
BEGIN SELECT RAISE(ABORT, 'planning reopen attempt completion is one-way'); END;

DROP TRIGGER backup_creations_identity_immutable_update;
DROP TRIGGER backup_creations_status_monotonic_update;
DROP TRIGGER backup_creations_append_only_delete;
DROP INDEX backup_creations_status_idx;
ALTER TABLE backup_creations RENAME TO backup_creations_v38;
CREATE TABLE backup_creations (
    request_id TEXT PRIMARY KEY REFERENCES service_requests(request_id),
    backup_id TEXT NOT NULL UNIQUE CHECK(length(trim(backup_id)) > 0),
    status TEXT NOT NULL CHECK(status IN ('reserved','confirmed','not_applied','uncertain')),
    sha256 TEXT,
    size_bytes INTEGER,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    resolution_reason TEXT,
    CHECK (
        (status='reserved' AND sha256 IS NULL AND size_bytes IS NULL
                           AND completed_at IS NULL AND resolution_reason IS NULL)
     OR (status='confirmed' AND length(trim(sha256)) > 0 AND size_bytes >= 0
                            AND completed_at IS NOT NULL)
     OR (status='not_applied' AND sha256 IS NULL AND size_bytes IS NULL
                              AND completed_at IS NOT NULL)
     OR (status='uncertain' AND completed_at IS NOT NULL
                          AND ((sha256 IS NULL AND size_bytes IS NULL)
                            OR (length(trim(sha256)) > 0 AND size_bytes >= 0)))
    )
);
INSERT INTO backup_creations(
    request_id, backup_id, status, sha256, size_bytes, created_at, completed_at,
    resolution_reason
)
SELECT request_id, backup_id,
       CASE status WHEN 'completed' THEN 'confirmed' ELSE status END,
       sha256, size_bytes, created_at, completed_at,
       CASE status WHEN 'completed' THEN 'migrated_confirmed' ELSE NULL END
  FROM backup_creations_v38;
DROP TABLE backup_creations_v38;
CREATE INDEX backup_creations_status_idx
    ON backup_creations(status, created_at);
CREATE TRIGGER backup_creations_identity_immutable_update
BEFORE UPDATE ON backup_creations
WHEN NEW.request_id IS NOT OLD.request_id
  OR NEW.backup_id IS NOT OLD.backup_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'backup creation identity is immutable'); END;
CREATE TRIGGER backup_creations_status_monotonic_update
BEFORE UPDATE OF status, sha256, size_bytes, completed_at, resolution_reason
ON backup_creations
WHEN NOT (
       (OLD.status='reserved' AND NEW.status IN ('confirmed','not_applied','uncertain'))
    OR (OLD.status='uncertain' AND NEW.status IN ('confirmed','not_applied'))
)
 OR NEW.completed_at IS NULL
 OR (NEW.status='confirmed' AND (
       NEW.sha256 IS NULL OR length(trim(NEW.sha256))=0
       OR NEW.size_bytes IS NULL OR NEW.size_bytes < 0))
 OR (NEW.status='not_applied' AND (
       NEW.sha256 IS NOT NULL OR NEW.size_bytes IS NOT NULL))
BEGIN SELECT RAISE(ABORT, 'backup creation outcome is monotonic'); END;
CREATE TRIGGER backup_creations_terminal_immutable_update
BEFORE UPDATE ON backup_creations
WHEN OLD.status IN ('confirmed','not_applied')
BEGIN SELECT RAISE(ABORT, 'terminal backup creation is immutable'); END;
CREATE TRIGGER backup_creations_append_only_delete
BEFORE DELETE ON backup_creations
BEGIN SELECT RAISE(ABORT, 'backup creations are append-only'); END;

DROP TRIGGER service_requests_status_monotonic_update;
CREATE TRIGGER service_requests_status_monotonic_update
BEFORE UPDATE OF status, operation_id, task_gid, result_json, completed_at, resolution_result_json, resolved_at
ON service_requests
WHEN NOT (
    (OLD.status='pending'
     AND NEW.status IN ('completed','uncertain')
     AND NEW.result_json IS NOT NULL AND NEW.completed_at IS NOT NULL
     AND NEW.resolution_result_json IS NULL AND NEW.resolved_at IS NULL)
 OR (OLD.status='uncertain' AND NEW.status='completed'
     AND NEW.operation_id IS OLD.operation_id
     AND NEW.task_gid IS OLD.task_gid
     AND NEW.result_json IS OLD.result_json
     AND NEW.completed_at IS OLD.completed_at
     AND NEW.resolution_result_json IS NOT NULL AND NEW.resolved_at IS NOT NULL)
 OR (OLD.status='completed' AND NEW.status='completed'
     AND OLD.command='backup-create'
     AND NEW.operation_id IS OLD.operation_id
     AND NEW.task_gid IS OLD.task_gid
     AND NEW.result_json IS OLD.result_json
     AND NEW.completed_at IS OLD.completed_at
     AND OLD.resolution_result_json IS NULL
     AND NEW.resolution_result_json IS NOT NULL AND NEW.resolved_at IS NOT NULL)
)
BEGIN SELECT RAISE(ABORT, 'service request completion or resolution is invalid'); END;
"""


_MIGRATION_40 = """
CREATE TABLE safe_reclaims (
    reclaim_id TEXT PRIMARY KEY,
    task_gid TEXT NOT NULL CHECK(length(trim(task_gid)) > 0),
    source_operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
    request_id TEXT NOT NULL UNIQUE REFERENCES service_requests(request_id),
    source_lease_id TEXT NOT NULL REFERENCES service_leases(lease_id),
    previous_owner_id TEXT NOT NULL CHECK(length(trim(previous_owner_id)) > 0),
    previous_run_id TEXT NOT NULL CHECK(length(trim(previous_run_id)) > 0),
    source_cycle_id TEXT REFERENCES verification_cycles(cycle_id),
    requested_owner_id TEXT NOT NULL CHECK(length(trim(requested_owner_id)) > 0),
    requested_run_id TEXT NOT NULL CHECK(length(trim(requested_run_id)) > 0),
    successor_operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
    successor_cycle_id TEXT REFERENCES verification_cycles(cycle_id),
    source_content_version_id TEXT NOT NULL REFERENCES content_versions(content_version_id),
    successor_content_version_id TEXT NOT NULL REFERENCES content_versions(content_version_id),
    stage TEXT NOT NULL CHECK(stage IN ('planning','research','verification')),
    reason TEXT NOT NULL CHECK(reason IN ('expired_actor_lease','terminated_actor_lease')),
    status TEXT NOT NULL CHECK(status IN ('prepared','claimed')),
    created_at TEXT NOT NULL,
    claimed_at TEXT,
    CHECK(previous_run_id != requested_run_id),
    CHECK((status='prepared' AND claimed_at IS NULL)
       OR (status='claimed' AND claimed_at IS NOT NULL)),
    CHECK((stage='verification' AND source_cycle_id IS NOT NULL AND successor_cycle_id IS NOT NULL)
       OR (stage IN ('planning','research') AND source_cycle_id IS NULL AND successor_cycle_id IS NULL))
);
CREATE INDEX safe_reclaims_task_idx ON safe_reclaims(task_gid, created_at);

CREATE TRIGGER safe_reclaims_binding_insert
BEFORE INSERT ON safe_reclaims
WHEN NOT EXISTS (
        SELECT 1 FROM operations AS source
         WHERE source.operation_id=NEW.source_operation_id
           AND source.task_gid=NEW.task_gid
           AND source.status='cancelled'
           AND source.phase='terminal'
           AND source.terminal_outcome='safe_reclaimed'
     )
  OR NOT EXISTS (
        SELECT 1 FROM operations AS successor
         WHERE successor.operation_id=NEW.successor_operation_id
           AND successor.task_gid=NEW.task_gid
           AND successor.status='open'
           AND successor.phase!='terminal'
           AND successor.successor_claim_mode IN ('stage_actor','verifier')
           AND successor.content_write_completed_at IS NULL
     )
  OR NOT EXISTS (
        SELECT 1 FROM service_leases AS lease
         WHERE lease.lease_id=NEW.source_lease_id
           AND lease.operation_id=NEW.source_operation_id
           AND lease.task_gid=NEW.task_gid
           AND lease.owner_id=NEW.previous_owner_id
           AND lease.run_id=NEW.previous_run_id
           AND lease.lease_kind='actor'
           AND lease.context_cycle_id IS NEW.source_cycle_id
           AND (lease.released_at IS NOT NULL OR julianday(lease.expires_at) <= julianday(NEW.created_at))
     )
  OR NOT EXISTS (
        SELECT 1 FROM service_requests AS request
         WHERE request.request_id=NEW.request_id
           AND request.command='safe-reclaim'
           AND request.owner_id=NEW.requested_owner_id
           AND request.run_id=NEW.requested_run_id
           AND request.status='pending'
     )
  OR NOT EXISTS (
        SELECT 1
          FROM content_versions AS source_version
          JOIN content_versions AS successor_version
            ON successor_version.content_version_id=NEW.successor_content_version_id
         WHERE source_version.content_version_id=NEW.source_content_version_id
           AND source_version.task_gid=NEW.task_gid
           AND source_version.confirmed=1
           AND successor_version.task_gid=NEW.task_gid
           AND successor_version.operation_id=NEW.successor_operation_id
           AND successor_version.boundary='successor_baseline'
           AND successor_version.confirmed=1
           AND successor_version.identity=source_version.identity
           AND successor_version.title=source_version.title
           AND successor_version.notes=source_version.notes
     )
  OR (NEW.source_cycle_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM verification_cycles AS cycle
         WHERE cycle.cycle_id=NEW.source_cycle_id
           AND cycle.operation_id=NEW.source_operation_id
           AND cycle.task_gid=NEW.task_gid
           AND cycle.run_id=NEW.previous_run_id
           AND cycle.outcome='safe_reclaimed'
           AND cycle.completed_at IS NOT NULL
     ))
  OR (NEW.successor_cycle_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM verification_cycles AS cycle
         WHERE cycle.cycle_id=NEW.successor_cycle_id
           AND cycle.operation_id=NEW.successor_operation_id
           AND cycle.task_gid=NEW.task_gid
           AND cycle.completed_at IS NULL
           AND cycle.outcome IS NULL
     ))
BEGIN SELECT RAISE(ABORT, 'safe reclaim binding is invalid'); END;

CREATE TRIGGER safe_reclaims_identity_immutable_update
BEFORE UPDATE ON safe_reclaims
WHEN NEW.reclaim_id IS NOT OLD.reclaim_id
  OR NEW.task_gid IS NOT OLD.task_gid
  OR NEW.source_operation_id IS NOT OLD.source_operation_id
  OR NEW.request_id IS NOT OLD.request_id
  OR NEW.source_lease_id IS NOT OLD.source_lease_id
  OR NEW.previous_owner_id IS NOT OLD.previous_owner_id
  OR NEW.previous_run_id IS NOT OLD.previous_run_id
  OR NEW.source_cycle_id IS NOT OLD.source_cycle_id
  OR NEW.requested_owner_id IS NOT OLD.requested_owner_id
  OR NEW.requested_run_id IS NOT OLD.requested_run_id
  OR NEW.successor_operation_id IS NOT OLD.successor_operation_id
  OR NEW.successor_cycle_id IS NOT OLD.successor_cycle_id
  OR NEW.source_content_version_id IS NOT OLD.source_content_version_id
  OR NEW.successor_content_version_id IS NOT OLD.successor_content_version_id
  OR NEW.stage IS NOT OLD.stage
  OR NEW.reason IS NOT OLD.reason
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'safe reclaim identity is immutable'); END;

CREATE TRIGGER safe_reclaims_status_transition_update
BEFORE UPDATE OF status,claimed_at ON safe_reclaims
WHEN NOT (OLD.status='prepared' AND NEW.status='claimed' AND NEW.claimed_at IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'safe reclaim claim transition is invalid'); END;
CREATE TRIGGER safe_reclaims_append_only_delete
BEFORE DELETE ON safe_reclaims
BEGIN SELECT RAISE(ABORT, 'safe reclaims are append-only'); END;

CREATE TRIGGER verification_cycles_safe_reclaimed_insert
BEFORE INSERT ON verification_cycles
WHEN NEW.outcome='safe_reclaimed'
BEGIN SELECT RAISE(ABORT, 'safe-reclaimed Verification outcome must close an existing incomplete cycle'); END;
CREATE TRIGGER verification_cycles_safe_reclaimed_update
BEFORE UPDATE OF outcome,completed_at,signed_content_version_id,signed_identity ON verification_cycles
WHEN NEW.outcome='safe_reclaimed' AND (
     OLD.completed_at IS NOT NULL OR OLD.outcome IS NOT NULL
     OR NEW.completed_at IS NULL
     OR NEW.signed_content_version_id IS NOT NULL OR NEW.signed_identity IS NOT NULL
     OR NOT EXISTS (
        SELECT 1 FROM operations AS operation
         WHERE operation.operation_id=NEW.operation_id
           AND operation.status='cancelled'
           AND operation.terminal_outcome='safe_reclaimed'
     )
)
BEGIN SELECT RAISE(ABORT, 'safe-reclaimed Verification cycle binding is invalid'); END;

CREATE TRIGGER operations_safe_reclaimed_immutable_update
BEFORE UPDATE ON operations
WHEN OLD.status='cancelled' AND OLD.terminal_outcome='safe_reclaimed'
BEGIN SELECT RAISE(ABORT, 'safe-reclaimed operation is immutable'); END;
CREATE TRIGGER safe_reclaimed_operation_steps_insert
BEFORE INSERT ON operation_steps
WHEN EXISTS (SELECT 1 FROM operations WHERE operation_id=NEW.operation_id AND status='cancelled' AND terminal_outcome='safe_reclaimed')
BEGIN SELECT RAISE(ABORT, 'safe-reclaimed operation cannot receive workflow steps'); END;
CREATE TRIGGER safe_reclaimed_actor_facts_insert
BEFORE INSERT ON operation_actor_facts
WHEN EXISTS (SELECT 1 FROM operations WHERE operation_id=NEW.operation_id AND status='cancelled' AND terminal_outcome='safe_reclaimed')
BEGIN SELECT RAISE(ABORT, 'safe-reclaimed operation cannot receive actor facts'); END;
CREATE TRIGGER safe_reclaimed_write_attempts_insert
BEFORE INSERT ON write_attempts
WHEN EXISTS (SELECT 1 FROM operations WHERE operation_id=NEW.operation_id AND status='cancelled' AND terminal_outcome='safe_reclaimed')
BEGIN SELECT RAISE(ABORT, 'safe-reclaimed operation cannot receive write attempts'); END;
CREATE TRIGGER safe_reclaimed_movement_attempts_insert
BEFORE INSERT ON movement_attempts
WHEN EXISTS (SELECT 1 FROM operations WHERE operation_id=NEW.operation_id AND status='cancelled' AND terminal_outcome='safe_reclaimed')
BEGIN SELECT RAISE(ABORT, 'safe-reclaimed operation cannot receive movement attempts'); END;
CREATE TRIGGER safe_reclaimed_content_versions_insert
BEFORE INSERT ON content_versions
WHEN NEW.operation_id IS NOT NULL AND EXISTS (
    SELECT 1 FROM operations WHERE operation_id=NEW.operation_id AND status='cancelled' AND terminal_outcome='safe_reclaimed'
)
BEGIN SELECT RAISE(ABORT, 'safe-reclaimed operation cannot receive content versions'); END;
"""

_MIGRATION_41 = """
DROP TRIGGER semantic_proposal_changes_update;
DROP TRIGGER semantic_proposal_changes_delete;
DROP TRIGGER semantic_proposals_core_immutable_update;
DROP TRIGGER semantic_proposals_status_transition_update;
DROP TRIGGER semantic_proposals_terminal_immutable_update;
DROP TRIGGER semantic_proposals_delete;
DROP INDEX semantic_proposal_changes_field_idx;
DROP INDEX semantic_proposals_queue_idx;
DROP INDEX semantic_proposals_operation_idx;
DROP INDEX semantic_proposals_active_operation_idx;

ALTER TABLE semantic_proposal_changes RENAME TO semantic_proposal_changes_v40;
ALTER TABLE semantic_proposals RENAME TO semantic_proposals_v40;

CREATE TABLE semantic_proposals (
    proposal_id TEXT PRIMARY KEY,
    task_gid TEXT NOT NULL,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    cycle_id TEXT NOT NULL REFERENCES verification_cycles(cycle_id),
    baseline_identity TEXT NOT NULL,
    candidate_identity TEXT NOT NULL,
    candidate_title TEXT NOT NULL,
    candidate_notes TEXT NOT NULL,
    proposal_reason TEXT NOT NULL,
    explanation_json TEXT NOT NULL CHECK (json_valid(explanation_json)),
    linked_changes_json TEXT NOT NULL CHECK (json_valid(linked_changes_json)),
    agent_attested_decisions_json TEXT NOT NULL CHECK (json_valid(agent_attested_decisions_json)),
    protocol_release TEXT NOT NULL,
    protocol_text TEXT NOT NULL,
    correction_class TEXT NOT NULL CHECK (correction_class IN ('large')),
    proposer_agent TEXT NOT NULL CHECK (proposer_agent IN ('claude','gpt','codex')),
    proposer_run_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','claimed','applied','stale')),
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    review_reason TEXT,
    approved_by TEXT,
    claimed_at TEXT,
    claimed_agent TEXT CHECK (claimed_agent IS NULL OR claimed_agent IN ('claude','gpt','codex','dish')),
    claimed_run_id TEXT,
    claim_request_id TEXT,
    applied_at TEXT,
    applied_identity TEXT,
    CHECK (length(baseline_identity)=64),
    CHECK (length(candidate_identity)=64),
    CHECK (length(trim(candidate_title))>0),
    CHECK (length(trim(proposal_reason))>0),
    CHECK (length(trim(proposer_run_id))>0),
    CHECK (
        (status='pending'
         AND reviewed_at IS NULL AND review_reason IS NULL AND approved_by IS NULL
         AND claimed_at IS NULL AND claimed_agent IS NULL AND claimed_run_id IS NULL
         AND claim_request_id IS NULL AND applied_at IS NULL AND applied_identity IS NULL)
     OR (status='approved'
         AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL AND approved_by IS NOT NULL
         AND claimed_at IS NULL AND claimed_agent IS NULL AND claimed_run_id IS NULL
         AND claim_request_id IS NULL AND applied_at IS NULL AND applied_identity IS NULL)
     OR (status='rejected'
         AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL AND approved_by IS NOT NULL
         AND claimed_at IS NULL AND claimed_agent IS NULL AND claimed_run_id IS NULL
         AND claim_request_id IS NULL AND applied_at IS NULL AND applied_identity IS NULL)
     OR (status='stale'
         AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL
         AND claimed_at IS NULL AND claimed_agent IS NULL AND claimed_run_id IS NULL
         AND claim_request_id IS NULL AND applied_at IS NULL AND applied_identity IS NULL)
     OR (status='claimed'
         AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL AND approved_by IS NOT NULL
         AND claimed_at IS NOT NULL AND claimed_agent IS NOT NULL AND claimed_run_id IS NOT NULL
         AND applied_at IS NULL AND applied_identity IS NULL)
     OR (status='applied'
         AND reviewed_at IS NOT NULL AND review_reason IS NOT NULL AND approved_by IS NOT NULL
         AND claimed_at IS NOT NULL AND claimed_agent IS NOT NULL AND claimed_run_id IS NOT NULL
         AND applied_at IS NOT NULL AND length(applied_identity)=64)
    )
);
CREATE INDEX semantic_proposals_queue_idx
    ON semantic_proposals(status, created_at, task_gid);
CREATE INDEX semantic_proposals_operation_idx
    ON semantic_proposals(operation_id, status, created_at);
CREATE UNIQUE INDEX semantic_proposals_active_operation_idx
    ON semantic_proposals(operation_id)
    WHERE status IN ('pending','approved','claimed');

INSERT INTO semantic_proposals(
    proposal_id,task_gid,operation_id,cycle_id,baseline_identity,candidate_identity,
    candidate_title,candidate_notes,proposal_reason,explanation_json,linked_changes_json,
    agent_attested_decisions_json,protocol_release,protocol_text,correction_class,
    proposer_agent,proposer_run_id,status,created_at,reviewed_at,review_reason,approved_by,
    claimed_at,claimed_agent,claimed_run_id,claim_request_id,applied_at,applied_identity
)
SELECT
    proposal_id,task_gid,operation_id,cycle_id,baseline_identity,candidate_identity,
    candidate_title,candidate_notes,proposal_reason,explanation_json,linked_changes_json,
    '[]',protocol_release,protocol_text,correction_class,
    proposer_agent,proposer_run_id,status,created_at,reviewed_at,review_reason,approved_by,
    claimed_at,claimed_agent,claimed_run_id,claim_request_id,applied_at,applied_identity
FROM semantic_proposals_v40;

CREATE TABLE semantic_proposal_changes (
    proposal_id TEXT NOT NULL REFERENCES semantic_proposals(proposal_id),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    field_name TEXT NOT NULL,
    before_json TEXT NOT NULL CHECK (json_valid(before_json)),
    after_json TEXT NOT NULL CHECK (json_valid(after_json)),
    CHECK (length(trim(field_name))>0),
    CHECK (before_json<>after_json),
    PRIMARY KEY(proposal_id, ordinal)
);
CREATE INDEX semantic_proposal_changes_field_idx
    ON semantic_proposal_changes(field_name, proposal_id);
INSERT INTO semantic_proposal_changes SELECT * FROM semantic_proposal_changes_v40;

DROP TABLE semantic_proposal_changes_v40;
DROP TABLE semantic_proposals_v40;

CREATE TRIGGER semantic_proposals_core_immutable_update
BEFORE UPDATE ON semantic_proposals
WHEN NEW.proposal_id IS NOT OLD.proposal_id
  OR NEW.task_gid IS NOT OLD.task_gid
  OR NEW.operation_id IS NOT OLD.operation_id
  OR NEW.cycle_id IS NOT OLD.cycle_id
  OR NEW.baseline_identity IS NOT OLD.baseline_identity
  OR NEW.candidate_identity IS NOT OLD.candidate_identity
  OR NEW.candidate_title IS NOT OLD.candidate_title
  OR NEW.candidate_notes IS NOT OLD.candidate_notes
  OR NEW.proposal_reason IS NOT OLD.proposal_reason
  OR NEW.explanation_json IS NOT OLD.explanation_json
  OR NEW.linked_changes_json IS NOT OLD.linked_changes_json
  OR NEW.agent_attested_decisions_json IS NOT OLD.agent_attested_decisions_json
  OR NEW.protocol_release IS NOT OLD.protocol_release
  OR NEW.protocol_text IS NOT OLD.protocol_text
  OR NEW.correction_class IS NOT OLD.correction_class
  OR NEW.proposer_agent IS NOT OLD.proposer_agent
  OR NEW.proposer_run_id IS NOT OLD.proposer_run_id
  OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT, 'semantic proposal core is immutable'); END;

CREATE TRIGGER semantic_proposals_status_transition_update
BEFORE UPDATE OF status ON semantic_proposals
WHEN NOT (
    (OLD.status='pending' AND NEW.status IN ('approved','rejected','stale'))
 OR (OLD.status='approved' AND NEW.status IN ('claimed','stale'))
 OR (OLD.status='claimed' AND NEW.status IN ('approved','applied','stale'))
 OR (OLD.status=NEW.status)
)
BEGIN SELECT RAISE(ABORT, 'semantic proposal status transition is invalid'); END;

CREATE TRIGGER semantic_proposals_terminal_immutable_update
BEFORE UPDATE ON semantic_proposals
WHEN OLD.status IN ('rejected','applied','stale')
BEGIN SELECT RAISE(ABORT, 'terminal semantic proposal is immutable'); END;

CREATE TRIGGER semantic_proposals_delete
BEFORE DELETE ON semantic_proposals
BEGIN SELECT RAISE(ABORT, 'semantic proposals are append-only'); END;

CREATE TRIGGER semantic_proposal_changes_update
BEFORE UPDATE ON semantic_proposal_changes
BEGIN SELECT RAISE(ABORT, 'semantic proposal changes are immutable'); END;
CREATE TRIGGER semantic_proposal_changes_delete
BEFORE DELETE ON semantic_proposal_changes
BEGIN SELECT RAISE(ABORT, 'semantic proposal changes are append-only'); END;
"""

_MIGRATION_42 = """
CREATE TABLE operation_run_retirements (
    retirement_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    owner_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source_lease_id TEXT REFERENCES service_leases(lease_id),
    reason TEXT NOT NULL,
    retired_at TEXT NOT NULL,
    CHECK (length(trim(owner_id))>0),
    CHECK (length(trim(run_id))>0),
    CHECK (length(trim(reason))>0),
    UNIQUE(operation_id, owner_id, run_id)
);
CREATE INDEX operation_run_retirements_operation_idx
    ON operation_run_retirements(operation_id, retired_at);

CREATE TRIGGER operation_run_retirements_immutable_update
BEFORE UPDATE ON operation_run_retirements
BEGIN SELECT RAISE(ABORT, 'operation run retirements are immutable'); END;
CREATE TRIGGER operation_run_retirements_append_only_delete
BEFORE DELETE ON operation_run_retirements
BEGIN SELECT RAISE(ABORT, 'operation run retirements are append-only'); END;
"""

_MIGRATION_43 = """
ALTER TABLE semantic_proposals ADD COLUMN claimed_owner_id TEXT;

-- Schema 42 persisted the proposal run but not its service owner. Recover the
-- exact owner only from deterministic evidence. Dish's mechanical applicant has
-- one fixed owner; connected applicants can be recovered from their durable
-- request identity, exact operation/run lease history, or an existing explicit
-- schema-42 retirement record. History supplies identity here; it does not by
-- itself create revocation.
UPDATE semantic_proposals
   SET claimed_owner_id='dish-mechanical'
 WHERE status='claimed' AND claimed_agent='dish';

UPDATE semantic_proposals
   SET claimed_owner_id=(
       SELECT request.owner_id
         FROM service_requests AS request
        WHERE request.request_id=semantic_proposals.claim_request_id
          AND request.run_id=semantic_proposals.claimed_run_id
        LIMIT 1
   )
 WHERE status='claimed'
   AND claimed_owner_id IS NULL
   AND claim_request_id IS NOT NULL;

UPDATE semantic_proposals
   SET claimed_owner_id=(
       SELECT MIN(lease.owner_id)
         FROM service_leases AS lease
        WHERE lease.operation_id=semantic_proposals.operation_id
          AND lease.run_id=semantic_proposals.claimed_run_id
       HAVING COUNT(DISTINCT lease.owner_id)=1
   )
 WHERE status='claimed' AND claimed_owner_id IS NULL;

UPDATE semantic_proposals
   SET claimed_owner_id=(
       SELECT MIN(retired.owner_id)
         FROM operation_run_retirements AS retired
        WHERE retired.operation_id=semantic_proposals.operation_id
          AND retired.run_id=semantic_proposals.claimed_run_id
       HAVING COUNT(DISTINCT retired.owner_id)=1
   )
 WHERE status='claimed' AND claimed_owner_id IS NULL;

CREATE TABLE migration_43_claim_owner_guard (value INTEGER);
CREATE TRIGGER migration_43_claim_owner_guard_abort
BEFORE INSERT ON migration_43_claim_owner_guard
WHEN NEW.value=0
BEGIN SELECT RAISE(ABORT, 'schema 43 cannot prove owner of claimed semantic proposal'); END;
INSERT INTO migration_43_claim_owner_guard(value)
SELECT 0 FROM semantic_proposals
 WHERE status='claimed' AND claimed_owner_id IS NULL
 LIMIT 1;
DROP TRIGGER migration_43_claim_owner_guard_abort;
DROP TABLE migration_43_claim_owner_guard;

CREATE TRIGGER semantic_proposals_claim_owner_consistency_update
BEFORE UPDATE OF status,claimed_owner_id,claimed_run_id ON semantic_proposals
WHEN (NEW.claimed_owner_id IS NOT NULL AND NEW.claimed_run_id IS NULL)
  OR (NEW.status IN ('pending','approved','rejected','stale') AND NEW.claimed_owner_id IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'semantic proposal claim owner is inconsistent'); END;

CREATE TABLE operation_run_revocations (
    revocation_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    owner_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    source_lease_id TEXT REFERENCES service_leases(lease_id),
    reason TEXT NOT NULL,
    revoked_at TEXT NOT NULL,
    CHECK (length(trim(owner_id))>0),
    CHECK (length(trim(run_id))>0),
    CHECK (length(trim(reason))>0),
    UNIQUE(operation_id, owner_id, run_id)
);
CREATE INDEX operation_run_revocations_operation_idx
    ON operation_run_revocations(operation_id, revoked_at);

INSERT INTO operation_run_revocations(
    revocation_id,operation_id,owner_id,run_id,source_lease_id,reason,revoked_at
)
SELECT retirement_id,operation_id,owner_id,run_id,source_lease_id,reason,retired_at
  FROM operation_run_retirements;

DROP TRIGGER operation_run_retirements_immutable_update;
DROP TRIGGER operation_run_retirements_append_only_delete;
DROP INDEX operation_run_retirements_operation_idx;
DROP TABLE operation_run_retirements;

CREATE TRIGGER operation_run_revocations_immutable_update
BEFORE UPDATE ON operation_run_revocations
BEGIN SELECT RAISE(ABORT, 'operation run revocations are immutable'); END;
CREATE TRIGGER operation_run_revocations_append_only_delete
BEFORE DELETE ON operation_run_revocations
BEGIN SELECT RAISE(ABORT, 'operation run revocations are append-only'); END;
"""


MIGRATIONS = {1: _MIGRATION_1, 2: _MIGRATION_2, 3: _MIGRATION_3, 4: _MIGRATION_4, 5: _MIGRATION_5, 6: _MIGRATION_6, 7: _MIGRATION_7, 8: _MIGRATION_8, 9: _MIGRATION_9, 10: _MIGRATION_10, 11: _MIGRATION_11, 12: _MIGRATION_12, 13: _MIGRATION_13, 14: _MIGRATION_14, 15: _MIGRATION_15, 16: _MIGRATION_16, 17: _MIGRATION_17, 18: _MIGRATION_18, 19: _MIGRATION_19, 20: _MIGRATION_20, 21: _MIGRATION_21, 22: _MIGRATION_22, 23: _MIGRATION_23, 24: _MIGRATION_24, 25: _MIGRATION_25, 26: _MIGRATION_26, 27: _MIGRATION_27, 28: _MIGRATION_28, 29: _MIGRATION_29, 30: _MIGRATION_30, 31: _MIGRATION_31, 32: _MIGRATION_32, 33: _MIGRATION_33, 34: _MIGRATION_34, 35: _MIGRATION_35, 36: _MIGRATION_36, 37: _MIGRATION_37, 38: _MIGRATION_38, 39: _MIGRATION_39, 40: _MIGRATION_40, 41: _MIGRATION_41, 42: _MIGRATION_42, 43: _MIGRATION_43}


def _content_digest(title: str, notes: str) -> str:
    clean_title = str(title).replace("\r\n", "\n")
    clean_notes = str(notes).replace("\r\n", "\n")
    payload = (
        len(clean_title.encode("utf-8")).to_bytes(8, "big") + clean_title.encode("utf-8")
        + len(clean_notes.encode("utf-8")).to_bytes(8, "big") + clean_notes.encode("utf-8")
    )
    return hashlib.sha256(payload).hexdigest()


_SEMANTIC_RECORD_SELECTORS = {
    "content_versions": "content_version_id",
    "task_content_state": "task_gid",
    "write_attempts": "attempt_id",
    "verification_cycles": "cycle_id",
    "marco_authorizations": "authorization_id",
    "dish_inspect_facts": "fact_id",
    "planning_reopen_attempts": "attempt_id",
    "planning_intent_challenges": "challenge_id",
    "backup_creations": "request_id",
    "service_requests": "request_id",
    "operations": "operation_id",
    "service_leases": "lease_id",
    "operation_execution_claims": "claim_id",
    "operation_run_revocations": "revocation_id",
    "operation_executions": "execution_id",
    "abandonment_attempts": "abandonment_id",
    "operation_successions": "succession_id",
    "safe_reclaims": "reclaim_id",
    "verification_hold_resets": "reset_id",
}
_SEMANTIC_PROVENANCE_FIELDS = (
    "task_gid", "operation_id", "request_id", "execution_id", "command",
    "challenge_id", "created_request_id", "claimed_request_id",
    "run_id", "actor_run_id", "owner_id", "claimed_owner_id", "cycle_id", "source_cycle_id",
)
_SEMANTIC_TIMESTAMP_FIELDS = (
    "created_at", "confirmed_at", "started_at", "finished_at", "completed_at",
    "acquired_at", "renewed_at", "expires_at", "released_at", "reserved_at",
    "claimed_at", "consumed_at", "resolved_at", "process_start", "expected_modified_at",
    "confirmed_modified_at", "content_write_completed_at",
    "signoff_completed_at", "movement_completed_at", "revoked_at",
)


def _semantic_record_row(
    conn: sqlite3.Connection, record_type: str, record_id: Any
) -> sqlite3.Row | None:
    selector = _SEMANTIC_RECORD_SELECTORS.get(record_type)
    if selector is None:
        return None
    return conn.execute(
        f"SELECT * FROM {record_type} WHERE {selector}=? LIMIT 1", (record_id,)
    ).fetchone()


def _semantic_selector(
    row: sqlite3.Row | None, field: str
) -> dict[str, str] | None:
    if row is None or field not in row.keys() or row[field] in {None, ""}:
        return None
    return {field: str(row[field])}


def _semantic_relationship(
    invariant: str,
    record_type: str,
    record_id: Any,
    row: sqlite3.Row | None,
) -> dict[str, Any]:
    """Describe the exact failed predicate without exposing governed payloads."""

    same_record = {"record_type": record_type, "record_id": str(record_id)}
    relationships: dict[str, dict[str, Any]] = {
        "content_identity_mismatch": {
            "source_fields": ["title", "notes"],
            "targets": [{**same_record, "fields": ["identity"]}],
            "required_predicate": "content_digest(title, notes) == identity",
        },
        "task_content_head_binding": {
            "source_fields": [
                "last_confirmed_content_version_id", "task_gid",
                "last_confirmed_identity", "last_confirmed_title", "last_confirmed_notes",
            ],
            "targets": [{
                "record_type": "content_versions",
                "selector": _semantic_selector(row, "last_confirmed_content_version_id"),
                "fields": ["task_gid", "identity", "title", "notes", "confirmed"],
            }],
            "required_predicate": (
                "selected content_versions row exists, is confirmed, and exactly matches "
                "the task_content_state head"
            ),
        },
        "confirmed_write_binding": {
            "source_fields": [
                "confirmed_content_version_id", "operation_id", "intended_identity", "outcome"
            ],
            "targets": [{
                "record_type": "content_versions",
                "selector": _semantic_selector(row, "confirmed_content_version_id"),
                "fields": ["operation_id", "identity", "confirmed"],
            }],
            "required_predicate": (
                "confirmed_content_version_id selects a confirmed version for the same operation "
                "whose identity equals intended_identity"
            ),
        },
        "verification_protocol_identity": {
            "source_fields": ["protocol_text", "protocol_release"],
            "targets": [{**same_record, "fields": ["protocol_release"]}],
            "required_predicate": "sha256(protocol_text) == digest encoded by protocol_release",
        },
        "verification_cycle_sequence": {
            "source_fields": ["task_gid"],
            "targets": [{
                "record_type": "verification_cycles",
                "selector": {"task_gid": str(record_id)},
                "fields": ["cycle_number"],
            }],
            "required_predicate": "cycle_number values are the contiguous sequence 1..max(cycle_number)",
        },
        "consumed_authorization_binding": {
            "source_fields": [
                "consumed_at", "consumed_identity", "reserved_by_operation_id", "reserved_at"
            ],
            "targets": [{**same_record, "fields": [
                "consumed_identity", "reserved_by_operation_id", "reserved_at"
            ]}],
            "required_predicate": (
                "consumed_at implies non-empty consumed_identity, reserved_by_operation_id, and reserved_at"
            ),
        },
        "hold_baseline_binding": {
            "source_fields": [
                "hold_content_version_id", "hold_identity", "hold_section_gid", "operation_id", "task_gid"
            ],
            "targets": [{
                "record_type": "content_versions",
                "selector": _semantic_selector(row, "hold_content_version_id"),
                "fields": ["operation_id", "task_gid", "identity", "confirmed"],
            }],
            "required_predicate": (
                "hold content exists, is confirmed, and matches the cycle operation, task, and hold identity "
                "unless migration reconciliation is explicitly required"
            ),
        },
        "approved_cycle_binding": {
            "source_fields": [
                "completed_at", "signed_content_version_id", "signed_identity", "operation_id", "task_gid"
            ],
            "targets": [{
                "record_type": "content_versions",
                "selector": _semantic_selector(row, "signed_content_version_id"),
                "fields": ["operation_id", "task_gid", "identity", "confirmed"],
            }],
            "required_predicate": (
                "approved cycle is completed and its signed version is confirmed for the same operation and task "
                "with identity equal to signed_identity"
            ),
        },
        "small_correction_lineage": {
            "source_fields": [
                "operation_id", "task_gid", "cycle_id", "correction_class",
                "reviewed_identity", "signed_content_version_id", "signed_identity",
            ],
            "targets": [
                {
                    "record_type": "write_attempts",
                    "selector_fields": [
                        "operation_id", "purpose=signoff", "outcome=confirmed",
                        "confirmed_content_version_id=signed_content_version_id",
                        "intended_identity=signed_identity", "context_json.cycle_id=cycle_id",
                        "context_json.correction_class=small",
                    ],
                    "fields": ["expected_identity"],
                },
                {
                    "record_type": "content_versions",
                    "selector_fields": [
                        "operation_id", "task_gid", "identity=signoff.expected_identity",
                        "confirmed=1",
                    ],
                    "fields": ["content_version_id", "identity"],
                },
                {
                    "record_type": "write_attempts",
                    "selector_fields": [
                        "operation_id", "purpose=content_write", "outcome=confirmed",
                        "expected_identity=reviewed_identity",
                        "intended_identity=corrected_content.identity",
                        "confirmed_content_version_id=corrected_content.content_version_id",
                    ],
                },
            ],
            "required_predicate": (
                "an approved small correction whose reviewed and signed identities differ has a matching "
                "confirmed signoff attempt, a confirmed corrected content version, and the confirmed "
                "content-write attempt that produced that version from reviewed_identity"
            ),
        },
        "dish_inspect_fact_binding": {
            "source_fields": [
                "cycle_id", "operation_id", "task_gid", "reviewed_content_version_id",
                "reviewed_identity", "verifier_agent", "run_id", "independence_attestation",
            ],
            "targets": [
                {
                    "record_type": "verification_cycles",
                    "selector": _semantic_selector(row, "cycle_id"),
                    "fields": [
                        "operation_id", "task_gid", "reviewed_content_version_id", "reviewed_identity",
                        "verifier_agent", "run_id", "independence_attestation",
                    ],
                },
                {
                    "record_type": "content_versions",
                    "selector": _semantic_selector(row, "reviewed_content_version_id"),
                    "fields": ["operation_id", "task_gid", "identity", "confirmed"],
                },
                {
                    "record_type": "operation_actor_facts",
                    "selector_fields": [
                        "operation_id", "task_gid", "role=verifier", "verifier_agent", "run_id",
                        "independence_attestation", "reviewed_identity", "cycle_id",
                    ],
                },
            ],
            "required_predicate": (
                "inspect fact exactly matches its cycle, confirmed reviewed content version, and verifier actor fact"
            ),
        },
        "planning_reopen_completion": {
            "source_fields": ["outcome", "finished_at"],
            "targets": [{**same_record, "fields": ["finished_at"]}],
            "required_predicate": "outcome=confirmed implies finished_at is present",
        },
        "planning_reopen_pending": {
            "source_fields": ["outcome", "finished_at"],
            "targets": [{**same_record, "fields": ["finished_at"]}],
            "required_predicate": "outcome=started implies finished_at is absent",
        },
        "backup_creation_request_binding": {
            "source_fields": [
                "request_id", "backup_id", "status", "sha256", "size_bytes"
            ],
            "targets": [{
                "record_type": "service_requests",
                "selector": _semantic_selector(row, "request_id"),
                "fields": [
                    "command", "status", "result_json", "resolution_result_json",
                    "completed_at", "resolved_at",
                ],
            }],
            "required_predicate": (
                "confirmed backup creation is bound to its exact backup-create request; "
                "when the authoritative request result is successful, its metadata matches exactly"
            ),
        },
        "backup_creation_result_missing": {
            "source_fields": ["request_id", "command", "status", "result_json"],
            "targets": [{
                "record_type": "backup_creations",
                "selector": _semantic_selector(row, "request_id"),
                "fields": ["backup_id", "status", "sha256", "size_bytes"],
            }],
            "required_predicate": (
                "every authoritative successful backup-create result has one confirmed "
                "backup_creations row with the same backup identity and metadata"
            ),
        },
        "change_operation_intent_binding": {
            "source_fields": ["operation_kind", "operation_id"],
            "targets": [{
                "record_type": "operation_steps",
                "selector_fields": ["operation_id", "step_name=change_intent"],
                "fields": ["intended_json", "completed_at"],
            }],
            "required_predicate": (
                "every change operation has one completed change_intent step with level small or large "
                "and a non-empty reason"
            ),
        },
        "completed_operation_state": {
            "source_fields": [
                "status", "completed_at", "phase", "terminal_outcome", "schema_version", "expected_identity"
            ],
            "targets": [{**same_record, "fields": [
                "completed_at", "phase", "terminal_outcome", "schema_version", "expected_identity"
            ]}],
            "required_predicate": (
                "status=completed implies completed_at, terminal phase, terminal_outcome, schema_version, "
                "and expected_identity are present"
            ),
        },
        "active_operation_placement_unbound": {
            "source_fields": ["status", "expected_section_gid", "migration_reconciliation_required"],
            "targets": [{**same_record, "fields": ["expected_section_gid"]}],
            "required_predicate": (
                "open or uncertain operation has expected_section_gid unless migration reconciliation is required"
            ),
        },
        "migration_reconciliation_reason_missing": {
            "source_fields": ["migration_reconciliation_required", "migration_reconciliation_reason"],
            "targets": [{**same_record, "fields": ["migration_reconciliation_reason"]}],
            "required_predicate": (
                "migration_reconciliation_required=1 implies a non-empty migration_reconciliation_reason"
            ),
        },
        "non_material_signoff_binding": {
            "source_fields": [
                "terminal_outcome", "inherited_signoff_cycle_id", "expected_identity", "task_gid"
            ],
            "targets": [{
                "record_type": "verification_cycles",
                "selector": _semantic_selector(row, "inherited_signoff_cycle_id"),
                "fields": ["signed_content_version_id", "signed_identity", "outcome", "completed_at"],
            }],
            "required_predicate": (
                "non-material completion inherits a completed approved cycle with confirmed signed content for "
                "the same task, directly or through confirmed non-material lineage to expected_identity"
            ),
        },
        "operation_signoff_binding": {
            "source_fields": ["operation_id", "signoff_completed_at"],
            "targets": [{
                "record_type": "verification_cycles",
                "selector": _semantic_selector(row, "operation_id"),
                "selector_field": "operation_id",
                "fields": ["outcome", "signed_identity", "signed_content_version_id"],
            }],
            "required_predicate": (
                "signoff_completed_at implies an approved verification cycle with signed identity and version"
            ),
        },
        "active_lease_on_incomplete_terminal_operation": {
            "source_fields": ["operation_id", "released_at"],
            "targets": [{
                "record_type": "operations",
                "selector": _semantic_selector(row, "operation_id"),
                "fields": ["status", "phase", "completed_at", "terminal_outcome"],
            }, {
                "record_type": "operation_steps/write_attempts/movement_attempts",
                "selector": _semantic_selector(row, "operation_id"),
                "fields": ["completed_at", "outcome"],
            }],
            "required_predicate": (
                "active cleanup-tail lease on a terminal operation requires complete terminal state, all steps "
                "completed, and no started or uncertain external-effect attempts"
            ),
        },
        "operation_execution_claim_binding": {
            "source_fields": ["operation_id", "execution_id"],
            "targets": [{
                "record_type": "operation_executions",
                "selector": _semantic_selector(row, "execution_id"),
                "fields": ["operation_id", "status", "resolved_at"],
            }],
            "required_predicate": (
                "claim execution exists, belongs to the same operation, and is started or unresolved uncertain"
            ),
        },
        "started_operation_execution_unclaimed": {
            "source_fields": ["execution_id", "status"],
            "targets": [{
                "record_type": "operation_execution_claims",
                "selector": _semantic_selector(row, "execution_id"),
                "selector_field": "execution_id",
            }],
            "required_predicate": "status=started implies exactly one execution claim exists",
        },
        "completed_operation_execution_claimed": {
            "source_fields": ["execution_id", "status", "resolved_at"],
            "targets": [{
                "record_type": "operation_execution_claims",
                "selector": _semantic_selector(row, "execution_id"),
                "selector_field": "execution_id",
            }],
            "required_predicate": (
                "only started or unresolved uncertain executions may retain an execution claim"
            ),
        },
        "operation_execution_evidence_document": {
            "source_fields": ["evidence_json"],
            "targets": [{**same_record, "fields": ["evidence_json"]}],
            "required_predicate": "evidence_json is a JSON object",
        },
        "operation_execution_evidence_binding": {
            "source_fields": ["execution_id", "operation_id", "evidence_json"],
            "targets": [{**same_record, "fields": ["execution_id", "operation_id"]}],
            "required_predicate": (
                "evidence_json.execution_id and evidence_json.operation_id equal the owning row identifiers"
            ),
        },
        "planning_intent_creation_binding": {
            "source_fields": ["challenge_id", "created_request_id", "owner_id", "run_id", "task_gid"],
            "targets": [{
                "record_type": "service_requests",
                "selector": _semantic_selector(row, "created_request_id"),
                "fields": ["command", "owner_id", "run_id", "status", "result_json"],
            }],
            "required_predicate": (
                "created request is the completed matching start request whose result returns this exact challenge"
            ),
        },
        "planning_intent_claim_binding": {
            "source_fields": ["challenge_id", "claimed_request_id", "owner_id", "run_id"],
            "targets": [{
                "record_type": "service_requests",
                "selector": _semantic_selector(row, "claimed_request_id"),
                "fields": ["command", "owner_id", "run_id", "request_id"],
            }],
            "required_predicate": (
                "claimed request is a distinct matching start request for the same authenticated owner and run"
            ),
        },
        "planning_intent_operation_binding": {
            "source_fields": ["challenge_id", "operation_id", "task_gid", "run_id"],
            "targets": [{
                "record_type": "operations",
                "selector": _semantic_selector(row, "operation_id"),
                "fields": ["operation_kind", "task_gid", "run_id"],
            }],
            "required_predicate": (
                "consumed challenge selects one Planning operation for the same task and run"
            ),
        },
        "verification_hold_reset_binding": {
            "source_fields": ["operation_id", "source_cycle_id", "candidate_identity"],
            "targets": [{
                "record_type": "content_versions",
                "selector_fields": ["operation_id", "candidate_identity"],
                "fields": ["confirmed"],
            }, {
                "record_type": "verification_cycles",
                "selector": _semantic_selector(row, "source_cycle_id"),
                "fields": ["operation_id", "outcome"],
            }],
            "required_predicate": (
                "confirmed candidate content exists for the operation and source cycle belongs to the same "
                "operation with outcome=verification-hold"
            ),
        },
        "audit_operation_execution_binding": {
            "source_fields": ["operation_id", "operation_execution_id"],
            "targets": [{
                "record_type": "operation_executions",
                "selector": _semantic_selector(row, "operation_execution_id"),
                "fields": ["operation_id", "status", "resolved_at"],
            }],
            "required_predicate": (
                "operation_execution_id selects an execution owned by the same operation as the audit event"
            ),
        },
        "abandonment_attempt_authority_binding": {
            "source_fields": [
                "source_operation_id", "source_lease_id", "task_gid",
                "abandoned_owner_id", "abandoned_run_id", "attempt_cycle_id",
            ],
            "targets": [{
                "record_type": "service_leases",
                "selector": _semantic_selector(row, "source_lease_id"),
                "fields": [
                    "operation_id", "task_gid", "owner_id", "run_id",
                    "lease_kind", "actor_attempt_seq", "context_cycle_id",
                ],
            }],
            "required_predicate": (
                "the source actor lease exists and exactly matches the abandoned operation, task, owner, run, "
                "and attempt cycle"
            ),
        },
        "abandonment_attempt_cycle_binding": {
            "source_fields": ["attempt_cycle_id", "source_operation_id", "task_gid", "abandoned_run_id"],
            "targets": [{
                "record_type": "verification_cycles",
                "selector": _semantic_selector(row, "attempt_cycle_id"),
                "fields": ["operation_id", "task_gid", "run_id"],
            }],
            "required_predicate": (
                "attempt_cycle_id selects a cycle for the same source operation, task, and abandoned run"
            ),
        },
        "abandonment_succession_binding": {
            "source_fields": [
                "abandonment_id", "source_operation_id", "successor_operation_id", "successor_cycle_id"
            ],
            "targets": [{
                "record_type": "operation_successions",
                "selector_fields": ["abandonment_id"],
                "fields": ["source_operation_id", "successor_operation_id", "successor_cycle_id"],
            }],
            "required_predicate": (
                "the succession for this abandonment exactly matches its source operation, successor operation, "
                "and successor cycle"
            ),
        },
        "abandonment_unexpected_succession": {
            "source_fields": ["abandonment_id", "successor_operation_id"],
            "targets": [{
                "record_type": "operation_successions",
                "selector_fields": ["abandonment_id"],
                "fields": ["succession_id", "successor_operation_id"],
            }],
            "required_predicate": (
                "no succession exists while the abandonment has no successor_operation_id"
            ),
        },
        "abandonment_prepared_successor_missing": {
            "source_fields": ["abandonment_id", "status", "successor_operation_id"],
            "targets": [{
                "record_type": "operation_successions",
                "selector_fields": ["abandonment_id"],
                "fields": ["succession_id", "successor_operation_id"],
            }],
            "required_predicate": (
                "status=awaiting_successor_claim implies one durable operation succession exists"
            ),
        },
        "abandonment_execution_binding": {
            "source_fields": ["current_execution_id", "source_operation_id"],
            "targets": [{
                "record_type": "operation_executions",
                "selector": _semantic_selector(row, "current_execution_id"),
                "fields": ["operation_id", "status", "resolved_at"],
            }],
            "required_predicate": (
                "current_execution_id selects an execution owned by the abandoned source operation"
            ),
        },
        "operation_succession_binding": {
            "source_fields": [
                "succession_id", "task_gid", "source_operation_id", "successor_operation_id",
                "abandonment_id", "source_content_version_id", "successor_content_version_id",
            ],
            "targets": [
                {
                    "record_type": "operations",
                    "selector_fields": ["source_operation_id", "successor_operation_id"],
                    "fields": ["task_gid", "status", "terminal_outcome", "expected_identity"],
                },
                {
                    "record_type": "abandonment_attempts",
                    "selector": _semantic_selector(row, "abandonment_id"),
                    "fields": ["source_operation_id", "successor_operation_id"],
                },
                {
                    "record_type": "content_versions",
                    "selector_fields": ["source_content_version_id", "successor_content_version_id"],
                    "fields": ["operation_id", "task_gid", "identity", "title", "notes", "boundary", "confirmed"],
                },
            ],
            "required_predicate": (
                "source, successor, abandonment, and confirmed content versions form one exact immutable "
                "agent-abandonment succession chain"
            ),
        },
        "agent_abandoned_source_terminal_binding": {
            "source_fields": ["operation_id", "status", "terminal_outcome"],
            "targets": [
                {
                    "record_type": "operation_successions",
                    "selector_fields": ["source_operation_id=operation_id"],
                    "fields": ["succession_id", "successor_operation_id"],
                },
                {
                    "record_type": "operation_steps/write_attempts/movement_attempts",
                    "selector": _semantic_selector(row, "operation_id"),
                    "fields": ["completed_at", "outcome"],
                },
            ],
            "required_predicate": (
                "an agent-abandoned terminal operation has a succession, no incomplete steps, and no started or "
                "uncertain external-effect attempts"
            ),
        },
        "prepared_successor_authority_binding": {
            "source_fields": ["operation_id", "status", "successor_claim_mode"],
            "targets": [
                {
                    "record_type": "operation_successions",
                    "selector_fields": ["successor_operation_id=operation_id"],
                    "fields": ["succession_id", "source_operation_id"],
                },
                {
                    "record_type": "service_leases",
                    "selector": _semantic_selector(row, "operation_id"),
                    "selector_field": "operation_id",
                    "fields": ["released_at"],
                },
            ],
            "required_predicate": (
                "a prepared successor is open, has one durable succession, and has no active lease before claim"
            ),
        },
        "abandoned_verification_cycle_binding": {
            "source_fields": [
                "cycle_id", "operation_id", "outcome", "completed_at",
                "signed_content_version_id", "signed_identity",
            ],
            "targets": [{
                "record_type": "operations",
                "selector": _semantic_selector(row, "operation_id"),
                "fields": ["status", "terminal_outcome"],
            }],
            "required_predicate": (
                "an abandoned verification cycle is completed, has no signed identity/version, and belongs to "
                "an agent-abandoned cancelled operation"
            ),
        },
        "safe_reclaim_binding": {
            "source_fields": [
                "reclaim_id", "task_gid", "source_operation_id", "successor_operation_id",
                "source_lease_id", "request_id", "previous_owner_id", "previous_run_id",
                "requested_owner_id", "requested_run_id", "source_content_version_id",
                "successor_content_version_id",
            ],
            "targets": [
                {
                    "record_type": "operations",
                    "selector_fields": ["source_operation_id", "successor_operation_id"],
                    "fields": ["task_gid", "status", "terminal_outcome"],
                },
                {
                    "record_type": "service_leases",
                    "selector": _semantic_selector(row, "source_lease_id"),
                    "fields": ["operation_id", "owner_id", "run_id"],
                },
                {
                    "record_type": "service_requests",
                    "selector": _semantic_selector(row, "request_id"),
                    "fields": ["command", "owner_id", "run_id"],
                },
                {
                    "record_type": "content_versions",
                    "selector_fields": ["source_content_version_id", "successor_content_version_id"],
                    "fields": ["operation_id", "task_gid", "identity", "title", "notes", "boundary", "confirmed"],
                },
            ],
            "required_predicate": (
                "safe reclaim binds one cancelled source, its exact inactive lease/request authority, "
                "one successor, and byte-equivalent confirmed source/successor content baselines"
            ),
        },
        "safe_reclaim_prepared_successor_binding": {
            "source_fields": ["successor_operation_id", "stage", "status"],
            "targets": [{
                "record_type": "operations",
                "selector": _semantic_selector(row, "successor_operation_id"),
                "fields": ["successor_claim_mode", "status"],
            }],
            "required_predicate": (
                "a prepared safe-reclaim successor advertises verifier claim mode for Verification "
                "or stage_actor claim mode for Planning/Research"
            ),
        },
        "safe_reclaim_claimed_successor_binding": {
            "source_fields": ["successor_operation_id", "status", "claimed_at"],
            "targets": [{
                "record_type": "operations",
                "selector": _semantic_selector(row, "successor_operation_id"),
                "fields": ["successor_claim_mode"],
            }],
            "required_predicate": "a claimed safe-reclaim successor has consumed its prepared claim mode",
        },
        "safe_reclaim_cycle_binding": {
            "source_fields": [
                "stage", "source_operation_id", "successor_operation_id",
                "source_cycle_id", "successor_cycle_id",
            ],
            "targets": [{
                "record_type": "verification_cycles",
                "selector_fields": ["source_cycle_id", "successor_cycle_id"],
                "fields": ["operation_id", "outcome", "completed_at"],
            }],
            "required_predicate": (
                "Verification safe reclaim closes the exact source cycle as safe_reclaimed and creates "
                "a successor cycle bound to the successor operation"
            ),
        },
        "safe_reclaimed_source_terminal_binding": {
            "source_fields": ["operation_id", "status", "terminal_outcome"],
            "targets": [
                {
                    "record_type": "safe_reclaims",
                    "selector_fields": ["source_operation_id=operation_id"],
                    "fields": ["reclaim_id", "successor_operation_id"],
                },
                {
                    "record_type": "operation_steps/write_attempts/movement_attempts",
                    "selector": _semantic_selector(row, "operation_id"),
                    "fields": ["completed_at", "outcome"],
                },
            ],
            "required_predicate": (
                "a safe-reclaimed source has one reclaim lineage, no incomplete workflow step, and no "
                "started or uncertain external-effect attempt"
            ),
        },
        "safe_reclaimed_verification_cycle_binding": {
            "source_fields": [
                "cycle_id", "operation_id", "outcome", "completed_at",
                "signed_content_version_id", "signed_identity",
            ],
            "targets": [{
                "record_type": "operations",
                "selector": _semantic_selector(row, "operation_id"),
                "fields": ["status", "terminal_outcome"],
            }],
            "required_predicate": (
                "a safe-reclaimed Verification cycle is completed, unsigned, and belongs to the exact "
                "safe-reclaimed cancelled source operation"
            ),
        },
    }
    if invariant.startswith("multiple_unresolved_"):
        target = invariant.removeprefix("multiple_unresolved_")
        return {
            "source_fields": ["operation_id"],
            "targets": [{
                "record_type": target,
                "selector": {"operation_id": str(record_id)},
                "fields": ["outcome"],
            }],
            "required_predicate": "at most one row per operation has outcome started or uncertain",
        }
    return relationships.get(invariant, {
        "source_fields": [],
        "targets": [same_record],
        "required_predicate": invariant,
    })


def _semantic_problem(
    conn: sqlite3.Connection,
    invariant: str,
    record_type: str,
    record_id: Any,
    *,
    related_record_type: str | None = None,
    related_record_id: Any | None = None,
    observed_count: int | None = None,
) -> dict[str, Any]:
    """Build a payload-safe diagnostic with exact relationship and provenance."""

    row = _semantic_record_row(conn, record_type, record_id)
    problem: dict[str, Any] = {
        "invariant": invariant,
        "record_type": record_type,
        "record_id": str(record_id),
        "broken_relationship": _semantic_relationship(
            invariant, record_type, record_id, row
        ),
    }
    if row is None and record_type == "task_verification_cycles":
        problem["mutation_provenance"] = {"task_gid": str(record_id)}
    if row is not None:
        provenance = {
            field: row[field]
            for field in _SEMANTIC_PROVENANCE_FIELDS
            if field in row.keys() and row[field] not in {None, ""}
        }
        timestamps = {
            field: row[field]
            for field in _SEMANTIC_TIMESTAMP_FIELDS
            if field in row.keys() and row[field] not in {None, ""}
        }
        if provenance:
            problem["mutation_provenance"] = provenance
        if timestamps:
            problem["timestamps"] = timestamps
    if related_record_type is not None:
        problem["related_record_type"] = related_record_type
        problem["related_record_id"] = str(related_record_id)
    if observed_count is not None:
        problem["observed_count"] = int(observed_count)
    return problem


def _validate_content_and_cycle_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for row in conn.execute("SELECT * FROM content_versions WHERE confirmed=1"):
        if _content_digest(row["title"], row["notes"]) != row["identity"]:
            problems.append(_semantic_problem(conn,
                "content_identity_mismatch", "content_versions", row["content_version_id"],
            ))
    for row in conn.execute("SELECT * FROM task_content_state"):
        bound = conn.execute(
            """SELECT task_gid, identity, title, notes, confirmed
                 FROM content_versions WHERE content_version_id=?""",
            (row["last_confirmed_content_version_id"],),
        ).fetchone()
        if (
            bound is None
            or bound["confirmed"] != 1
            or bound["task_gid"] != row["task_gid"]
            or bound["identity"] != row["last_confirmed_identity"]
            or bound["title"] != row["last_confirmed_title"]
            or bound["notes"] != row["last_confirmed_notes"]
        ):
            problems.append(_semantic_problem(conn,
                "task_content_head_binding", "task_content_state", row["task_gid"],
            ))
    for row in conn.execute("SELECT * FROM write_attempts WHERE outcome='confirmed'"):
        bound = conn.execute(
            "SELECT identity,confirmed,operation_id FROM content_versions WHERE content_version_id=?",
            (row["confirmed_content_version_id"],),
        ).fetchone()
        if bound is None or bound["confirmed"] != 1 or bound["operation_id"] != row["operation_id"] or bound["identity"] != row["intended_identity"]:
            problems.append(_semantic_problem(conn,
                "confirmed_write_binding", "write_attempts", row["attempt_id"],
            ))
    for row in conn.execute("SELECT * FROM verification_cycles"):
        release = str(row["protocol_release"] or "")
        text = str(row["protocol_text"] or "")
        if release.startswith("sha256:") and hashlib.sha256(text.encode("utf-8")).hexdigest() != release.split(":", 1)[1].split(";", 1)[0].strip():
            problems.append(_semantic_problem(conn,
                "verification_protocol_identity", "verification_cycles", row["cycle_id"],
            ))
    for task in conn.execute("SELECT DISTINCT task_gid FROM verification_cycles"):
        numbers = [r[0] for r in conn.execute("SELECT cycle_number FROM verification_cycles WHERE task_gid=? ORDER BY cycle_number", (task[0],))]
        if numbers and numbers != list(range(1, max(numbers) + 1)):
            problems.append(_semantic_problem(conn,
                "verification_cycle_sequence", "task_verification_cycles", task[0],
            ))
    for row in conn.execute("SELECT * FROM marco_authorizations WHERE consumed_at IS NOT NULL"):
        if not row["consumed_identity"] or not row["reserved_by_operation_id"] or not row["reserved_at"]:
            problems.append(_semantic_problem(conn,
                "consumed_authorization_binding", "marco_authorizations", row["authorization_id"],
            ))
    for row in conn.execute(
        """SELECT cycle.*, operation.migration_reconciliation_required
             FROM verification_cycles AS cycle
             JOIN operations AS operation ON operation.operation_id=cycle.operation_id
            WHERE cycle.completed_at IS NOT NULL
              AND (cycle.route IN ('evidence','human_review') OR cycle.outcome='verification-hold')"""
    ):
        held = conn.execute(
            "SELECT task_gid,operation_id,identity,confirmed FROM content_versions WHERE content_version_id=?",
            (row["hold_content_version_id"],),
        ).fetchone()
        valid = bool(
            row["hold_identity"] and row["hold_section_gid"] and held is not None
            and held["confirmed"] == 1 and held["task_gid"] == row["task_gid"]
            and held["operation_id"] == row["operation_id"]
            and held["identity"] == row["hold_identity"]
        )
        if not valid and row["migration_reconciliation_required"] != 1:
            problems.append(_semantic_problem(conn,
                "hold_baseline_binding", "verification_cycles", row["cycle_id"],
            ))
    for row in conn.execute("SELECT * FROM verification_cycles WHERE outcome='approved'"):
        signed = conn.execute(
            "SELECT identity,confirmed,operation_id,task_gid FROM content_versions WHERE content_version_id=?",
            (row["signed_content_version_id"],),
        ).fetchone()
        if (row["completed_at"] is None or row["signed_identity"] is None or signed is None
                or signed["confirmed"] != 1 or signed["operation_id"] != row["operation_id"]
                or signed["task_gid"] != row["task_gid"] or signed["identity"] != row["signed_identity"]):
            problems.append(_semantic_problem(conn,
                "approved_cycle_binding", "verification_cycles", row["cycle_id"],
            ))
    for row in conn.execute(
        """SELECT cycle.*
             FROM verification_cycles AS cycle
             JOIN operations AS operation ON operation.operation_id=cycle.operation_id
            WHERE cycle.outcome='approved' AND cycle.correction_class='small'
              AND cycle.reviewed_identity IS NOT cycle.signed_identity
              AND operation.migration_reconciliation_required != 1"""
    ):
        signoff_attempt = None
        for attempt in conn.execute(
            """SELECT * FROM write_attempts
                 WHERE operation_id=? AND purpose='signoff' AND outcome='confirmed'
                   AND confirmed_content_version_id=? AND intended_identity=?
                 ORDER BY started_at DESC, rowid DESC""",
            (
                row["operation_id"],
                row["signed_content_version_id"],
                row["signed_identity"],
            ),
        ):
            try:
                context = json.loads(attempt["context_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if (
                isinstance(context, dict)
                and context.get("cycle_id") == row["cycle_id"]
                and context.get("correction_class") == "small"
            ):
                signoff_attempt = attempt
                break
        corrected_version = None
        correction_write = None
        if signoff_attempt is not None:
            corrected_version = conn.execute(
                """SELECT content_version_id,operation_id,task_gid,identity,confirmed
                     FROM content_versions
                    WHERE operation_id=? AND task_gid=? AND identity=? AND confirmed=1
                    ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                (
                    row["operation_id"],
                    row["task_gid"],
                    signoff_attempt["expected_identity"],
                ),
            ).fetchone()
        if corrected_version is not None:
            correction_write = conn.execute(
                """SELECT attempt_id FROM write_attempts
                     WHERE operation_id=? AND purpose='content_write' AND outcome='confirmed'
                       AND expected_identity=? AND intended_identity=?
                       AND confirmed_content_version_id=?
                     ORDER BY started_at DESC, rowid DESC LIMIT 1""",
                (
                    row["operation_id"],
                    row["reviewed_identity"],
                    corrected_version["identity"],
                    corrected_version["content_version_id"],
                ),
            ).fetchone()
        if signoff_attempt is None or corrected_version is None or correction_write is None:
            problems.append(_semantic_problem(conn,
                "small_correction_lineage", "verification_cycles", row["cycle_id"],
            ))

def _validate_operation_and_inspection_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for row in conn.execute("SELECT * FROM dish_inspect_facts"):
        cycle = conn.execute(
            """SELECT operation_id,task_gid,reviewed_content_version_id,reviewed_identity,
                      verifier_agent,run_id,independence_attestation
                 FROM verification_cycles WHERE cycle_id=?""",
            (row["cycle_id"],),
        ).fetchone()
        version = conn.execute(
            "SELECT operation_id,task_gid,identity,confirmed FROM content_versions WHERE content_version_id=?",
            (row["reviewed_content_version_id"],),
        ).fetchone()
        actor = conn.execute(
            """SELECT 1 FROM operation_actor_facts
                 WHERE operation_id=? AND task_gid=? AND role='verifier'
                   AND agent=? AND run_id=?
                   AND COALESCE(independence_attestation,'')=COALESCE(?, '')
                   AND candidate_identity=? AND source_cycle_id=? LIMIT 1""",
            (row["operation_id"], row["task_gid"], row["verifier_agent"], row["run_id"],
             row["independence_attestation"], row["reviewed_identity"], row["cycle_id"]),
        ).fetchone()
        if (
            cycle is None or version is None or actor is None
            or cycle["operation_id"] != row["operation_id"]
            or cycle["task_gid"] != row["task_gid"]
            or cycle["reviewed_content_version_id"] != row["reviewed_content_version_id"]
            or cycle["reviewed_identity"] != row["reviewed_identity"]
            or cycle["verifier_agent"] != row["verifier_agent"]
            or cycle["run_id"] != row["run_id"]
            or (cycle["independence_attestation"] or "") != (row["independence_attestation"] or "")
            or version["operation_id"] != row["operation_id"]
            or version["task_gid"] != row["task_gid"]
            or version["identity"] != row["reviewed_identity"] or version["confirmed"] != 1
        ):
            problems.append(_semantic_problem(conn,
                "dish_inspect_fact_binding", "dish_inspect_facts", row["fact_id"],
            ))
    for row in conn.execute("SELECT * FROM planning_reopen_attempts"):
        if row["outcome"] == "confirmed" and not row["finished_at"]:
            problems.append(_semantic_problem(conn,
                "planning_reopen_completion", "planning_reopen_attempts", row["attempt_id"],
            ))
        if row["outcome"] == "started" and row["finished_at"] is not None:
            problems.append(_semantic_problem(conn,
                "planning_reopen_pending", "planning_reopen_attempts", row["attempt_id"],
            ))
    for row in conn.execute("SELECT * FROM operations"):
        if (
            row["operation_kind"] == "change"
            and row["migration_reconciliation_required"] != 1
        ):
            intent = conn.execute(
                """SELECT intended_json, completed_at FROM operation_steps
                     WHERE operation_id=? AND step_name='change_intent'""",
                (row["operation_id"],),
            ).fetchone()
            try:
                intended = None if intent is None else json.loads(intent["intended_json"])
            except (TypeError, ValueError):
                intended = None
            valid_intent = bool(
                intent is not None
                and intent["completed_at"]
                and isinstance(intended, dict)
                and intended.get("level") in {"small", "large"}
                and isinstance(intended.get("reason"), str)
                and intended["reason"].strip()
            )
            if not valid_intent:
                problems.append(_semantic_problem(
                    conn,
                    "change_operation_intent_binding",
                    "operations",
                    row["operation_id"],
                    related_record_type="operation_steps",
                    related_record_id=f"{row['operation_id']}:change_intent",
                ))
        if row["status"] == "completed" and (row["completed_at"] is None or row["phase"] != "terminal" or not row["terminal_outcome"] or not row["schema_version"] or not row["expected_identity"]):
            problems.append(_semantic_problem(conn,
                "completed_operation_state", "operations", row["operation_id"],
            ))
        if row["status"] in {"open", "uncertain"}:
            if row["expected_section_gid"] is None and row["migration_reconciliation_required"] != 1:
                problems.append(_semantic_problem(conn,
                    "active_operation_placement_unbound", "operations", row["operation_id"],
                ))
            if row["migration_reconciliation_required"] == 1 and not str(row["migration_reconciliation_reason"] or "").strip():
                problems.append(_semantic_problem(conn,
                    "migration_reconciliation_reason_missing", "operations", row["operation_id"],
                ))
        if row["terminal_outcome"] == "non_material_checkin":
            inherited = conn.execute(
                """SELECT cycle.signed_identity, cycle.signed_content_version_id,
                          cycle.outcome, cycle.completed_at, version.identity,
                          version.confirmed, version.task_gid
                     FROM verification_cycles AS cycle
                     LEFT JOIN content_versions AS version
                       ON version.content_version_id=cycle.signed_content_version_id
                    WHERE cycle.cycle_id=?""",
                (row["inherited_signoff_cycle_id"],),
            ).fetchone()
            lineage = conn.execute(
                """SELECT 1
                     FROM operations AS prior
                     JOIN write_attempts AS candidate_write
                       ON candidate_write.operation_id=prior.operation_id
                              AND candidate_write.outcome='confirmed'
                     JOIN content_versions AS candidate
                       ON candidate.content_version_id=candidate_write.confirmed_content_version_id
                    WHERE prior.task_gid=?
                      AND prior.status='completed'
                      AND prior.terminal_outcome='non_material_checkin'
                      AND prior.inherited_signoff_cycle_id=?
                      AND candidate_write.intended_identity=?
                      AND candidate.confirmed=1
                      AND candidate.task_gid=?
                      AND candidate.identity=?
                    LIMIT 1""",
                (
                    row["task_gid"], row["inherited_signoff_cycle_id"],
                    row["expected_identity"], row["task_gid"], row["expected_identity"],
                ),
            ).fetchone()
            if (
                inherited is None or inherited["outcome"] != "approved"
                or inherited["completed_at"] is None
                or inherited["identity"] != inherited["signed_identity"]
                or inherited["confirmed"] != 1 or inherited["task_gid"] != row["task_gid"]
                or (inherited["signed_identity"] != row["expected_identity"] and lineage is None)
            ):
                problems.append(_semantic_problem(conn,
                    "non_material_signoff_binding", "operations", row["operation_id"],
                ))
        if row["signoff_completed_at"] is not None:
            approved = conn.execute(
                "SELECT 1 FROM verification_cycles WHERE operation_id=? AND outcome='approved' AND signed_identity IS NOT NULL AND signed_content_version_id IS NOT NULL",
                (row["operation_id"],),
            ).fetchone()
            if approved is None:
                problems.append(_semantic_problem(conn,
                    "operation_signoff_binding", "operations", row["operation_id"],
                ))

def _validate_execution_and_lease_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for table in ("write_attempts", "movement_attempts"):
        for row in conn.execute(
            f"""SELECT operation_id, COUNT(*) AS unresolved_count
                  FROM {table}
                 WHERE outcome IN ('started','uncertain')
                 GROUP BY operation_id
                HAVING COUNT(*) > 1"""
        ):
            problems.append(_semantic_problem(conn,
                f"multiple_unresolved_{table}",
                "operations",
                row["operation_id"],
                related_record_type=table,
                related_record_id=row["operation_id"],
                observed_count=int(row["unresolved_count"]),
            ))
    # A service lease is transport ownership, not workflow state. Terminal
    # status revokes mutation authority before response cleanup, so a complete
    # terminal operation may retain an active cleanup-tail lease. It is safe
    # only after every declared step and external-effect attempt is resolved;
    # otherwise the terminal row still contradicts its durable evidence.
    for row in conn.execute(
        """SELECT lease.lease_id, lease.operation_id, operation.phase,
                  operation.completed_at, operation.terminal_outcome
             FROM service_leases AS lease
             JOIN operations AS operation ON operation.operation_id=lease.operation_id
            WHERE lease.released_at IS NULL
              AND operation.status IN ('completed','cancelled')"""
    ):
        pending = conn.execute(
            "SELECT 1 FROM operation_steps WHERE operation_id=? AND completed_at IS NULL LIMIT 1",
            (row["operation_id"],),
        ).fetchone()
        unresolved = conn.execute(
            """SELECT 1 FROM write_attempts
                 WHERE operation_id=? AND outcome IN ('started','uncertain')
               UNION ALL
               SELECT 1 FROM movement_attempts
                 WHERE operation_id=? AND outcome IN ('started','uncertain')
               LIMIT 1""",
            (row["operation_id"], row["operation_id"]),
        ).fetchone()
        terminal_incomplete = (
            row["phase"] != "terminal"
            or not row["completed_at"]
            or not row["terminal_outcome"]
        )
        if terminal_incomplete or pending is not None or unresolved is not None:
            problems.append(_semantic_problem(conn,
                "active_lease_on_incomplete_terminal_operation",
                "service_leases",
                row["lease_id"],
                related_record_type="operations",
                related_record_id=row["operation_id"],
            ))
    for row in conn.execute("SELECT * FROM operation_execution_claims"):
        if row["execution_id"] is None:
            continue
        execution = conn.execute(
            "SELECT operation_id,status,resolved_at FROM operation_executions WHERE execution_id=?",
            (row["execution_id"],),
        ).fetchone()
        if (
            execution is None
            or execution["operation_id"] != row["operation_id"]
            or not (
                execution["status"] == "started"
                or (execution["status"] == "uncertain" and execution["resolved_at"] is None)
            )
        ):
            problems.append(_semantic_problem(conn,
                "operation_execution_claim_binding",
                "operation_execution_claims",
                row["claim_id"],
            ))
    for row in conn.execute("SELECT * FROM operation_executions"):
        claim = conn.execute(
            "SELECT 1 FROM operation_execution_claims WHERE execution_id=?",
            (row["execution_id"],),
        ).fetchone()
        if row["status"] == "started" and claim is None:
            problems.append(_semantic_problem(conn,
                "started_operation_execution_unclaimed",
                "operation_executions",
                row["execution_id"],
            ))
        if (
            row["status"] not in {"started", "uncertain"}
            or (row["status"] == "uncertain" and row["resolved_at"] is not None)
        ) and claim is not None:
            problems.append(_semantic_problem(conn,
                "completed_operation_execution_claimed",
                "operation_executions",
                row["execution_id"],
            ))
        if row["evidence_json"]:
            try:
                recovery = json.loads(row["evidence_json"])
            except (TypeError, ValueError):
                problems.append(_semantic_problem(conn,
                    "operation_execution_evidence_document",
                    "operation_executions",
                    row["execution_id"],
                ))
                continue
            if not isinstance(recovery, dict):
                problems.append(_semantic_problem(conn,
                    "operation_execution_evidence_document",
                    "operation_executions",
                    row["execution_id"],
                ))
                continue
            if (
                recovery.get("execution_id") != row["execution_id"]
                or recovery.get("operation_id") != row["operation_id"]
            ):
                problems.append(_semantic_problem(conn,
                    "operation_execution_evidence_binding",
                    "operation_executions",
                    row["execution_id"],
                ))
    for row in conn.execute(
        """SELECT audit.event_id,audit.operation_id,audit.operation_execution_id,
                  execution.operation_id AS execution_operation_id
             FROM audit_events AS audit
             LEFT JOIN operation_executions AS execution
               ON execution.execution_id=audit.operation_execution_id
            WHERE audit.operation_execution_id IS NOT NULL"""
    ):
        if (
            row["execution_operation_id"] is None
            or row["operation_id"] != row["execution_operation_id"]
        ):
            problems.append(_semantic_problem(
                conn,
                "audit_operation_execution_binding",
                "audit_events",
                row["event_id"],
                related_record_type="operation_executions",
                related_record_id=row["operation_execution_id"],
            ))

def _validate_planning_intent_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for row in conn.execute("SELECT * FROM planning_intent_challenges"):
        created = conn.execute(
            "SELECT * FROM service_requests WHERE request_id=?",
            (row["created_request_id"],),
        ).fetchone()
        valid_created = False
        if (
            created is not None
            and created["command"] == "start"
            and created["owner_id"] == row["owner_id"]
            and created["run_id"] == row["run_id"]
            and created["status"] == "completed"
        ):
            try:
                result = json.loads(created["result_json"] or "null")
            except (TypeError, ValueError):
                result = None
            confirmation = (
                (result.get("data") or {}).get("planning_intent_confirmation")
                if isinstance(result, dict)
                else None
            )
            valid_created = bool(
                isinstance(result, dict)
                and result.get("code") == "CONFIRMATION_REQUIRED"
                and result.get("task_gid") == row["task_gid"]
                and (result.get("data") or {}).get("intent_challenge_id")
                == row["challenge_id"]
                and isinstance(confirmation, dict)
                and confirmation.get("challenge_id") == row["challenge_id"]
            )
        if not valid_created:
            problems.append(_semantic_problem(
                conn,
                "planning_intent_creation_binding",
                "planning_intent_challenges",
                row["challenge_id"],
                related_record_type="service_requests",
                related_record_id=row["created_request_id"],
            ))

        if row["status"] in {"claimed", "consumed"}:
            claimed = conn.execute(
                "SELECT * FROM service_requests WHERE request_id=?",
                (row["claimed_request_id"],),
            ).fetchone()
            if (
                claimed is None
                or claimed["command"] != "start"
                or claimed["owner_id"] != row["owner_id"]
                or claimed["run_id"] != row["run_id"]
                or claimed["request_id"] == row["created_request_id"]
            ):
                problems.append(_semantic_problem(
                    conn,
                    "planning_intent_claim_binding",
                    "planning_intent_challenges",
                    row["challenge_id"],
                    related_record_type="service_requests",
                    related_record_id=row["claimed_request_id"],
                ))

        if row["status"] == "consumed":
            operation = conn.execute(
                "SELECT * FROM operations WHERE operation_id=?",
                (row["operation_id"],),
            ).fetchone()
            if (
                operation is None
                or operation["operation_kind"] != "planning"
                or operation["task_gid"] != row["task_gid"]
                or operation["run_id"] != row["run_id"]
            ):
                problems.append(_semantic_problem(
                    conn,
                    "planning_intent_operation_binding",
                    "planning_intent_challenges",
                    row["challenge_id"],
                    related_record_type="operations",
                    related_record_id=row["operation_id"],
                ))


def _validate_backup_and_reset_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    def authoritative_backup_result(request: sqlite3.Row) -> dict[str, Any] | None:
        try:
            encoded = request["resolution_result_json"] or request["result_json"]
            result = json.loads(encoded or "null")
        except (TypeError, ValueError):
            return None
        return result if isinstance(result, dict) else None

    for row in conn.execute("SELECT * FROM backup_creations WHERE status='confirmed'"):
        request = conn.execute(
            "SELECT * FROM service_requests WHERE request_id=?", (row["request_id"],)
        ).fetchone()
        valid = bool(request is not None and request["command"] == "backup-create")
        if valid and request["status"] == "completed":
            result = authoritative_backup_result(request)
            if isinstance(result, dict) and result.get("ok"):
                backup = (result.get("data") or {}).get("backup")
                valid = bool(
                    isinstance(backup, dict)
                    and backup.get("backup_id") == row["backup_id"]
                    and backup.get("sha256") == row["sha256"]
                    and backup.get("size_bytes") == row["size_bytes"]
                )
            # A completed failure with exact confirmed destination evidence is a
            # supported crash/reconciliation frontier. Startup or exact replay
            # may add the successful resolution result without replacing the
            # original first outcome.
        if not valid:
            problems.append(_semantic_problem(
                conn,
                "backup_creation_request_binding",
                "backup_creations",
                row["request_id"],
            ))
    for request in conn.execute(
        "SELECT * FROM service_requests WHERE command='backup-create' AND status='completed'"
    ):
        result = authoritative_backup_result(request)
        if not isinstance(result, dict) or not result.get("ok"):
            continue
        backup = (result.get("data") or {}).get("backup")
        creation = conn.execute(
            "SELECT * FROM backup_creations WHERE request_id=?",
            (request["request_id"],),
        ).fetchone()
        if not (
            creation is not None
            and creation["status"] == "confirmed"
            and isinstance(backup, dict)
            and backup.get("backup_id") == creation["backup_id"]
            and backup.get("sha256") == creation["sha256"]
            and backup.get("size_bytes") == creation["size_bytes"]
        ):
            problems.append(_semantic_problem(
                conn,
                "backup_creation_result_missing",
                "service_requests",
                request["request_id"],
            ))
    for row in conn.execute("SELECT * FROM verification_hold_resets"):
        version = conn.execute(
            """SELECT 1 FROM content_versions
                 WHERE operation_id=? AND identity=? AND confirmed=1 LIMIT 1""",
            (row["operation_id"], row["candidate_identity"]),
        ).fetchone()
        cycle = conn.execute(
            "SELECT 1 FROM verification_cycles WHERE cycle_id=? AND operation_id=? AND outcome='verification-hold'",
            (row["source_cycle_id"], row["operation_id"]),
        ).fetchone()
        if version is None or cycle is None:
            problems.append(_semantic_problem(conn,
                "verification_hold_reset_binding", "verification_hold_resets", row["reset_id"],
            ))

def _validate_operation_run_revocation_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for row in conn.execute("SELECT * FROM operation_run_revocations"):
        operation = conn.execute(
            "SELECT 1 FROM operations WHERE operation_id=?", (row["operation_id"],)
        ).fetchone()
        lease = None
        if row["source_lease_id"] is not None:
            lease = conn.execute(
                "SELECT operation_id,owner_id,run_id FROM service_leases WHERE lease_id=?",
                (row["source_lease_id"],),
            ).fetchone()
        if operation is None or (
            row["source_lease_id"] is not None
            and (
                lease is None
                or lease["operation_id"] != row["operation_id"]
                or lease["owner_id"] != row["owner_id"]
                or lease["run_id"] != row["run_id"]
            )
        ):
            problems.append(_semantic_problem(
                conn,
                "operation_run_revocation_binding",
                "operation_run_revocations",
                row["revocation_id"],
                related_record_type=(
                    "service_leases" if row["source_lease_id"] is not None else "operations"
                ),
                related_record_id=(row["source_lease_id"] or row["operation_id"]),
            ))


def _validate_abandonment_attempt_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for row in conn.execute("SELECT * FROM abandonment_attempts"):
        lease = conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=?", (row["source_lease_id"],)
        ).fetchone()
        source = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (row["source_operation_id"],)
        ).fetchone()
        if (
            lease is None
            or source is None
            or source["task_gid"] != row["task_gid"]
            or lease["operation_id"] != row["source_operation_id"]
            or lease["task_gid"] != row["task_gid"]
            or lease["owner_id"] != row["abandoned_owner_id"]
            or lease["run_id"] != row["abandoned_run_id"]
            or lease["lease_kind"] != "actor"
            or lease["actor_attempt_seq"] is None
            or lease["context_cycle_id"] != row["attempt_cycle_id"]
        ):
            problems.append(_semantic_problem(
                conn,
                "abandonment_attempt_authority_binding",
                "abandonment_attempts",
                row["abandonment_id"],
                related_record_type="service_leases",
                related_record_id=row["source_lease_id"],
            ))
        if row["attempt_cycle_id"] is not None:
            cycle = conn.execute(
                "SELECT operation_id,task_gid,run_id FROM verification_cycles WHERE cycle_id=?",
                (row["attempt_cycle_id"],),
            ).fetchone()
            if (
                cycle is None
                or cycle["operation_id"] != row["source_operation_id"]
                or cycle["task_gid"] != row["task_gid"]
                or cycle["run_id"] != row["abandoned_run_id"]
            ):
                problems.append(_semantic_problem(
                    conn,
                    "abandonment_attempt_cycle_binding",
                    "abandonment_attempts",
                    row["abandonment_id"],
                    related_record_type="verification_cycles",
                    related_record_id=row["attempt_cycle_id"],
                ))
        succession = conn.execute(
            "SELECT * FROM operation_successions WHERE abandonment_id=?",
            (row["abandonment_id"],),
        ).fetchone()
        if row["successor_operation_id"] is not None:
            if (
                succession is None
                or succession["source_operation_id"] != row["source_operation_id"]
                or succession["successor_operation_id"] != row["successor_operation_id"]
                or succession["successor_cycle_id"] != row["successor_cycle_id"]
            ):
                problems.append(_semantic_problem(
                    conn,
                    "abandonment_succession_binding",
                    "abandonment_attempts",
                    row["abandonment_id"],
                    related_record_type="operation_successions",
                    related_record_id=None if succession is None else succession["succession_id"],
                ))
        elif succession is not None:
            problems.append(_semantic_problem(
                conn,
                "abandonment_unexpected_succession",
                "abandonment_attempts",
                row["abandonment_id"],
                related_record_type="operation_successions",
                related_record_id=succession["succession_id"],
            ))
        if row["status"] == "awaiting_successor_claim" and succession is None:
            problems.append(_semantic_problem(
                conn,
                "abandonment_prepared_successor_missing",
                "abandonment_attempts",
                row["abandonment_id"],
            ))
        if row["current_execution_id"] is not None:
            execution = conn.execute(
                "SELECT operation_id FROM operation_executions WHERE execution_id=?",
                (row["current_execution_id"],),
            ).fetchone()
            if execution is None or execution["operation_id"] != row["source_operation_id"]:
                problems.append(_semantic_problem(
                    conn,
                    "abandonment_execution_binding",
                    "abandonment_attempts",
                    row["abandonment_id"],
                    related_record_type="operation_executions",
                    related_record_id=row["current_execution_id"],
                ))


def _validate_safe_reclaim_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for row in conn.execute("SELECT * FROM safe_reclaims"):
        source = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (row["source_operation_id"],)
        ).fetchone()
        successor = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (row["successor_operation_id"],)
        ).fetchone()
        lease = conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=?", (row["source_lease_id"],)
        ).fetchone()
        request = conn.execute(
            "SELECT * FROM service_requests WHERE request_id=?", (row["request_id"],)
        ).fetchone()
        source_version = conn.execute(
            "SELECT * FROM content_versions WHERE content_version_id=?",
            (row["source_content_version_id"],),
        ).fetchone()
        successor_version = conn.execute(
            "SELECT * FROM content_versions WHERE content_version_id=?",
            (row["successor_content_version_id"],),
        ).fetchone()
        invalid = (
            source is None
            or successor is None
            or lease is None
            or request is None
            or source_version is None
            or successor_version is None
            or source["task_gid"] != row["task_gid"]
            or source["status"] != "cancelled"
            or source["terminal_outcome"] != "safe_reclaimed"
            or successor["task_gid"] != row["task_gid"]
            or lease["operation_id"] != row["source_operation_id"]
            or lease["owner_id"] != row["previous_owner_id"]
            or lease["run_id"] != row["previous_run_id"]
            or request["command"] != "safe-reclaim"
            or request["owner_id"] != row["requested_owner_id"]
            or request["run_id"] != row["requested_run_id"]
            or source_version["task_gid"] != row["task_gid"]
            or source_version["confirmed"] != 1
            or successor_version["operation_id"] != row["successor_operation_id"]
            or successor_version["boundary"] != "successor_baseline"
            or successor_version["confirmed"] != 1
            or successor_version["identity"] != source_version["identity"]
            or successor_version["title"] != source_version["title"]
            or successor_version["notes"] != source_version["notes"]
        )
        if invalid:
            problems.append(_semantic_problem(
                conn, "safe_reclaim_binding", "safe_reclaims", row["reclaim_id"]
            ))
            continue
        expected_mode = "verifier" if row["stage"] == "verification" else "stage_actor"
        if row["status"] == "prepared" and successor["successor_claim_mode"] != expected_mode:
            problems.append(_semantic_problem(
                conn, "safe_reclaim_prepared_successor_binding", "safe_reclaims", row["reclaim_id"]
            ))
        if row["status"] == "claimed" and successor["successor_claim_mode"] != "none":
            problems.append(_semantic_problem(
                conn, "safe_reclaim_claimed_successor_binding", "safe_reclaims", row["reclaim_id"]
            ))
        if row["stage"] == "verification":
            source_cycle = conn.execute(
                "SELECT * FROM verification_cycles WHERE cycle_id=?", (row["source_cycle_id"],)
            ).fetchone()
            successor_cycle = conn.execute(
                "SELECT * FROM verification_cycles WHERE cycle_id=?", (row["successor_cycle_id"],)
            ).fetchone()
            if (
                source_cycle is None
                or source_cycle["operation_id"] != row["source_operation_id"]
                or source_cycle["outcome"] != "safe_reclaimed"
                or source_cycle["completed_at"] is None
                or successor_cycle is None
                or successor_cycle["operation_id"] != row["successor_operation_id"]
            ):
                problems.append(_semantic_problem(
                    conn, "safe_reclaim_cycle_binding", "safe_reclaims", row["reclaim_id"]
                ))

    for row in conn.execute(
        "SELECT operation_id FROM operations WHERE status='cancelled' AND terminal_outcome='safe_reclaimed'"
    ):
        reclaim = conn.execute(
            "SELECT 1 FROM safe_reclaims WHERE source_operation_id=?", (row["operation_id"],)
        ).fetchone()
        pending = conn.execute(
            "SELECT 1 FROM operation_steps WHERE operation_id=? AND completed_at IS NULL LIMIT 1",
            (row["operation_id"],),
        ).fetchone()
        unresolved = conn.execute(
            """SELECT 1 FROM write_attempts WHERE operation_id=? AND outcome IN ('started','uncertain')
               UNION ALL
               SELECT 1 FROM movement_attempts WHERE operation_id=? AND outcome IN ('started','uncertain')
               LIMIT 1""",
            (row["operation_id"], row["operation_id"]),
        ).fetchone()
        if reclaim is None or pending is not None or unresolved is not None:
            problems.append(_semantic_problem(
                conn, "safe_reclaimed_source_terminal_binding", "operations", row["operation_id"]
            ))

    for row in conn.execute("SELECT * FROM verification_cycles WHERE outcome='safe_reclaimed'"):
        operation = conn.execute(
            "SELECT status,terminal_outcome FROM operations WHERE operation_id=?",
            (row["operation_id"],),
        ).fetchone()
        if (
            row["completed_at"] is None
            or row["signed_content_version_id"] is not None
            or row["signed_identity"] is not None
            or operation is None
            or operation["status"] != "cancelled"
            or operation["terminal_outcome"] != "safe_reclaimed"
        ):
            problems.append(_semantic_problem(
                conn, "safe_reclaimed_verification_cycle_binding", "verification_cycles", row["cycle_id"]
            ))

def _validate_succession_evidence(
    conn: sqlite3.Connection, problems: list[dict[str, Any]]
) -> None:
    for row in conn.execute("SELECT * FROM operation_successions"):
        source = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (row["source_operation_id"],)
        ).fetchone()
        successor = conn.execute(
            "SELECT * FROM operations WHERE operation_id=?", (row["successor_operation_id"],)
        ).fetchone()
        abandonment = conn.execute(
            "SELECT * FROM abandonment_attempts WHERE abandonment_id=?", (row["abandonment_id"],)
        ).fetchone()
        source_version = conn.execute(
            "SELECT * FROM content_versions WHERE content_version_id=?",
            (row["source_content_version_id"],),
        ).fetchone()
        successor_version = conn.execute(
            "SELECT * FROM content_versions WHERE content_version_id=?",
            (row["successor_content_version_id"],),
        ).fetchone()
        if (
            source is None
            or successor is None
            or abandonment is None
            or source_version is None
            or successor_version is None
            or source["task_gid"] != row["task_gid"]
            or source["status"] != "cancelled"
            or source["terminal_outcome"] != "agent_abandoned"
            or successor["task_gid"] != row["task_gid"]
            or successor_version["operation_id"] != successor["operation_id"]
            or successor_version["boundary"] != "successor_baseline"
            or successor_version["confirmed"] != 1
            or source_version["confirmed"] != 1
            or source_version["task_gid"] != row["task_gid"]
            or successor_version["task_gid"] != row["task_gid"]
            or successor_version["identity"] != source_version["identity"]
            or successor_version["title"] != source_version["title"]
            or successor_version["notes"] != source_version["notes"]
            or successor["expected_identity"] != successor_version["identity"]
            or abandonment["source_operation_id"] != source["operation_id"]
            or abandonment["successor_operation_id"] != successor["operation_id"]
        ):
            problems.append(_semantic_problem(
                conn,
                "operation_succession_binding",
                "operation_successions",
                row["succession_id"],
            ))

    for row in conn.execute(
        "SELECT operation_id FROM operations WHERE status='cancelled' AND terminal_outcome='agent_abandoned'"
    ):
        succession = conn.execute(
            "SELECT 1 FROM operation_successions WHERE source_operation_id=?",
            (row["operation_id"],),
        ).fetchone()
        pending = conn.execute(
            "SELECT 1 FROM operation_steps WHERE operation_id=? AND completed_at IS NULL LIMIT 1",
            (row["operation_id"],),
        ).fetchone()
        unresolved = conn.execute(
            """SELECT 1 FROM write_attempts
                 WHERE operation_id=? AND outcome IN ('started','uncertain')
               UNION ALL
               SELECT 1 FROM movement_attempts
                 WHERE operation_id=? AND outcome IN ('started','uncertain')
               LIMIT 1""",
            (row["operation_id"], row["operation_id"]),
        ).fetchone()
        if succession is None or pending is not None or unresolved is not None:
            problems.append(_semantic_problem(
                conn,
                "agent_abandoned_source_terminal_binding",
                "operations",
                row["operation_id"],
            ))

    for row in conn.execute(
        "SELECT * FROM operations WHERE successor_claim_mode IN ('stage_actor','verifier')"
    ):
        succession = conn.execute(
            "SELECT * FROM operation_successions WHERE successor_operation_id=?",
            (row["operation_id"],),
        ).fetchone()
        reclaim = conn.execute(
            "SELECT * FROM safe_reclaims WHERE successor_operation_id=? AND status='prepared'",
            (row["operation_id"],),
        ).fetchone()
        active_lease = conn.execute(
            "SELECT 1 FROM service_leases WHERE operation_id=? AND released_at IS NULL",
            (row["operation_id"],),
        ).fetchone()
        if (succession is None and reclaim is None) or row["status"] != "open" or active_lease is not None:
            related_type = "safe_reclaims" if reclaim is not None else "operation_successions"
            related_id = (
                reclaim["reclaim_id"] if reclaim is not None
                else (None if succession is None else succession["succession_id"])
            )
            problems.append(_semantic_problem(
                conn,
                "prepared_successor_authority_binding",
                "operations",
                row["operation_id"],
                related_record_type=related_type,
                related_record_id=related_id,
            ))

    for row in conn.execute("SELECT * FROM verification_cycles WHERE outcome='abandoned'"):
        operation = conn.execute(
            "SELECT status,terminal_outcome FROM operations WHERE operation_id=?",
            (row["operation_id"],),
        ).fetchone()
        if (
            row["completed_at"] is None
            or row["signed_content_version_id"] is not None
            or row["signed_identity"] is not None
            or operation is None
            or operation["status"] != "cancelled"
            or operation["terminal_outcome"] != "agent_abandoned"
        ):
            problems.append(_semantic_problem(
                conn,
                "abandoned_verification_cycle_binding",
                "verification_cycles",
                row["cycle_id"],
            ))


def _validate_semantic_evidence(conn: sqlite3.Connection) -> None:
    problems: list[dict[str, Any]] = []
    _validate_content_and_cycle_evidence(conn, problems)
    _validate_operation_and_inspection_evidence(conn, problems)
    _validate_execution_and_lease_evidence(conn, problems)
    _validate_planning_intent_evidence(conn, problems)
    _validate_backup_and_reset_evidence(conn, problems)
    _validate_operation_run_revocation_evidence(conn, problems)
    _validate_abandonment_attempt_evidence(conn, problems)
    _validate_succession_evidence(conn, problems)
    _validate_safe_reclaim_evidence(conn, problems)
    if problems:
        raise DishRuleError(
            "VALIDATION_FAILED", "database durable evidence is semantically inconsistent",
            rule="database_semantic_evidence_invalid",
            details={
                "problems": problems[:50],
                "problem_count": len(problems),
                "diagnostic_timestamp": utc_now(),
                "transaction_state": {
                    "connection_in_transaction": bool(conn.in_transaction),
                    "evidence_visibility": (
                        "connection_local_uncommitted"
                        if conn.in_transaction
                        else "committed_database"
                    ),
                },
            },
        )
