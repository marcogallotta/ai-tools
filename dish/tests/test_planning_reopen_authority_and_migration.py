from __future__ import annotations

import shlex
import sqlite3
import threading

import pytest

import dish_service.application as service_application
import dish_tool.task_store as task_store
from dish_service.application import DishService
from dish_service.leases import ServicePrincipal
from dish_service.request_replay import begin_request, complete_request
from dish_service.admin_cli import build_parser as build_admin_parser
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database import (
    begin_planning_reopen_attempt,
    finish_planning_reopen_attempt,
)
from dish_tool.database_initialization import initialize_database
from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope
from dish_tool.database_schema import MIGRATIONS, _execute_script_statements
from tests.support.planning_intent import confirmed_planning_start
from tests.support.thread_teardown import join_thread, managed_thread
from tests.support.request_restore import Backend, _service
from tests.support.planning_reopen import (
    ADMIN,
    ARGS,
    FRESH_ADMIN_ID,
    FRESH_START_CHALLENGE_ID,
    FRESH_START_ID,
    PLANNER,
    REQUEST_ID,
    CompletedBackend,
    SimulatedProcessDeath,
    assert_unresolved_blocked as _assert_unresolved_blocked,
    exact_replay as _exact_replay,
    fresh_reopen as _fresh_reopen,
    restart as _restart,
    rows as _rows,
    start as _start,
)



















def test_concurrent_planning_start_cannot_cross_reopen_attempt_insertion(
    tmp_path, monkeypatch
):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)
    start_reached_live_read = threading.Event()
    continue_start = threading.Event()
    original_read = backend.read_task
    paused = False

    def coordinated_read(gid):
        nonlocal paused
        if (
            threading.current_thread().name == "planning-start"
            and not paused
        ):
            paused = True
            start_reached_live_read.set()
            assert continue_start.wait(timeout=5)
        return original_read(gid)

    backend.read_task = coordinated_read
    result_box = {}

    def run_start():
        result_box["result"] = _start(service)

    thread = managed_thread(target=run_start, name="planning-start")
    thread.start()
    assert start_reached_live_read.wait(timeout=5)

    with monkeypatch.context() as killed:
        killed.setattr(
            task_store,
            "finish_planning_reopen_attempt",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                SimulatedProcessDeath("leave reopen unresolved")
            ),
        )
        with pytest.raises(SimulatedProcessDeath):
            service.execute_admin(
                "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
            )

    assert backend.completed is False
    continue_start.set()
    join_thread(thread, timeout=5)
    assert not thread.is_alive()
    result = result_box["result"]
    assert result["code"] == "BACKEND_UNCERTAIN"
    assert result["errors"][0]["rule"] == "planning_reopen_reconciliation_required"
    assert result["data"]["original_request_id"] == REQUEST_ID

    conn = initialize_database(service.config.db_path)
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM operations WHERE task_gid='t'"
        ).fetchone()[0] == 0
    finally:
        conn.close()
def test_changed_exact_request_reuse_still_conflicts(tmp_path, monkeypatch):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)

    with monkeypatch.context() as killed:
        killed.setattr(
            backend,
            "update_task_completed",
            lambda **kwargs: (_ for _ in ()).throw(SimulatedProcessDeath()),
        )
        with pytest.raises(SimulatedProcessDeath):
            service.execute_admin(
                "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
            )

    changed = service.execute_admin(
        "reopen-planning",
        {"task_gid": "t", "reason": "different reason"},
        principal=ADMIN,
        request_id=REQUEST_ID,
    )
    assert changed["code"] == "CONFLICT"
    assert changed["errors"][0]["rule"] == "service_request_identity_conflict"
def test_exact_replay_preserves_owner_and_run_binding(tmp_path, monkeypatch):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)

    with monkeypatch.context() as killed:
        killed.setattr(
            backend,
            "update_task_completed",
            lambda **kwargs: (_ for _ in ()).throw(SimulatedProcessDeath()),
        )
        with pytest.raises(SimulatedProcessDeath):
            service.execute_admin(
                "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
            )

    wrong_run = service.execute_admin(
        "reopen-planning",
        ARGS,
        principal=ServicePrincipal(owner_id=ADMIN.owner_id, run_id="other-run"),
        request_id=REQUEST_ID,
    )
    assert wrong_run["code"] == "CONFLICT"
    assert wrong_run["errors"][0]["rule"] == "service_request_identity_conflict"
    assert backend.completed is True
    assert backend.reopens == 0
def test_not_applied_attempt_is_terminal_and_replays_without_retry(tmp_path):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)

    def reject_without_effect(*, task_gid, completed):
        from dish_tool.errors import BackendFailure
        raise BackendFailure(
            "BACKEND_REJECTED", "Asana rejected reopen",
            rule="backend_rejected", retryable=True,
        )

    backend.update_task_completed = reject_without_effect
    first = service.execute_admin(
        "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
    )
    assert first["code"] == "BACKEND_REJECTED"
    attempt, request, domain, invocation = _rows(service)
    assert attempt["outcome"] == "not_applied"
    assert request["status"] == "completed"
    assert domain == 0
    assert invocation == 1

    replay = _exact_replay(service)
    assert replay["code"] == "BACKEND_REJECTED"
    assert replay["data"]["request_replayed"] is True
    assert backend.completed is True
def test_recovery_command_preserves_exact_task_reason_and_request(tmp_path, monkeypatch):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)

    with monkeypatch.context() as killed:
        killed.setattr(
            backend,
            "update_task_completed",
            lambda **kwargs: (_ for _ in ()).throw(SimulatedProcessDeath()),
        )
        with pytest.raises(SimulatedProcessDeath):
            service.execute_admin(
                "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
            )

    blocked = _start(service)
    argv = shlex.split(blocked["data"]["admin_command"])
    parsed = build_admin_parser().parse_args(argv[1:])
    assert parsed.command == "reopen-planning"
    assert parsed.task_gid == ARGS["task_gid"]
    assert parsed.reason == ARGS["reason"]
    assert parsed.request_id == REQUEST_ID
def test_orphan_attempt_blocks_with_explicit_authority_conflict(tmp_path):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)
    conn = initialize_database(service.config.db_path)
    try:
        begin_planning_reopen_attempt(
            conn,
            task_gid="t",
            expected_identity=task_store.read_complete_task(
                backend, task_gid="t", project_gid=COOKING_PROJECT_GID
            ).identity,
            expected_section_gid="rq",
            expected_modified_at="m0",
            reason=ARGS["reason"],
            actor_run_id=ADMIN.run_id,
            request_id=None,
        )
    finally:
        conn.close()

    result = _start(service)
    assert result["code"] == "BACKEND_UNCERTAIN"
    assert result["data"]["replay_original_request"] is False
    assert result["data"]["required_admin_action"] == "manual-reconciliation"
    assert result["data"]["admin_command"] is None
    assert result["data"]["authority_conflict"]
def test_historical_terminal_request_remains_blocked_for_manual_authority(tmp_path):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)
    conn = initialize_database(service.config.db_path)
    try:
        begin_request(
            conn, request_id=REQUEST_ID, owner_id=ADMIN.owner_id,
            run_id=ADMIN.run_id, command="reopen-planning", arguments=ARGS,
        )
        live = task_store.read_complete_task(
            backend, task_gid="t", project_gid=COOKING_PROJECT_GID
        )
        begin_planning_reopen_attempt(
            conn, task_gid="t", expected_identity=live.identity,
            expected_section_gid=live.section_gid,
            expected_modified_at=live.modified_at,
            expected_version_source=live.version_source,
            expected_version_reliable=("completion" in live.version_reliable_for),
            reason=ARGS["reason"], actor_run_id=ADMIN.run_id, request_id=REQUEST_ID,
        )
        complete_request(
            conn, request_id=REQUEST_ID,
            result=error_envelope(
                "reopen-planning",
                DishRuleError(
                    "INTERNAL_ERROR", "historical terminal result",
                    rule="unexpected_internal_failure",
                ),
                task_gid="t",
            ),
        )
    finally:
        conn.close()

    startup = service.startup_check()["startup"]["planning_reopen_recovery"]
    assert startup["resume_safe"] == 1
    pending = startup["pending"][0]
    assert pending["original_request_status"] == "completed"
    assert pending["replay_original_request"] is False
    assert pending["required_admin_action"] == "manual-reconciliation"
    assert pending["authority_conflict"]
    blocked = _start(service)
    assert blocked["data"]["required_admin_action"] == "manual-reconciliation"
@pytest.mark.parametrize("attempt_outcome", ["started", "uncertain"])
def test_migration_reopens_historical_uncertain_request_for_reconciliation(
    tmp_path, attempt_outcome
):
    db_path = tmp_path / "historical.sqlite3"
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    for version in range(1, 30):
        _execute_script_statements(conn, MIGRATIONS[version])
        conn.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
            (version, f"v{version}"),
        )
        conn.execute(f"PRAGMA user_version = {version}")
    conn.execute("COMMIT")
    begin_request(
        conn,
        request_id=REQUEST_ID,
        owner_id=ADMIN.owner_id,
        run_id=ADMIN.run_id,
        command="reopen-planning",
        arguments=ARGS,
    )
    attempt = begin_planning_reopen_attempt(
        conn,
        task_gid="t",
        expected_identity="identity",
        expected_section_gid="rq",
        expected_modified_at="m0",
        reason=ARGS["reason"],
        actor_run_id=ADMIN.run_id,
        request_id=REQUEST_ID,
    )
    if attempt_outcome == "uncertain":
        finish_planning_reopen_attempt(
            conn, attempt_id=attempt["attempt_id"], outcome="uncertain"
        )
    complete_request(
        conn,
        request_id=REQUEST_ID,
        result=error_envelope(
            "reopen-planning",
            DishRuleError(
                "BACKEND_UNCERTAIN", "unresolved", rule="planning_reopen_outcome_uncertain"
            ),
            task_gid="t",
        ),
    )

    conn.close()

    upgraded = initialize_database(db_path)
    try:
        request = upgraded.execute(
            "SELECT status,result_json,completed_at FROM service_requests WHERE request_id=?",
            (REQUEST_ID,),
        ).fetchone()
        assert tuple(request) == ("pending", None, None)
        assert upgraded.execute(
            "SELECT task_gid FROM planning_reopen_attempts WHERE attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()[0] == "t"
        finish_planning_reopen_attempt(
            upgraded,
            attempt_id=attempt["attempt_id"],
            outcome="confirmed",
            confirmed_modified_at="m1",
        )
        assert upgraded.execute(
            "SELECT outcome FROM planning_reopen_attempts WHERE attempt_id=?",
            (attempt["attempt_id"],),
        ).fetchone()[0] == "confirmed"
    finally:
        upgraded.close()


def test_planning_reopen_return_to_baseline_with_advanced_version_is_uncertain(tmp_path):
    from dish_tool.errors import BackendFailure

    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)

    def rejected_after_intervening_change(*, task_gid, completed):
        backend.modified_at = "m1"
        raise BackendFailure(
            "BACKEND_REJECTED", "rejected after another mutation",
            rule="backend_rejected", retryable=True,
        )

    backend.update_task_completed = rejected_after_intervening_change
    result = service.execute_admin(
        "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
    )
    assert result["code"] == "BACKEND_UNCERTAIN"
    assert result["errors"][0]["rule"] == "planning_reopen_outcome_uncertain"
    attempt, _request, _domain, _invocation = _rows(service)
    assert attempt["outcome"] == "uncertain"
    assert attempt["expected_modified_at"] == "m0"
    assert attempt["expected_version_reliable"] == 1
