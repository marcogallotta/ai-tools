"""SQLite schema, audit, and state transitions."""

from __future__ import annotations

import hashlib
import json
import os
import time
import sqlite3
import tempfile
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
BEFORE UPDATE OF status, operation_id, task_gid, result_json, completed_at ON service_requests
WHEN OLD.status <> 'pending'
  OR NEW.status NOT IN ('completed','uncertain')
  OR NEW.result_json IS NULL
  OR NEW.completed_at IS NULL
BEGIN SELECT RAISE(ABORT, 'service request completion is one-way'); END;

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

MIGRATIONS = {1: _MIGRATION_1, 2: _MIGRATION_2, 3: _MIGRATION_3, 4: _MIGRATION_4, 5: _MIGRATION_5, 6: _MIGRATION_6, 7: _MIGRATION_7, 8: _MIGRATION_8, 9: _MIGRATION_9, 10: _MIGRATION_10, 11: _MIGRATION_11, 12: _MIGRATION_12, 13: _MIGRATION_13, 14: _MIGRATION_14, 15: _MIGRATION_15, 16: _MIGRATION_16, 17: _MIGRATION_17, 18: _MIGRATION_18, 19: _MIGRATION_19, 20: _MIGRATION_20, 21: _MIGRATION_21, 22: _MIGRATION_22, 23: _MIGRATION_23, 24: _MIGRATION_24, 25: _MIGRATION_25, 26: _MIGRATION_26, 27: _MIGRATION_27, 28: _MIGRATION_28, 29: _MIGRATION_29}


def _backup_legacy_database(db_path: Path) -> None:
    """Keep one transactionally complete legacy snapshot before migration.

    Copying only the main SQLite file can omit committed pages still resident in
    a WAL file. Build the legacy backup through SQLite's online backup API and
    replace any earlier incomplete artifact while the live database is still on
    a pre-redesign schema.
    """

    if not db_path.exists() or str(db_path) == ":memory:":
        return
    backup = db_path.with_suffix(db_path.suffix + ".legacy-v2.bak")
    source = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    source.row_factory = sqlite3.Row
    temp_path: Path | None = None
    try:
        version = int(source.execute("PRAGMA user_version").fetchone()[0])
        if version >= 3:
            return
        with tempfile.NamedTemporaryFile(
            dir=backup.parent,
            prefix=f".{backup.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
        target = sqlite3.connect(str(temp_path), timeout=30, isolation_level=None)
        try:
            source.backup(target)
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            target.close()
        check = sqlite3.connect(str(temp_path), timeout=30, isolation_level=None)
        try:
            if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("legacy backup integrity check failed")
            if int(check.execute("PRAGMA user_version").fetchone()[0]) != version:
                raise sqlite3.DatabaseError("legacy backup schema version mismatch")
        finally:
            check.close()
        os.replace(temp_path, backup)
        temp_path = None
    finally:
        source.close()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


WAL_BUSY_TIMEOUT_MS = 100
WAL_RETRY_ATTEMPTS = 20
WAL_RETRY_SLEEP_BASE_SECONDS = 0.01
WAL_RETRY_SLEEP_CAP_SECONDS = 0.1
MIGRATION_BUSY_TIMEOUT_MS = 2000
RUNTIME_BUSY_TIMEOUT_MS = 30000


def initialize_database(
    path: str | os.PathLike[str] = DEFAULT_DB_PATH,
) -> sqlite3.Connection:
    db_path = Path(path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _backup_legacy_database(db_path)
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {WAL_BUSY_TIMEOUT_MS}")
    journal_exc: sqlite3.OperationalError | None = None
    # A second initializer can briefly collide with the first while SQLite is
    # establishing WAL mode. Retry only this narrow busy/locked boundary; after
    # the bounded window, a persistent reader is reported as a structured lock.
    for attempt in range(WAL_RETRY_ATTEMPTS):
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            journal_exc = None
            break
        except sqlite3.OperationalError as exc:
            text = str(exc).lower()
            if "locked" not in text and "busy" not in text:
                conn.close()
                raise
            journal_exc = exc
            time.sleep(min(WAL_RETRY_SLEEP_BASE_SECONDS * (attempt + 1), WAL_RETRY_SLEEP_CAP_SECONDS))
    if journal_exc is not None:
        conn.close()
        raise DishRuleError(
            "BACKEND_REJECTED",
            "database journal mode could not be established while another reader holds the file",
            rule="database_reader_lock",
            retryable=True,
        ) from journal_exc
    conn.execute(f"PRAGMA busy_timeout = {MIGRATION_BUSY_TIMEOUT_MS}")
    try:
        migrate_database(conn)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower() or "busy" in str(exc).lower():
            conn.close()
            raise DishRuleError(
                "BACKEND_REJECTED",
                "database initialization is blocked by another writer",
                rule="database_writer_lock",
                retryable=True,
                details={"timeout_ms": MIGRATION_BUSY_TIMEOUT_MS},
            ) from exc
        conn.close()
        raise
    conn.execute(f"PRAGMA busy_timeout = {RUNTIME_BUSY_TIMEOUT_MS}")
    try:
        _validate_current_database(conn)
    except Exception:
        conn.close()
        raise
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


def _schema_version_state(conn: sqlite3.Connection) -> tuple[int, int | None]:
    try:
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        has_ledger = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone() is not None
        ledger_version = None
        if has_ledger:
            ledger_version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
            ledger_version = None if ledger_version is None else int(ledger_version)
        return user_version, ledger_version
    except (sqlite3.DatabaseError, TypeError, ValueError, IndexError) as exc:
        raise DishRuleError(
            "VALIDATION_FAILED", "database migration ledger is malformed",
            rule="database_ledger_malformed",
            details={"error": f"{type(exc).__name__}: {exc}"},
        ) from exc


def _validate_version_claims(conn: sqlite3.Connection, *, allow_empty: bool = False) -> None:
    current = max(MIGRATIONS)
    user_version, ledger_version = _schema_version_state(conn)
    if user_version > current:
        raise DishRuleError("VALIDATION_FAILED", "database user_version is newer than this release", rule="database_future_user_version", details={"user_version": user_version, "current": current})
    if ledger_version is not None and ledger_version > current:
        raise DishRuleError("VALIDATION_FAILED", "database migration ledger is newer than this release", rule="database_future_ledger", details={"ledger_version": ledger_version, "current": current})
    if ledger_version is not None and ledger_version != user_version:
        raise DishRuleError("VALIDATION_FAILED", "database migration ledger and user_version disagree", rule="database_version_disagreement", details={"user_version": user_version, "ledger_version": ledger_version})
    if ledger_version is not None:
        ledger_rows = [int(row[0]) for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")]
        expected_rows = list(range(1, ledger_version + 1))
        if ledger_rows != expected_rows:
            raise DishRuleError(
                "VALIDATION_FAILED",
                "database migration ledger is not contiguous",
                rule="database_ledger_gap",
                details={"versions": ledger_rows, "expected": expected_rows},
            )
    if not allow_empty and user_version > 0 and ledger_version is None:
        raise DishRuleError("VALIDATION_FAILED", "versioned database is missing its migration ledger", rule="database_ledger_missing", details={"user_version": user_version})


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
    "backup_creations": "request_id",
    "service_requests": "request_id",
    "operations": "operation_id",
    "service_leases": "lease_id",
    "operation_execution_claims": "claim_id",
    "operation_executions": "execution_id",
    "two_pass_resets": "reset_id",
}
_SEMANTIC_PROVENANCE_FIELDS = (
    "task_gid", "operation_id", "request_id", "execution_id", "command",
    "run_id", "actor_run_id", "owner_id", "cycle_id", "source_cycle_id",
)
_SEMANTIC_TIMESTAMP_FIELDS = (
    "created_at", "confirmed_at", "started_at", "finished_at", "completed_at",
    "acquired_at", "renewed_at", "expires_at", "released_at", "reserved_at",
    "consumed_at", "resolved_at", "process_start", "expected_modified_at",
    "confirmed_modified_at", "content_write_completed_at",
    "signoff_completed_at", "movement_completed_at",
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
                "fields": ["command", "status", "result_json", "completed_at"],
            }],
            "required_predicate": (
                "completed backup creation metadata exactly matches a completed successful "
                "backup-create service result for the same request"
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
                "every completed successful backup-create request has one completed "
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
        "two_pass_reset_binding": {
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
                "operation with outcome=two-pass-hold"
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


def _validate_semantic_evidence(conn: sqlite3.Connection) -> None:
    problems: list[dict[str, Any]] = []
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
              AND (cycle.route IN ('evidence','human_review') OR cycle.outcome='two-pass-hold')"""
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
    for row in conn.execute("SELECT * FROM backup_creations WHERE status='completed'"):
        request = conn.execute(
            "SELECT * FROM service_requests WHERE request_id=?", (row["request_id"],)
        ).fetchone()
        valid = False
        if request is not None and request["command"] == "backup-create" and request["status"] == "completed":
            try:
                result = json.loads(request["result_json"] or "null")
            except (TypeError, ValueError):
                result = None
            backup = (result.get("data") or {}).get("backup") if isinstance(result, dict) else None
            valid = bool(
                isinstance(result, dict)
                and result.get("ok")
                and isinstance(backup, dict)
                and backup.get("backup_id") == row["backup_id"]
                and backup.get("sha256") == row["sha256"]
                and backup.get("size_bytes") == row["size_bytes"]
            )
        if not valid:
            problems.append(_semantic_problem(
                conn,
                "backup_creation_request_binding",
                "backup_creations",
                row["request_id"],
            ))
    for request in conn.execute(
        """SELECT * FROM service_requests
             WHERE command='backup-create' AND status='completed'
               AND json_extract(result_json, '$.ok')=1"""
    ):
        creation = conn.execute(
            "SELECT status FROM backup_creations WHERE request_id=?",
            (request["request_id"],),
        ).fetchone()
        if creation is None or creation["status"] != "completed":
            problems.append(_semantic_problem(
                conn,
                "backup_creation_result_missing",
                "service_requests",
                request["request_id"],
            ))
    for row in conn.execute("SELECT * FROM two_pass_resets"):
        version = conn.execute(
            """SELECT 1 FROM content_versions
                 WHERE operation_id=? AND identity=? AND confirmed=1 LIMIT 1""",
            (row["operation_id"], row["candidate_identity"]),
        ).fetchone()
        cycle = conn.execute(
            "SELECT 1 FROM verification_cycles WHERE cycle_id=? AND operation_id=? AND outcome='two-pass-hold'",
            (row["source_cycle_id"], row["operation_id"]),
        ).fetchone()
        if version is None or cycle is None:
            problems.append(_semantic_problem(conn,
                "two_pass_reset_binding", "two_pass_resets", row["reset_id"],
            ))
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


def _validate_current_database(conn: sqlite3.Connection) -> None:
    current = max(MIGRATIONS)
    _validate_version_claims(conn)
    user_version, ledger_version = _schema_version_state(conn)
    if user_version != current or ledger_version != current:
        raise DishRuleError("VALIDATION_FAILED", "database did not converge to the current schema", rule="database_schema_not_current", details={"user_version": user_version, "ledger_version": ledger_version, "current": current})
    required = {"operations", "operation_steps", "operation_actor_facts", "verification_cycles", "write_attempts", "movement_attempts", "task_content_state", "content_versions", "audit_events", "marco_authorizations", "service_leases", "service_requests", "operation_execution_claims", "operation_executions", "dish_inspect_facts", "planning_reopen_attempts", "backup_creations"}
    actual = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    missing = sorted(required - actual)
    if missing:
        raise DishRuleError("VALIDATION_FAILED", "current-version database is missing required tables", rule="database_schema_incomplete", details={"missing_tables": missing})
    expected = _canonical_schema_manifest()
    actual_manifest = _schema_manifest(conn)
    if actual_manifest != expected:
        missing_objects = sorted(set(expected) - set(actual_manifest))
        extra_objects = sorted(set(actual_manifest) - set(expected))
        altered_objects = sorted(name for name in set(expected) & set(actual_manifest) if expected[name] != actual_manifest[name])
        raise DishRuleError(
            "VALIDATION_FAILED",
            "current-version database schema does not match the canonical release schema",
            rule="database_schema_signature_mismatch",
            details={"missing_objects": missing_objects, "extra_objects": extra_objects, "altered_objects": altered_objects},
        )
    _validate_semantic_evidence(conn)


def _normalized_schema_sql(sql: str | None) -> str:
    import re
    text = " ".join((sql or "").split())
    text = re.sub(r"\s*([(),])\s*", r"\1", text)
    return text


def _schema_manifest(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute(
        """SELECT type, name, sql FROM sqlite_master
           WHERE type IN ('table','index','trigger')
             AND name NOT LIKE 'sqlite_%'
           ORDER BY type, name"""
    )
    return {f"{row[0]}:{row[1]}": _normalized_schema_sql(row[2]) for row in rows}


_CANONICAL_SCHEMA_MANIFEST: dict[str, str] | None = None


def _canonical_schema_manifest() -> dict[str, str]:
    global _CANONICAL_SCHEMA_MANIFEST
    if _CANONICAL_SCHEMA_MANIFEST is None:
        probe = sqlite3.connect(":memory:", isolation_level=None)
        try:
            probe.execute("PRAGMA foreign_keys = ON")
            probe.execute("BEGIN IMMEDIATE")
            probe.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)")
            for version in sorted(MIGRATIONS):
                _execute_script_statements(probe, MIGRATIONS[version])
                probe.execute("INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'canonical')", (version,))
                probe.execute(f"PRAGMA user_version = {version}")
            probe.execute("COMMIT")
            _CANONICAL_SCHEMA_MANIFEST = _schema_manifest(probe)
        finally:
            probe.close()
    return dict(_CANONICAL_SCHEMA_MANIFEST)


def migrate_database(conn: sqlite3.Connection) -> None:
    # Hold one SQLite write lock across discovery and every migration. This makes
    # concurrent initializers serialize instead of racing on CREATE/ALTER steps.
    conn.execute("BEGIN IMMEDIATE")
    try:
        # Validate existing claims before creating or applying anything. A truly
        # empty database is the only permitted ledger-less state.
        existing_tables = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()[0]
        _validate_version_claims(conn, allow_empty=not bool(existing_tables))
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
