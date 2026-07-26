"""SQLite schema, audit, and state transitions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
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

MIGRATIONS = {1: _MIGRATION_1, 2: _MIGRATION_2, 3: _MIGRATION_3, 4: _MIGRATION_4, 5: _MIGRATION_5, 6: _MIGRATION_6, 7: _MIGRATION_7, 8: _MIGRATION_8, 9: _MIGRATION_9, 10: _MIGRATION_10, 11: _MIGRATION_11, 12: _MIGRATION_12, 13: _MIGRATION_13, 14: _MIGRATION_14, 15: _MIGRATION_15, 16: _MIGRATION_16, 17: _MIGRATION_17}


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
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 100")
    journal_exc: sqlite3.OperationalError | None = None
    # A second initializer can briefly collide with the first while SQLite is
    # establishing WAL mode. Retry only this narrow busy/locked boundary; after
    # the bounded window, a persistent reader is reported as a structured lock.
    for attempt in range(20):
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
            time.sleep(min(0.01 * (attempt + 1), 0.1))
    if journal_exc is not None:
        conn.close()
        raise DishRuleError(
            "BACKEND_REJECTED",
            "database journal mode could not be established while another reader holds the file",
            rule="database_reader_lock",
            retryable=True,
        ) from journal_exc
    conn.execute("PRAGMA busy_timeout = 2000")
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
                details={"timeout_ms": 2000},
            ) from exc
        conn.close()
        raise
    conn.execute("PRAGMA busy_timeout = 30000")
    _validate_current_database(conn)
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


def _validate_semantic_evidence(conn: sqlite3.Connection) -> None:
    problems: list[dict[str, Any]] = []
    for row in conn.execute("SELECT * FROM content_versions WHERE confirmed=1"):
        if _content_digest(row["title"], row["notes"]) != row["identity"]:
            problems.append({"kind": "content_identity_mismatch", "id": row["content_version_id"]})
    for row in conn.execute("SELECT * FROM write_attempts WHERE outcome='confirmed'"):
        bound = conn.execute(
            "SELECT identity,confirmed,operation_id FROM content_versions WHERE content_version_id=?",
            (row["confirmed_content_version_id"],),
        ).fetchone()
        if bound is None or bound["confirmed"] != 1 or bound["operation_id"] != row["operation_id"] or bound["identity"] != row["intended_identity"]:
            problems.append({"kind": "confirmed_write_binding", "id": row["attempt_id"]})
    for row in conn.execute("SELECT * FROM verification_cycles"):
        release = str(row["protocol_release"] or "")
        text = str(row["protocol_text"] or "")
        if release.startswith("sha256:") and hashlib.sha256(text.encode("utf-8")).hexdigest() != release.split(":", 1)[1].split(";", 1)[0].strip():
            problems.append({"kind": "verification_protocol_identity", "id": row["cycle_id"]})
    for task in conn.execute("SELECT DISTINCT task_gid FROM verification_cycles"):
        numbers = [r[0] for r in conn.execute("SELECT cycle_number FROM verification_cycles WHERE task_gid=? ORDER BY cycle_number", (task[0],))]
        if numbers and numbers != list(range(1, max(numbers) + 1)):
            problems.append({"kind": "verification_cycle_sequence", "id": task[0]})
    for row in conn.execute("SELECT * FROM marco_authorizations WHERE consumed_at IS NOT NULL"):
        if not row["consumed_identity"] or not row["reserved_by_operation_id"] or not row["reserved_at"]:
            problems.append({"kind": "consumed_authorization_binding", "id": row["authorization_id"]})
    for row in conn.execute("SELECT * FROM verification_cycles WHERE outcome='approved'"):
        signed = conn.execute(
            "SELECT identity,confirmed,operation_id,task_gid FROM content_versions WHERE content_version_id=?",
            (row["signed_content_version_id"],),
        ).fetchone()
        if (row["completed_at"] is None or row["signed_identity"] is None or signed is None
                or signed["confirmed"] != 1 or signed["operation_id"] != row["operation_id"]
                or signed["task_gid"] != row["task_gid"] or signed["identity"] != row["signed_identity"]):
            problems.append({"kind": "approved_cycle_binding", "id": row["cycle_id"]})
    for row in conn.execute("SELECT * FROM operations"):
        if row["status"] == "completed" and (row["completed_at"] is None or row["phase"] != "terminal" or not row["terminal_outcome"] or not row["schema_version"] or not row["expected_identity"]):
            problems.append({"kind": "completed_operation_state", "id": row["operation_id"]})
        if row["signoff_completed_at"] is not None:
            approved = conn.execute(
                "SELECT 1 FROM verification_cycles WHERE operation_id=? AND outcome='approved' AND signed_identity IS NOT NULL AND signed_content_version_id IS NOT NULL",
                (row["operation_id"],),
            ).fetchone()
            if approved is None:
                problems.append({"kind": "operation_signoff_binding", "id": row["operation_id"]})
    if problems:
        raise DishRuleError(
            "VALIDATION_FAILED", "database durable evidence is semantically inconsistent",
            rule="database_semantic_evidence_invalid",
            details={"problems": problems[:50], "problem_count": len(problems)},
        )


def _validate_current_database(conn: sqlite3.Connection) -> None:
    current = max(MIGRATIONS)
    _validate_version_claims(conn)
    user_version, ledger_version = _schema_version_state(conn)
    if user_version != current or ledger_version != current:
        raise DishRuleError("VALIDATION_FAILED", "database did not converge to the current schema", rule="database_schema_not_current", details={"user_version": user_version, "ledger_version": ledger_version, "current": current})
    required = {"operations", "operation_steps", "operation_actor_facts", "verification_cycles", "write_attempts", "movement_attempts", "task_content_state", "content_versions", "audit_events", "marco_authorizations"}
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
