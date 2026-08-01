from __future__ import annotations

import sqlite3
import threading

import pytest

from dish_service.leases import LeaseManager, ServicePrincipal
from dish_tool.database import transition_operation
from dish_tool.database_schema import initialize_database
from dish_tool.errors import DishRuleError
from tests.support.thread_teardown import join_thread, managed_thread
from tests.support.operational import _approved, _service
from tests.support.submission import _signed


@pytest.mark.flake_stress
def test_inspect_waits_for_submit_transaction_and_returns_completed_state(
    tmp_path, monkeypatch
):
    import dish_tool.step9 as step9

    service, _backend = _service(tmp_path)
    operation_id, verifier = _approved(service)
    terminal_audit_entered = threading.Event()
    release_terminal_audit = threading.Event()
    inspect_database_attempted = threading.Event()
    inspect_done = threading.Event()
    submitted: list[dict] = []
    inspected: list[dict] = []
    thread_errors: list[BaseException] = []
    real_record_audit = step9.record_audit
    import dish_tool.database_schema as database_schema
    real_migrate_database = database_schema.migrate_database

    def pause_terminal_audit(*args, **kwargs):
        if kwargs.get("event_type") == "operation.submitted":
            terminal_audit_entered.set()
            assert release_terminal_audit.wait(5)
        return real_record_audit(*args, **kwargs)

    def observed_migrate_database(conn):
        if threading.current_thread().name == "partial-submit-inspector":
            inspect_database_attempted.set()
        return real_migrate_database(conn)

    monkeypatch.setattr(step9, "record_audit", pause_terminal_audit)
    monkeypatch.setattr(database_schema, "migrate_database", observed_migrate_database)

    def submit():
        try:
            submitted.append(
                service.execute_agent(
                    "submit", {"submission_id": operation_id}, principal=verifier
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            thread_errors.append(exc)

    submit_thread = managed_thread(target=submit, name="partial-submit-writer")
    submit_thread.start()
    assert terminal_audit_entered.wait(5)

    def inspect():
        try:
            inspected.append(
                service.execute_agent(
                    "inspect",
                    {"agent": "codex", "submission_id": operation_id},
                    principal=verifier,
                )
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            thread_errors.append(exc)
        finally:
            inspect_done.set()

    inspect_thread = managed_thread(target=inspect, name="partial-submit-inspector")
    inspect_thread.start()
    assert inspect_database_attempted.wait(timeout=5), (
        "inspector never reached database initialization while submit was paused"
    )
    assert not inspect_done.is_set(), (
        "inspector returned while terminal evidence was still uncommitted"
    )

    release_terminal_audit.set()
    join_thread(submit_thread, timeout=5)
    join_thread(inspect_thread, timeout=5)
    assert not submit_thread.is_alive()
    assert not inspect_thread.is_alive()
    assert thread_errors == []
    assert submitted[0]["ok"]
    assert inspected[0]["ok"]
    assert inspected[0]["state"] == "completed"


@pytest.mark.flake_stress
def test_concurrent_read_accepts_durable_submit_intent_during_external_move(
    tmp_path, monkeypatch
):
    service, backend = _service(tmp_path)
    operation_id, verifier = _approved(service)
    movement_entered = threading.Event()
    release_movement = threading.Event()
    submitted: list[dict] = []
    real_move = backend.move_task_to_section

    def pause_external_move(*args, **kwargs):
        movement_entered.set()
        assert release_movement.wait(5)
        return real_move(*args, **kwargs)

    monkeypatch.setattr(backend, "move_task_to_section", pause_external_move)
    submit_thread = managed_thread(
        target=lambda: submitted.append(
            service.execute_agent(
                "submit", {"submission_id": operation_id}, principal=verifier
            )
        )
    )
    submit_thread.start()
    assert movement_entered.wait(5)

    # The started movement attempt and execution claim are durable recovery
    # authority around the Asana call. They are intentionally visible and must
    # validate as an in-progress operation, not as database corruption.
    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=verifier,
    )
    assert inspected["ok"]
    assert inspected["submission_id"] == operation_id
    visible = initialize_database(service.config.db_path)
    try:
        attempt = visible.execute(
            "SELECT outcome FROM movement_attempts "
            "WHERE operation_id=? AND purpose='destination_submission' "
            "ORDER BY started_at DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        execution = visible.execute(
            "SELECT status FROM operation_executions WHERE operation_id=? "
            "ORDER BY created_at DESC LIMIT 1",
            (operation_id,),
        ).fetchone()
        assert attempt["outcome"] == "started"
        assert execution["status"] == "started"
    finally:
        visible.close()

    release_movement.set()
    join_thread(submit_thread, timeout=5)
    assert not submit_thread.is_alive()
    assert submitted[0]["ok"]


@pytest.mark.flake_stress
def test_concurrent_read_accepts_terminal_lease_cleanup_tail(tmp_path, monkeypatch):
    service, _backend = _service(tmp_path)
    operation_id, verifier = _approved(service)
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    submitted: list[dict] = []
    real_finalize = service._finalize_successful_lease

    def pause_cleanup(**kwargs):
        cleanup_entered.set()
        assert release_cleanup.wait(5)
        return real_finalize(**kwargs)

    monkeypatch.setattr(service, "_finalize_successful_lease", pause_cleanup)
    submit_thread = managed_thread(
        target=lambda: submitted.append(
            service.execute_agent(
                "submit", {"submission_id": operation_id}, principal=verifier
            )
        )
    )
    submit_thread.start()
    assert cleanup_entered.wait(5)

    # The workflow is already terminal while transport lease cleanup is still
    # in progress. That lease has no mutation authority and must not make a
    # fresh read classify the database as semantically corrupt.
    inspected = service.execute_agent(
        "inspect",
        {"agent": "codex", "submission_id": operation_id},
        principal=verifier,
    )
    assert inspected["ok"]
    assert inspected["state"] == "completed"
    assert inspected["allowed_actions"] == []

    release_cleanup.set()
    join_thread(submit_thread, timeout=5)
    assert not submit_thread.is_alive()
    assert submitted[0]["ok"]


def test_submit_terminal_step_transition_and_audits_roll_back_together(
    tmp_path, monkeypatch
):
    import dish_tool.step9 as step9

    application, _backend, operation_id = _signed(tmp_path)
    real_record_audit = step9.record_audit

    def fail_submission_audit(*args, **kwargs):
        if kwargs.get("event_type") == "operation.submitted":
            raise sqlite3.OperationalError("simulated submission audit failure")
        return real_record_audit(*args, **kwargs)

    monkeypatch.setattr(step9, "record_audit", fail_submission_audit)
    failed = application.execute("submit", submission_id=operation_id)
    assert failed["code"] == "BACKEND_UNCERTAIN"

    operation = application.conn.execute(
        "SELECT status,phase,terminal_outcome,completed_at FROM operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    terminal_step = application.conn.execute(
        "SELECT 1 FROM operation_steps WHERE operation_id=? AND step_name='submission_terminal'",
        (operation_id,),
    ).fetchone()
    submitted_audit = application.conn.execute(
        "SELECT 1 FROM audit_events WHERE operation_id=? AND event_type='operation.submitted'",
        (operation_id,),
    ).fetchone()
    terminal_transition = application.conn.execute(
        """SELECT 1 FROM audit_events
             WHERE operation_id=? AND event_type='operation.transition'
               AND json_extract(details, '$.to_status')='completed'""",
        (operation_id,),
    ).fetchone()

    assert tuple(operation) == ("open", "await_submission", None, None)
    assert terminal_step is None
    assert submitted_audit is None
    assert terminal_transition is None


def test_next_task_lease_does_not_reap_incomplete_terminal_evidence(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    conn.execute(
        """INSERT INTO operations(
               operation_id,task_gid,operation_kind,status,expected_identity,
               schema_version,created_at,phase,expected_section_gid
           ) VALUES('old','task','initial','open','identity','2','now',
                    'prepare_required','research')"""
    )
    old_lease = LeaseManager(conn).acquire(
        "old", ServicePrincipal("owner", "old-run")
    )
    conn.execute(
        "INSERT INTO operation_steps(operation_id,step_name,intended_json) "
        "VALUES('old','terminal_pending','{}')"
    )
    transition_operation(
        conn,
        "old",
        phase="terminal",
        status="completed",
        terminal_outcome="destination_handled",
    )
    conn.execute(
        """INSERT INTO operations(
               operation_id,task_gid,operation_kind,status,expected_identity,
               schema_version,created_at,phase,expected_section_gid
           ) VALUES('new','task','initial','open','identity-2','2','later',
                    'prepare_required','research')"""
    )

    with pytest.raises(DishRuleError) as exc:
        LeaseManager(conn).acquire(
            "new", ServicePrincipal("owner", "new-run")
        )
    assert exc.value.rule == "task_lease_held"
    active = conn.execute(
        "SELECT released_at FROM service_leases WHERE lease_id=?",
        (old_lease["lease_id"],),
    ).fetchone()
    assert active["released_at"] is None
    conn.close()


def test_next_task_lease_reaps_safe_terminal_cleanup_tail(tmp_path):
    db_path = tmp_path / "dish.db"
    conn = initialize_database(db_path)
    conn.execute(
        """INSERT INTO operations(
               operation_id,task_gid,operation_kind,status,expected_identity,
               schema_version,created_at,phase,expected_section_gid
           ) VALUES('old','task','initial','open','identity','2','now',
                    'prepare_required','research')"""
    )
    old_owner = ServicePrincipal("owner", "old-run")
    old_lease = LeaseManager(conn).acquire("old", old_owner)
    transition_operation(
        conn,
        "old",
        phase="terminal",
        status="completed",
        terminal_outcome="destination_handled",
    )
    conn.execute(
        """INSERT INTO operations(
               operation_id,task_gid,operation_kind,status,expected_identity,
               schema_version,created_at,phase,expected_section_gid
           ) VALUES('new','task','initial','open','identity-2','2','later',
                    'prepare_required','research')"""
    )

    new_owner = ServicePrincipal("owner", "new-run")
    new_lease = LeaseManager(conn).acquire("new", new_owner)
    reaped = conn.execute(
        "SELECT released_at,release_reason FROM service_leases WHERE lease_id=?",
        (old_lease["lease_id"],),
    ).fetchone()

    assert reaped["released_at"] is not None
    assert reaped["release_reason"] == "terminal_lease_reaped"
    assert new_lease["operation_id"] == "new"
    assert LeaseManager(conn).release_terminal("old", old_owner) is None
    conn.close()
