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
from dish_tool.admin_cli import build_parser as build_admin_parser
from dish_tool.constants import COOKING_PROJECT_GID
from dish_tool.database import (
    begin_planning_reopen_attempt,
    finish_planning_reopen_attempt,
    initialize_database,
)
from dish_tool.errors import DishRuleError
from dish_tool.results import error_envelope
from dish_tool.database_schema import MIGRATIONS, _execute_script_statements
from tests.planning_intent_support import confirmed_planning_start
from tests.support.request_restore import Backend, _service


class SimulatedProcessDeath(BaseException):
    pass


ADMIN = ServicePrincipal(owner_id="admin", run_id="marco-run")
PLANNER = ServicePrincipal(owner_id="action", run_id="planner-run")
REQUEST_ID = "a0000000-0000-4000-8000-000000000001"
FRESH_ADMIN_ID = "a0000000-0000-4000-8000-000000000002"
FRESH_START_ID = "a0000000-0000-4000-8000-000000000003"
FRESH_START_CHALLENGE_ID = "a0000000-0000-4000-8000-000000000006"
ARGS = {"task_gid": "t", "reason": "repeat the cook"}


class CompletedBackend(Backend):
    def __init__(self):
        super().__init__()
        self.title = "Bare"
        self.notes = ""
        self.completed = True
        self.modified_at = "m0"
        self.reopens = 0
        self.fail_next_read = False

    def read_task(self, gid):
        if self.fail_next_read:
            self.fail_next_read = False
            raise RuntimeError("reread unavailable")
        task = super().read_task(gid)
        if gid == "other":
            task["completed"] = False
            task["modified_at"] = "other-m0"
        else:
            task["completed"] = self.completed
            task["modified_at"] = self.modified_at
        return task

    def update_task_completed(self, *, task_gid, completed):
        self.reopens += 1
        self.completed = completed
        self.modified_at = f"m{self.reopens}"


def _restart(service, backend):
    return DishService(
        service.config,
        backend_factory=lambda: backend,
        release_loader=service.release_loader,
    )


def _rows(service):
    conn = initialize_database(service.config.db_path)
    try:
        attempt = conn.execute(
            "SELECT * FROM planning_reopen_attempts WHERE task_gid='t'"
        ).fetchone()
        request = conn.execute(
            "SELECT * FROM service_requests WHERE request_id=?", (REQUEST_ID,)
        ).fetchone()
        domain_audits = conn.execute(
            """SELECT COUNT(*) FROM audit_events
                 WHERE event_type='planning.task_reopened'
                   AND json_extract(details, '$.attempt_id')=?""",
            (None if attempt is None else attempt["attempt_id"],),
        ).fetchone()[0]
        invocation_audits = conn.execute(
            """SELECT COUNT(*) FROM audit_events
                 WHERE event_type='dish-admin.reopen-planning'
                   AND json_extract(details, '$.request_id')=?""",
            (REQUEST_ID,),
        ).fetchone()[0]
        return attempt, request, domain_audits, invocation_audits
    finally:
        conn.close()


def _start(
    service,
    request_id=FRESH_START_ID,
    challenge_request_id=FRESH_START_CHALLENGE_ID,
):
    return confirmed_planning_start(
        service,
        {"agent": "gpt", "task_gid": "t", "kind": "planning"},
        principal=PLANNER,
        challenge_request_id=challenge_request_id,
        start_request_id=request_id,
    )


def _fresh_reopen(service):
    return service.execute_admin(
        "reopen-planning", ARGS, principal=ADMIN, request_id=FRESH_ADMIN_ID
    )


def _exact_replay(service):
    return service.execute_admin(
        "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
    )


def _assert_unresolved_blocked(service):
    start = _start(service)
    assert start["code"] == "BACKEND_UNCERTAIN"
    assert start["errors"][0]["rule"] == "planning_reopen_reconciliation_required"
    assert start["data"]["original_request_id"] == REQUEST_ID
    fresh = _fresh_reopen(service)
    assert fresh["code"] == "BACKEND_UNCERTAIN"
    assert fresh["errors"][0]["rule"] == "planning_reopen_reconciliation_required"

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

    thread = threading.Thread(target=run_start, name="planning-start")
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
    thread.join(timeout=5)
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
            expected_modified_at=live.modified_at, reason=ARGS["reason"],
            actor_run_id=ADMIN.run_id, request_id=REQUEST_ID,
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
