import json
import os
import socket
import sqlite3

import pytest

from dish_tool.constants import (
    MAX_REQUEST_LIFETIME_SECONDS,
    NONTERMINAL_STATES,
    RECOVERY_QUARANTINE_SECONDS,
    RECOVERY_SAFETY_MARGIN_SECONDS,
    SCHEMA_VERSION,
    TERMINAL_STATES,
)
from dish_tool.database import (
    record_audit,
)
from dish_tool.database_initialization import initialize_database
from dish_tool.database_migrations import migrate_database
from dish_tool.database_migrations import _execute_script_statements
from dish_tool.database_schema import MIGRATIONS
from dish_tool.errors import DishRuleError
from dish_tool.recovery import (
    current_process_identity,
    process_identity_is_live,
)


def insert_submission(conn, submission_id, task_gid, status):
    conn.execute(
        """
        INSERT INTO submissions (
            submission_id, task_gid, submission_kind, protocol_release,
            release_commit, protocol_bundle, canonical_manifest,
            editor_agent, editor_family, status, created_at
        ) VALUES (?, ?, 'planning', 'fixture-v1', 'abc', '{}', '{}',
                  'claude', 'claude', ?, '2026-07-21T00:00:00Z')
        """,
        (submission_id, task_gid, status),
    )
    conn.commit()


@pytest.mark.database_boundary
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_bootstrap
@pytest.mark.invariant_database_bootstrap
@pytest.mark.smoke
@pytest.mark.real_database_bootstrap
def test_schema_creation_and_migration_are_idempotent(tmp_path):
    db_path = tmp_path / "dish-tool.db"
    conn = initialize_database(db_path)
    migrate_database(conn)
    migrate_database(conn)

    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"schema_migrations", "submissions", "audit_events"} <= tables

    submission_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(submissions)")
    }
    assert {
        "failed_verification_passes",
        "destination_section_name",
        "destination_section_gid",
        "write_attempt_id",
        "in_flight_at",
        "in_flight_hostname",
        "in_flight_pid",
        "in_flight_process_start",
        "research_queue_moved_at",
        "notes_written_at",
        "task_content_written_at",
        "destination_moved_at",
        "baseline_title",
        "baseline_title_fields",
        "prepared_title",
        "prepared_title_fields",
    } <= submission_columns
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


@pytest.mark.database_boundary
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_upgrade
@pytest.mark.smoke
def test_schema_34_and_35_upgrade_existing_database_with_current_journals(tmp_path):
    db_path = tmp_path / "upgrade.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 34):
            _execute_script_statements(conn, MIGRATIONS[version])
            conn.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                (version, f"v{version}"),
            )
            conn.execute(f"PRAGMA user_version={version}")
        conn.execute("COMMIT")
    finally:
        conn.close()

    upgraded = initialize_database(db_path)
    try:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        planning_table = upgraded.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' AND name='planning_intent_challenges'"
        ).fetchone()
        assert planning_table is not None
        audit_columns = {
            row[1] for row in upgraded.execute("PRAGMA table_info(audit_events)")
        }
        assert "operation_execution_id" in audit_columns
    finally:
        upgraded.close()


@pytest.mark.database_boundary
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_upgrade
@pytest.mark.smoke
def test_schema_35_upgrades_schema_34_audit_journal(tmp_path):
    db_path = tmp_path / "schema-34.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 35):
            _execute_script_statements(conn, MIGRATIONS[version])
            conn.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                (version, f"v{version}"),
            )
            conn.execute(f"PRAGMA user_version={version}")
        conn.execute("COMMIT")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 34
        assert "operation_execution_id" not in {
            row[1] for row in conn.execute("PRAGMA table_info(audit_events)")
        }
    finally:
        conn.close()

    upgraded = initialize_database(db_path)
    try:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "operation_execution_id" in {
            row[1] for row in upgraded.execute("PRAGMA table_info(audit_events)")
        }
        assert upgraded.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='index' AND name='audit_events_operation_execution_idx'"
        ).fetchone() is not None
        assert upgraded.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type='trigger' AND name='audit_events_execution_binding_insert'"
        ).fetchone() is not None
    finally:
        upgraded.close()


@pytest.mark.database_boundary
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_upgrade
def test_schema_41_preserves_semantic_proposals_and_allows_mechanical_claim_actor(tmp_path):
    db_path = tmp_path / "schema-40-semantic-proposal.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 41):
            _execute_script_statements(conn, MIGRATIONS[version])
            conn.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                (version, f"v{version}"),
            )
            conn.execute(f"PRAGMA user_version={version}")
        conn.execute(
            """INSERT INTO operations (
                   operation_id,task_gid,operation_kind,status,expected_identity,
                   schema_version,created_at
               ) VALUES ('op-v40','task-v40','initial','open',?, '1.0.10','2026-08-09T00:00:00Z')""",
            ("a" * 64,),
        )
        conn.execute(
            """INSERT INTO verification_cycles (
                   cycle_id,operation_id,task_gid,cycle_number,protocol_release,created_at
               ) VALUES ('cycle-v40','op-v40','task-v40',1,'release-v40','2026-08-09T00:00:01Z')"""
        )
        conn.execute(
            """INSERT INTO semantic_proposals (
                   proposal_id,task_gid,operation_id,cycle_id,baseline_identity,candidate_identity,
                   candidate_title,candidate_notes,proposal_reason,explanation_json,linked_changes_json,
                   protocol_release,protocol_text,correction_class,proposer_agent,proposer_run_id,
                   status,created_at
               ) VALUES (
                   'proposal-v40','task-v40','op-v40','cycle-v40',?,?,
                   'Candidate','notes','reason','{}','[]','release-v40','protocol','large','gpt','run-v40',
                   'pending','2026-08-09T00:00:02Z'
               )""",
            ("a" * 64, "b" * 64),
        )
        conn.execute(
            """INSERT INTO semantic_proposal_changes (
                   proposal_id,ordinal,field_name,before_json,after_json
               ) VALUES ('proposal-v40',0,'Locks','"old"','"new"')"""
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    upgraded = sqlite3.connect(db_path, isolation_level=None)
    upgraded.row_factory = sqlite3.Row
    upgraded.execute("PRAGMA foreign_keys=ON")
    migrate_database(upgraded)
    try:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        proposal = upgraded.execute(
            "SELECT * FROM semantic_proposals WHERE proposal_id='proposal-v40'"
        ).fetchone()
        assert proposal["candidate_identity"] == "b" * 64
        assert proposal["agent_attested_decisions_json"] == "[]"
        change = upgraded.execute(
            "SELECT field_name,before_json,after_json FROM semantic_proposal_changes "
            "WHERE proposal_id='proposal-v40'"
        ).fetchone()
        assert tuple(change) == ("Locks", '"old"', '"new"')
        upgraded.execute(
            """UPDATE semantic_proposals
                  SET status='approved',reviewed_at='2026-08-09T00:00:03Z',
                      review_reason='approved',approved_by='Marco'
                WHERE proposal_id='proposal-v40'"""
        )
        upgraded.execute(
            """UPDATE semantic_proposals
                  SET status='claimed',claimed_at='2026-08-09T00:00:04Z',
                      claimed_agent='dish',claimed_run_id='mechanical-run'
                WHERE proposal_id='proposal-v40'"""
        )
        assert upgraded.execute(
            "SELECT claimed_agent FROM semantic_proposals WHERE proposal_id='proposal-v40'"
        ).fetchone()[0] == "dish"
    finally:
        upgraded.close()


@pytest.mark.smoke
def test_partial_unique_index_holds_for_every_nonterminal_state(tmp_path):
    conn = initialize_database(tmp_path / "dish-tool.db")
    for index, status in enumerate(sorted(NONTERMINAL_STATES)):
        task_gid = f"task-{index}"
        insert_submission(conn, f"first-{index}", task_gid, status)
        with pytest.raises(sqlite3.IntegrityError):
            insert_submission(conn, f"second-{index}", task_gid, "drafting")


@pytest.mark.smoke
def test_partial_unique_index_releases_for_terminal_states(tmp_path):
    conn = initialize_database(tmp_path / "dish-tool.db")
    for index, status in enumerate(sorted(TERMINAL_STATES)):
        task_gid = f"terminal-{index}"
        insert_submission(conn, f"old-{index}", task_gid, status)
        insert_submission(conn, f"new-{index}", task_gid, "drafting")
        rows = conn.execute(
            "SELECT submission_id, status FROM submissions "
            "WHERE task_gid=? ORDER BY submission_id",
            (task_gid,),
        ).fetchall()
        assert [(row[0], row[1]) for row in rows] == [
            (f"new-{index}", "drafting"),
            (f"old-{index}", status),
        ]


@pytest.mark.smoke
def test_audit_rows_allow_null_submission_and_keep_task_gid(tmp_path):
    conn = initialize_database(tmp_path / "dish-tool.db")
    event_id = record_audit(
        conn,
        submission_id=None,
        task_gid="task-123",
        event_type="generic_note_bypass",
        actor_agent="codex",
        details={"command": "set-notes", "code": "OK"},
    )
    row = conn.execute(
        "SELECT submission_id, task_gid, details FROM audit_events WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    assert row[0] is None
    assert row[1] == "task-123"
    assert json.loads(row[2]) == {"code": "OK", "command": "set-notes"}



@pytest.mark.smoke
def test_process_identity_and_recovery_quarantine_invariant():
    identity = current_process_identity()
    assert identity.hostname == socket.gethostname()
    assert identity.pid == os.getpid()
    assert identity.process_start
    assert process_identity_is_live(identity) is True
    assert (
        RECOVERY_QUARANTINE_SECONDS
        > MAX_REQUEST_LIFETIME_SECONDS + RECOVERY_SAFETY_MARGIN_SECONDS
    )



@pytest.mark.smoke
def test_legacy_submission_write_attempt_evidence_remains_readable(tmp_path):
    conn = initialize_database(tmp_path / "dish-tool.db")
    insert_submission(conn, "s1", "t1", "in_flight")
    conn.execute(
        """UPDATE submissions
              SET write_attempt_id='legacy-attempt',
                  in_flight_at='2026-07-01T00:00:00+00:00',
                  in_flight_hostname='legacy-host',
                  in_flight_pid=123,
                  in_flight_process_start='legacy-start'
            WHERE submission_id='s1'"""
    )

    row = conn.execute(
        """SELECT status, write_attempt_id, in_flight_at, in_flight_hostname,
                  in_flight_pid, in_flight_process_start
             FROM submissions WHERE submission_id='s1'"""
    ).fetchone()
    assert tuple(row) == (
        "in_flight",
        "legacy-attempt",
        "2026-07-01T00:00:00+00:00",
        "legacy-host",
        123,
        "legacy-start",
    )


@pytest.mark.database_boundary
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_upgrade
def test_schema_43_converts_run_retirements_to_explicit_revocations(tmp_path):
    db_path = tmp_path / "schema-42-run-retirement.db"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 43):
            _execute_script_statements(conn, MIGRATIONS[version])
            conn.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                (version, f"v{version}"),
            )
            conn.execute(f"PRAGMA user_version={version}")
        conn.execute(
            """INSERT INTO operations (
                   operation_id,task_gid,operation_kind,status,expected_identity,
                   schema_version,created_at
               ) VALUES ('op-v42','task-v42','initial','open',?, '2','2026-08-10T00:00:00Z')""",
            ("a" * 64,),
        )
        conn.execute(
            """INSERT INTO operation_run_retirements(
                   retirement_id,operation_id,owner_id,run_id,source_lease_id,reason,retired_at
               ) VALUES('retirement-v42','op-v42','owner-v42','run-v42',NULL,
                        'Marco kill/replace: historical explicit kill','2026-08-10T00:00:01Z')"""
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    upgraded = sqlite3.connect(db_path, isolation_level=None)
    upgraded.row_factory = sqlite3.Row
    upgraded.execute("PRAGMA foreign_keys=ON")
    migrate_database(upgraded)
    try:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 43
        assert upgraded.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='operation_run_retirements'"
        ).fetchone() is None
        row = upgraded.execute(
            "SELECT * FROM operation_run_revocations WHERE revocation_id='retirement-v42'"
        ).fetchone()
        assert row is not None
        assert (row["operation_id"], row["owner_id"], row["run_id"]) == (
            "op-v42",
            "owner-v42",
            "run-v42",
        )
        assert row["source_lease_id"] is None
        assert row["revoked_at"] == "2026-08-10T00:00:01Z"
        assert "claimed_owner_id" in {
            column[1] for column in upgraded.execute("PRAGMA table_info(semantic_proposals)")
        }
    finally:
        upgraded.close()


def _schema42_claimed_proposal_db(
    db_path,
    *,
    include_exact_lease: bool,
) -> None:
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 43):
            _execute_script_statements(conn, MIGRATIONS[version])
            conn.execute(
                "INSERT INTO schema_migrations(version,applied_at) VALUES(?,?)",
                (version, f"v{version}"),
            )
            conn.execute(f"PRAGMA user_version={version}")
        conn.execute(
            """INSERT INTO operations(
                   operation_id,task_gid,operation_kind,status,expected_identity,
                   schema_version,phase,created_at
               ) VALUES('op-claimed-v42','task-claimed-v42','initial','open',?,
                        '2','await_verification','2026-08-10T00:00:00Z')""",
            ("a" * 64,),
        )
        conn.execute(
            """INSERT INTO verification_cycles(
                   cycle_id,operation_id,task_gid,cycle_number,protocol_release,
                   created_at,protocol_text
               ) VALUES('cycle-claimed-v42','op-claimed-v42','task-claimed-v42',1,
                        '1.0.11','2026-08-10T00:00:01Z','verification protocol')"""
        )
        if include_exact_lease:
            conn.execute(
                """INSERT INTO service_leases(
                       lease_id,operation_id,task_gid,owner_id,run_id,acquired_at,
                       renewed_at,expires_at,released_at,release_reason,lease_kind,
                       actor_attempt_seq,context_cycle_id
                   ) VALUES('lease-claimed-v42','op-claimed-v42','task-claimed-v42',
                            'action:gpt','claimed-run-v42','2026-08-10T00:00:02Z',
                            '2026-08-10T00:00:02Z','2026-08-10T00:30:02Z',
                            '2026-08-10T00:05:00Z','normal release','actor',1,
                            'cycle-claimed-v42')"""
            )
        conn.execute(
            """INSERT INTO semantic_proposals(
                   proposal_id,task_gid,operation_id,cycle_id,baseline_identity,
                   candidate_identity,candidate_title,candidate_notes,proposal_reason,
                   explanation_json,linked_changes_json,agent_attested_decisions_json,
                   protocol_release,protocol_text,correction_class,proposer_agent,
                   proposer_run_id,status,created_at,reviewed_at,review_reason,approved_by,
                   claimed_at,claimed_agent,claimed_run_id,claim_request_id
               ) VALUES(
                   'proposal-claimed-v42','task-claimed-v42','op-claimed-v42',
                   'cycle-claimed-v42',?,?, 'Candidate','notes','approved correction',
                   '{}','[]','[]','1.0.11','verification protocol','large','codex',
                   'proposal-author-v42','claimed','2026-08-10T00:00:03Z',
                   '2026-08-10T00:00:04Z','Marco approved','Marco',
                   '2026-08-10T00:00:05Z','codex','claimed-run-v42',NULL
               )""",
            ("a" * 64, "b" * 64),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


@pytest.mark.database_boundary
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_upgrade
def test_schema_43_backfills_claimed_proposal_owner_from_exact_schema42_lease(tmp_path):
    db_path = tmp_path / "schema-42-claimed-proposal.db"
    _schema42_claimed_proposal_db(db_path, include_exact_lease=True)

    upgraded = sqlite3.connect(db_path, isolation_level=None)
    upgraded.row_factory = sqlite3.Row
    upgraded.execute("PRAGMA foreign_keys=ON")
    migrate_database(upgraded)
    try:
        row = upgraded.execute(
            "SELECT status,claimed_owner_id,claimed_run_id FROM semantic_proposals "
            "WHERE proposal_id='proposal-claimed-v42'"
        ).fetchone()
        assert row["status"] == "claimed"
        assert row["claimed_owner_id"] == "action:gpt"
        assert row["claimed_run_id"] == "claimed-run-v42"
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 43
    finally:
        upgraded.close()


@pytest.mark.database_boundary
@pytest.mark.production_sqlite_pragmas
@pytest.mark.database_boundary_upgrade
def test_schema_43_refuses_claimed_proposal_without_provable_owner(tmp_path):
    db_path = tmp_path / "schema-42-ownerless-claimed-proposal.db"
    _schema42_claimed_proposal_db(db_path, include_exact_lease=False)

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError, match="cannot prove owner"):
        migrate_database(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 42
    assert "claimed_owner_id" not in {
        column[1] for column in conn.execute("PRAGMA table_info(semantic_proposals)")
    }
    conn.close()
