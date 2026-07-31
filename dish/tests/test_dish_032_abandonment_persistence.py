from __future__ import annotations

import json
import sqlite3

import pytest

from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.constants import SCHEMA_VERSION
from dish_tool.database import (
    apply_operation_abandonment_succession_in_transaction,
    assert_clean_abandonment_restart_source,
    confirm_task_content,
    create_abandonment_attempt_in_transaction,
    create_operation,
    create_verification_cycle,
    declare_operation_step,
)
from dish_tool.database_schema import (
    MIGRATIONS,
    _execute_script_statements,
    _validate_semantic_evidence,
    initialize_database,
)
from dish_tool.errors import DishRuleError
from dish_tool.models import OperationActors


def _source(
    conn: sqlite3.Connection,
    *,
    task_gid: str = "task-1",
    operation_kind: str = "initial",
    owner_id: str = "owner-1",
    run_id: str = "run-1",
    phase: str = "prepare_required",
    cycle: bool = False,
):
    identity = confirm_task_content(
        conn,
        task_gid=task_gid,
        title=f"Dish {task_gid}",
        notes=f"Notes {task_gid}",
        schema_version="2",
    )
    operation = create_operation(
        conn,
        task_gid=task_gid,
        operation_kind=operation_kind,
        expected_identity=identity.digest,
        schema_version="2",
        expected_section_gid="section-1",
        actors=OperationActors(
            editor_agent="gpt",
            researcher_agent="gpt",
            run_id=run_id,
        ),
    )
    if phase != "prepare_required":
        conn.execute(
            "UPDATE operations SET phase=? WHERE operation_id=?",
            (phase, operation["operation_id"]),
        )
    cycle_row = None
    context_cycle_id = None
    if cycle:
        cycle_row = create_verification_cycle(
            conn,
            operation_id=operation["operation_id"],
            task_gid=task_gid,
            cycle_number=1,
            protocol_release="verification-v1",
            protocol_text="protocol",
            verifier_agent="claude",
            run_id=run_id,
            independence_attestation="independent",
        )
        context_cycle_id = cycle_row["cycle_id"]
    lease = LeaseManager(conn).acquire(
        operation["operation_id"],
        ServicePrincipal(owner_id, run_id),
        context_cycle_id=context_cycle_id,
    )
    source_version_id = conn.execute(
        "SELECT last_confirmed_content_version_id FROM task_content_state WHERE task_gid=?",
        (task_gid,),
    ).fetchone()[0]
    return operation, lease, cycle_row, source_version_id


def _start_abandonment(
    conn: sqlite3.Connection,
    *,
    operation,
    lease,
    cycle=None,
    abandonment_id: str = "abandonment-1",
):
    if lease["released_at"] is None:
        LeaseManager(conn).release(
            operation["operation_id"], None, reason="stale actor released", admin=True
        )
        lease = conn.execute(
            "SELECT * FROM service_leases WHERE lease_id=?", (lease["lease_id"],)
        ).fetchone()
    conn.execute("BEGIN IMMEDIATE")
    row = create_abandonment_attempt_in_transaction(
        conn,
        abandonment_id=abandonment_id,
        task_gid=operation["task_gid"],
        source_operation_id=operation["operation_id"],
        source_lease_id=lease["lease_id"],
        abandoned_owner_id=lease["owner_id"],
        abandoned_run_id=lease["run_id"],
        attempt_cycle_id=None if cycle is None else cycle["cycle_id"],
        reason="conversation permanently unavailable",
    )
    conn.execute("COMMIT")
    return row


def test_schema_33_adds_abandonment_lineage_and_prepared_claim_state():
    conn = initialize_database(":memory:")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 35
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"abandonment_attempts", "operation_successions"} <= tables
    columns = {
        row[1] for row in conn.execute("PRAGMA table_info(operations)")
    }
    assert "successor_claim_mode" in columns



def test_migration_32_preserves_stage_2_actor_attempt_context(tmp_path):
    db_path = tmp_path / "stage-2.sqlite"
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        for version in range(1, 32):
            _execute_script_statements(conn, MIGRATIONS[version])
            conn.execute(
                "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (version, f"v{version}"),
            )
            conn.execute(f"PRAGMA user_version = {version}")
        conn.execute(
            """INSERT INTO operations(
                   operation_id,task_gid,operation_kind,status,phase,
                   expected_identity,expected_section_gid,schema_version,created_at
               ) VALUES('op','task','initial','open','prepare_required',
                        'identity','section','2','2026-07-30T00:00:00Z')"""
        )
        conn.execute(
            """INSERT INTO service_leases(
                   lease_id,operation_id,task_gid,owner_id,run_id,
                   acquired_at,renewed_at,expires_at,lease_kind,
                   actor_attempt_seq,context_cycle_id
               ) VALUES('lease','op','task','owner','run',
                        '2026-07-30T00:00:00Z','2026-07-30T00:00:00Z',
                        '2026-07-30T01:00:00Z','actor',1,NULL)"""
        )
        conn.execute("COMMIT")
    finally:
        conn.close()

    upgraded = initialize_database(db_path)
    try:
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == 35
        assert upgraded.execute(
            "SELECT successor_claim_mode FROM operations WHERE operation_id='op'"
        ).fetchone()[0] == "none"
        assert tuple(upgraded.execute(
            "SELECT lease_kind,actor_attempt_seq,context_cycle_id FROM service_leases WHERE lease_id='lease'"
        ).fetchone()) == ("actor", 1, None)
        assert upgraded.execute(
            "SELECT COUNT(*) FROM abandonment_attempts"
        ).fetchone()[0] == 0
        assert upgraded.execute(
            "SELECT COUNT(*) FROM operation_successions"
        ).fetchone()[0] == 0
    finally:
        upgraded.close()

def test_abandonment_requires_latest_exact_actor_attempt_and_one_active_per_task():
    conn = initialize_database(":memory:")
    operation, first, _, _ = _source(conn)
    manager = LeaseManager(conn)
    manager.release(operation["operation_id"], None, reason="admin release", admin=True)
    second = manager.acquire(
        operation["operation_id"], ServicePrincipal("owner-1", "run-1")
    )

    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(DishRuleError) as exc:
        create_abandonment_attempt_in_transaction(
            conn,
            abandonment_id="old-attempt",
            task_gid=operation["task_gid"],
            source_operation_id=operation["operation_id"],
            source_lease_id=first["lease_id"],
            abandoned_owner_id=first["owner_id"],
            abandoned_run_id=first["run_id"],
            reason="old lease",
        )
    assert exc.value.rule == "abandonment_authority_invalid"
    conn.execute("ROLLBACK")

    current = _start_abandonment(
        conn,
        operation=operation,
        lease=second,
        abandonment_id="current-attempt",
    )
    assert current["status"] == "started"

    other_identity = confirm_task_content(
        conn,
        task_gid="task-2",
        title="Other",
        notes="Other notes",
        schema_version="2",
    )
    other = create_operation(
        conn,
        task_gid="task-2",
        operation_kind="initial",
        expected_identity=other_identity.digest,
        schema_version="2",
        expected_section_gid="section-1",
        actors=OperationActors(editor_agent="gpt", researcher_agent="gpt", run_id="run-2"),
    )
    other_lease = LeaseManager(conn).acquire(
        other["operation_id"], ServicePrincipal("owner-2", "run-2")
    )
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(DishRuleError):
        create_abandonment_attempt_in_transaction(
            conn,
            abandonment_id="wrong-task-copy",
            task_gid=operation["task_gid"],
            source_operation_id=other["operation_id"],
            source_lease_id=other_lease["lease_id"],
            abandoned_owner_id=other_lease["owner_id"],
            abandoned_run_id=other_lease["run_id"],
            reason="wrong task",
        )
    conn.execute("ROLLBACK")



def test_live_actor_lease_cannot_start_abandonment():
    conn = initialize_database(":memory:")
    operation, lease, _, _ = _source(conn)
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(DishRuleError) as exc:
        create_abandonment_attempt_in_transaction(
            conn,
            abandonment_id="live-lease",
            task_gid=operation["task_gid"],
            source_operation_id=operation["operation_id"],
            source_lease_id=lease["lease_id"],
            abandoned_owner_id=lease["owner_id"],
            abandoned_run_id=lease["run_id"],
            reason="still live",
        )
    assert exc.value.rule == "abandonment_authority_invalid"
    conn.execute("ROLLBACK")

def test_admin_request_lease_cannot_become_abandoned_actor_attempt():
    conn = initialize_database(":memory:")
    operation, actor_lease, _, _ = _source(conn)
    LeaseManager(conn).release(operation["operation_id"], None, reason="replace", admin=True)
    admin_lease = LeaseManager(conn).acquire(
        operation["operation_id"],
        ServicePrincipal("marco", "admin-run"),
        lease_kind="admin_request",
    )
    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(DishRuleError) as exc:
        create_abandonment_attempt_in_transaction(
            conn,
            abandonment_id="admin-not-actor",
            task_gid=operation["task_gid"],
            source_operation_id=operation["operation_id"],
            source_lease_id=admin_lease["lease_id"],
            abandoned_owner_id=admin_lease["owner_id"],
            abandoned_run_id=admin_lease["run_id"],
            reason="not actor authority",
        )
    assert exc.value.rule == "abandonment_authority_invalid"
    conn.execute("ROLLBACK")
    assert actor_lease["lease_kind"] == "actor"


def test_clean_succession_is_one_atomic_source_successor_baseline_transition():
    conn = initialize_database(":memory:")
    operation, lease, _, source_version_id = _source(conn)
    _start_abandonment(conn, operation=operation, lease=lease)

    conn.execute("BEGIN IMMEDIATE")
    abandonment, successor, succession = apply_operation_abandonment_succession_in_transaction(
        conn,
        abandonment_id="abandonment-1",
        succession_id="succession-1",
        successor_operation_id="successor-1",
        source_content_version_id=source_version_id,
        successor_content_version_id="successor-version-1",
        successor_operation_kind="initial",
        successor_phase="prepare_required",
        successor_expected_section_gid="section-1",
        successor_schema_version="2",
        successor_claim_mode="stage_actor",
        transition_reason="agent permanently unavailable",
        candidate_transfer_kind="restored_stage_baseline",
        result={"required_action": "start"},
    )
    _validate_semantic_evidence(conn)
    conn.execute("COMMIT")

    source = conn.execute(
        "SELECT * FROM operations WHERE operation_id=?", (operation["operation_id"],)
    ).fetchone()
    baseline = conn.execute(
        "SELECT * FROM content_versions WHERE content_version_id='successor-version-1'"
    ).fetchone()
    source_version = conn.execute(
        "SELECT * FROM content_versions WHERE content_version_id=?", (source_version_id,)
    ).fetchone()
    released = conn.execute(
        "SELECT * FROM service_leases WHERE lease_id=?", (lease["lease_id"],)
    ).fetchone()

    assert (source["status"], source["phase"], source["terminal_outcome"]) == (
        "cancelled",
        "terminal",
        "agent_abandoned",
    )
    assert successor["successor_claim_mode"] == "stage_actor"
    assert successor["run_id"] is None
    assert successor["content_write_completed_at"] is None
    assert baseline["boundary"] == "successor_baseline"
    assert (baseline["identity"], baseline["title"], baseline["notes"]) == (
        source_version["identity"],
        source_version["title"],
        source_version["notes"],
    )
    assert abandonment["status"] == "awaiting_successor_claim"
    assert abandonment["successor_operation_id"] == successor["operation_id"]
    assert succession["abandonment_id"] == abandonment["abandonment_id"]
    assert released["released_at"] is not None
    assert released["release_reason"] == "stale actor released"


def test_clean_restart_rejects_pending_steps_and_unresolved_effects():
    conn = initialize_database(":memory:")
    operation, lease, _, source_version_id = _source(conn)
    declare_operation_step(conn, operation["operation_id"], "candidate_write", {"x": 1})
    _start_abandonment(conn, operation=operation, lease=lease)

    conn.execute("BEGIN IMMEDIATE")
    with pytest.raises(DishRuleError) as exc:
        apply_operation_abandonment_succession_in_transaction(
            conn,
            abandonment_id="abandonment-1",
            succession_id="succession-pending",
            successor_operation_id="successor-pending",
            source_content_version_id=source_version_id,
            successor_content_version_id="version-pending",
            successor_operation_kind="initial",
            successor_phase="prepare_required",
            successor_expected_section_gid="section-1",
            successor_schema_version="2",
            successor_claim_mode="stage_actor",
            transition_reason="agent unavailable",
            candidate_transfer_kind="restored_stage_baseline",
        )
    assert exc.value.rule == "abandonment_pending_steps"
    conn.execute("ROLLBACK")
    assert conn.execute(
        "SELECT status FROM operations WHERE operation_id=?", (operation["operation_id"],)
    ).fetchone()[0] == "open"

    other = initialize_database(":memory:")
    other_operation, _, _, _ = _source(other, task_gid="task-unresolved")
    other.execute(
        """INSERT INTO write_attempts(
               attempt_id,operation_id,expected_identity,outcome,started_at,
               purpose,schema_version,context_json
           ) VALUES('write-1',?,?,'uncertain','now','content_write','2','{}')""",
        (other_operation["operation_id"], other_operation["expected_identity"]),
    )
    with pytest.raises(DishRuleError) as exc:
        assert_clean_abandonment_restart_source(
            other, source_operation_id=other_operation["operation_id"]
        )
    assert exc.value.rule == "abandonment_unresolved_effects"


def test_verification_succession_closes_only_exact_incomplete_cycle_and_creates_unbound_cycle():
    conn = initialize_database(":memory:")
    operation, lease, cycle, source_version_id = _source(
        conn, phase="await_verification", cycle=True
    )
    _start_abandonment(conn, operation=operation, lease=lease, cycle=cycle)

    conn.execute("BEGIN IMMEDIATE")
    abandonment, successor, succession = apply_operation_abandonment_succession_in_transaction(
        conn,
        abandonment_id="abandonment-1",
        succession_id="verification-succession",
        successor_operation_id="verification-successor",
        source_content_version_id=source_version_id,
        successor_content_version_id="verification-baseline",
        successor_operation_kind="initial",
        successor_phase="await_verification",
        successor_expected_section_gid="section-1",
        successor_schema_version="2",
        successor_claim_mode="verifier",
        transition_reason="verifier unavailable",
        candidate_transfer_kind="inherited_confirmed_candidate",
        source_cycle_id=cycle["cycle_id"],
        close_source_cycle_as_abandoned=True,
        successor_cycle_id="cycle-2",
        successor_cycle_number=2,
        successor_protocol_release="verification-v1",
        successor_protocol_text="protocol",
    )
    _validate_semantic_evidence(conn)
    conn.execute("COMMIT")

    closed = conn.execute(
        "SELECT * FROM verification_cycles WHERE cycle_id=?", (cycle["cycle_id"],)
    ).fetchone()
    fresh = conn.execute(
        "SELECT * FROM verification_cycles WHERE cycle_id='cycle-2'"
    ).fetchone()
    assert closed["outcome"] == "abandoned"
    assert closed["completed_at"] is not None
    assert closed["signed_identity"] is None
    assert fresh["operation_id"] == successor["operation_id"]
    assert fresh["verifier_agent"] is None
    assert fresh["run_id"] is None
    assert fresh["reviewed_identity"] is None
    assert abandonment["successor_cycle_id"] == fresh["cycle_id"]
    assert succession["source_cycle_id"] == closed["cycle_id"]


def test_succession_rolls_back_as_one_unit_on_late_failure():
    conn = initialize_database(":memory:")
    operation, lease, _, source_version_id = _source(conn)
    _start_abandonment(conn, operation=operation, lease=lease)

    conn.execute("BEGIN IMMEDIATE")
    apply_operation_abandonment_succession_in_transaction(
        conn,
        abandonment_id="abandonment-1",
        succession_id="rollback-succession",
        successor_operation_id="rollback-successor",
        source_content_version_id=source_version_id,
        successor_content_version_id="rollback-baseline",
        successor_operation_kind="initial",
        successor_phase="prepare_required",
        successor_expected_section_gid="section-1",
        successor_schema_version="2",
        successor_claim_mode="stage_actor",
        transition_reason="agent unavailable",
        candidate_transfer_kind="restored_stage_baseline",
    )
    conn.execute("ROLLBACK")

    source = conn.execute(
        "SELECT status,terminal_outcome FROM operations WHERE operation_id=?",
        (operation["operation_id"],),
    ).fetchone()
    abandonment = conn.execute(
        "SELECT status,successor_operation_id FROM abandonment_attempts WHERE abandonment_id='abandonment-1'"
    ).fetchone()
    assert tuple(source) == ("open", None)
    assert tuple(abandonment) == ("started", None)
    assert conn.execute(
        "SELECT 1 FROM operations WHERE operation_id='rollback-successor'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM operation_successions WHERE succession_id='rollback-succession'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT release_reason FROM service_leases WHERE lease_id=?", (lease["lease_id"],)
    ).fetchone()[0] == "stale actor released"


def test_agent_abandoned_source_and_succession_are_immutable():
    conn = initialize_database(":memory:")
    operation, lease, _, source_version_id = _source(conn)
    _start_abandonment(conn, operation=operation, lease=lease)
    conn.execute("BEGIN IMMEDIATE")
    apply_operation_abandonment_succession_in_transaction(
        conn,
        abandonment_id="abandonment-1",
        succession_id="immutable-succession",
        successor_operation_id="immutable-successor",
        source_content_version_id=source_version_id,
        successor_content_version_id="immutable-baseline",
        successor_operation_kind="initial",
        successor_phase="prepare_required",
        successor_expected_section_gid="section-1",
        successor_schema_version="2",
        successor_claim_mode="stage_actor",
        transition_reason="agent unavailable",
        candidate_transfer_kind="restored_stage_baseline",
    )
    conn.execute("COMMIT")

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE operations SET verifier_agent='claude' WHERE operation_id=?",
            (operation["operation_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO operation_steps VALUES(?, 'late', '{}', NULL)",
            (operation["operation_id"],),
        )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE operation_successions SET transition_reason='changed' WHERE succession_id='immutable-succession'"
        )


def test_abandonment_tables_are_not_exposed_as_commands():
    from dish_service.command_spec import ACTION_COMMANDS

    assert "abandon-operation" not in ACTION_COMMANDS
    assert "reconcile-abandonment" not in ACTION_COMMANDS
