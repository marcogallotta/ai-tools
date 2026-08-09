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
    record_actor_fact,
)
from dish_tool.database_initialization import initialize_database
from dish_tool.database_migrations import _execute_script_statements
from dish_tool.database_schema import (
    MIGRATIONS,
    _validate_semantic_evidence,
)
from dish_tool.errors import DishRuleError
from dish_tool.models import OperationActors
from tests.support.abandonment_scenarios import (
    persistence_source as _source,
    start_abandonment as _start_abandonment,
)





def test_schema_33_adds_abandonment_lineage_and_prepared_claim_state():
    conn = initialize_database(":memory:")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 41
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
        assert upgraded.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
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
    record_actor_fact(
        conn,
        operation_id=operation["operation_id"],
        task_gid=operation["task_gid"],
        role="verifier",
        agent="gpt",
        run_id=second["run_id"],
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
    with pytest.raises(DishRuleError) as wrong_task:
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
    assert wrong_task.value.rule == "abandonment_authority_invalid"
    conn.execute("ROLLBACK")
def test_abandonment_permits_cycle_owning_attempt_when_later_attempt_never_engaged_operation():
    conn = initialize_database(":memory:")
    operation, first, _, _ = _source(conn)
    manager = LeaseManager(conn)
    manager.release(operation["operation_id"], None, reason="admin release", admin=True)
    second = manager.acquire(
        operation["operation_id"], ServicePrincipal("owner-2", "run-2")
    )
    manager.release(operation["operation_id"], None, reason="admin release", admin=True)
    manager.acquire(operation["operation_id"], ServicePrincipal("owner-3", "run-3"))
    manager.release(operation["operation_id"], None, reason="admin release", admin=True)

    conn.execute("BEGIN IMMEDIATE")
    row = create_abandonment_attempt_in_transaction(
        conn,
        abandonment_id="cycle-owner-attempt",
        task_gid=operation["task_gid"],
        source_operation_id=operation["operation_id"],
        source_lease_id=second["lease_id"],
        abandoned_owner_id=second["owner_id"],
        abandoned_run_id=second["run_id"],
        reason="run-3 never claimed operation actor facts",
    )
    conn.execute("COMMIT")
    assert row["status"] == "started"
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
