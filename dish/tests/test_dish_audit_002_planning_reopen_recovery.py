from __future__ import annotations

import shlex
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
from tests.test_dish_tool_r52_request_restore_durability import Backend, _service


class SimulatedProcessDeath(BaseException):
    pass


ADMIN = ServicePrincipal(owner_id="admin", run_id="marco-run")
PLANNER = ServicePrincipal(owner_id="action", run_id="planner-run")
REQUEST_ID = "a0000000-0000-4000-8000-000000000001"
FRESH_ADMIN_ID = "a0000000-0000-4000-8000-000000000002"
FRESH_START_ID = "a0000000-0000-4000-8000-000000000003"
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


def _start(service, request_id=FRESH_START_ID):
    return service.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "t", "kind": "planning"},
        principal=PLANNER,
        request_id=request_id,
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


def test_crash_before_external_reopen_exact_replay_safely_resumes(tmp_path, monkeypatch):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)

    def die_before_effect(*, task_gid, completed):
        raise SimulatedProcessDeath("before external effect")

    with monkeypatch.context() as killed:
        killed.setattr(backend, "update_task_completed", die_before_effect)
        with pytest.raises(SimulatedProcessDeath):
            service.execute_admin(
                "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
            )

    attempt, request, domain, _invocation = _rows(service)
    assert attempt["outcome"] == "started"
    assert request["status"] == "pending"
    assert backend.completed is True
    assert domain == 0

    restarted = _restart(service, backend)
    startup = restarted.startup_check()
    recovery = startup["startup"]["planning_reopen_recovery"]
    assert recovery["resume_safe"] == 1
    assert recovery["pending"][0]["original_request_id"] == REQUEST_ID
    assert ARGS["reason"] in recovery["pending"][0]["admin_command"]
    _assert_unresolved_blocked(restarted)

    replay = _exact_replay(restarted)
    assert replay["ok"]
    assert backend.completed is False
    assert backend.reopens == 1
    attempt, request, domain, invocation = _rows(restarted)
    assert attempt["outcome"] == "confirmed"
    assert request["status"] == "completed"
    assert domain == invocation == 1

    stored = _exact_replay(restarted)
    assert stored["ok"]
    assert stored["data"]["request_replayed"] is True
    assert backend.reopens == 1


def test_concurrent_exact_replays_issue_external_reopen_once(tmp_path, monkeypatch):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)

    with monkeypatch.context() as killed:
        killed.setattr(
            backend,
            "update_task_completed",
            lambda **kwargs: (_ for _ in ()).throw(
                SimulatedProcessDeath("before external effect")
            ),
        )
        with pytest.raises(SimulatedProcessDeath):
            service.execute_admin(
                "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
            )

    start = threading.Barrier(3)
    first_update_entered = threading.Event()
    duplicate_update_entered = threading.Event()
    release_first_update = threading.Event()
    update_lock = threading.Lock()

    def controlled_update(*, task_gid, completed):
        with update_lock:
            backend.reopens += 1
            call_number = backend.reopens
        if call_number == 1:
            first_update_entered.set()
            assert release_first_update.wait(timeout=5)
        else:
            duplicate_update_entered.set()
        backend.completed = completed
        backend.modified_at = f"m{call_number}"

    monkeypatch.setattr(backend, "update_task_completed", controlled_update)
    services = [_restart(service, backend), _restart(service, backend)]
    results: list[dict] = []
    failures: list[BaseException] = []

    def replay(worker):
        try:
            start.wait(timeout=5)
            results.append(_exact_replay(worker))
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    threads = [
        threading.Thread(target=replay, args=(worker,)) for worker in services
    ]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    assert first_update_entered.wait(timeout=5)
    assert not duplicate_update_entered.wait(timeout=0.5)
    release_first_update.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert failures == []
    assert len(results) == 2
    assert all(result["ok"] for result in results)
    assert backend.reopens == 1
    attempt, request, domain, invocation = _rows(service)
    assert attempt["outcome"] == "confirmed"
    assert request["status"] == "completed"
    assert domain == invocation == 1


def test_crash_after_external_reopen_is_confirmed_without_repeating_effect(tmp_path, monkeypatch):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)
    original_update = backend.update_task_completed

    def die_after_effect(*, task_gid, completed):
        original_update(task_gid=task_gid, completed=completed)
        raise SimulatedProcessDeath("after external effect")

    with monkeypatch.context() as killed:
        killed.setattr(backend, "update_task_completed", die_after_effect)
        with pytest.raises(SimulatedProcessDeath):
            service.execute_admin(
                "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
            )

    restarted = _restart(service, backend)
    startup = restarted.startup_check()
    recovery = startup["startup"]["planning_reopen_recovery"]
    assert recovery["applied_pending_replay"] == 1
    assert recovery["pending"][0]["original_request_id"] == REQUEST_ID
    assert backend.reopens == 1
    _assert_unresolved_blocked(restarted)

    replay = _exact_replay(restarted)
    assert replay["ok"]
    assert backend.reopens == 1
    attempt, request, domain, invocation = _rows(restarted)
    assert attempt["outcome"] == "confirmed"
    assert request["status"] == "completed"
    assert domain == invocation == 1
    stored = _exact_replay(restarted)
    assert stored["data"]["request_replayed"] is True
    assert backend.reopens == 1


def test_crash_after_reread_before_attempt_finalization_reconciles(tmp_path, monkeypatch):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)

    with monkeypatch.context() as killed:
        killed.setattr(
            task_store,
            "finish_planning_reopen_attempt",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                SimulatedProcessDeath("after reread")
            ),
        )
        with pytest.raises(SimulatedProcessDeath):
            service.execute_admin(
                "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
            )

    restarted = _restart(service, backend)
    startup = restarted.startup_check()
    assert startup["startup"]["planning_reopen_recovery"]["applied_pending_replay"] == 1
    _assert_unresolved_blocked(restarted)
    replay = _exact_replay(restarted)
    assert replay["ok"]
    assert backend.reopens == 1
    attempt, request, domain, invocation = _rows(restarted)
    assert attempt["outcome"] == "confirmed"
    assert request["status"] == "completed"
    assert domain == invocation == 1


def test_crash_after_attempt_finalization_before_request_completion_is_replayed(tmp_path, monkeypatch):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)

    with monkeypatch.context() as killed:
        killed.setattr(
            service_application,
            "complete_request",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                SimulatedProcessDeath("before request completion")
            ),
        )
        with pytest.raises(SimulatedProcessDeath):
            service.execute_admin(
                "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
            )

    attempt, request, domain, invocation = _rows(service)
    assert attempt["outcome"] == "confirmed"
    assert request["status"] == "pending"
    assert domain == invocation == 1
    _assert_unresolved_blocked(service)

    def backend_unavailable():
        raise RuntimeError("Asana unavailable during restart")

    restarted = DishService(
        service.config,
        backend_factory=backend_unavailable,
        release_loader=service.release_loader,
    )
    startup = restarted.startup_check()
    assert startup["startup"]["planning_reopen_recovery"]["confirmed"] == 1
    assert startup["startup"]["planning_reopen_recovery"]["errors"] == []

    # Startup completed the journal from local terminal evidence. Planning can
    # proceed once the backend is available again, and exact replay is stored.
    restarted = _restart(service, backend)
    allowed = _start(
        restarted, request_id="a0000000-0000-4000-8000-000000000005"
    )
    assert allowed["ok"]
    replay = _exact_replay(restarted)
    assert replay["ok"]
    assert replay["data"]["request_replayed"] is True
    assert backend.reopens == 1
    _attempt, request, domain, invocation = _rows(restarted)
    assert request["status"] == "completed"
    assert domain == invocation == 1


def test_terminal_attempt_exact_replay_completes_without_backend(tmp_path, monkeypatch):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)

    with monkeypatch.context() as killed:
        killed.setattr(
            service_application,
            "complete_request",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                SimulatedProcessDeath("before request completion")
            ),
        )
        with pytest.raises(SimulatedProcessDeath):
            service.execute_admin(
                "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
            )

    def backend_unavailable():
        raise RuntimeError("Asana unavailable during exact replay")

    restarted = DishService(
        service.config,
        backend_factory=backend_unavailable,
        release_loader=service.release_loader,
    )
    replay = _exact_replay(restarted)
    assert replay["ok"]
    assert replay["data"]["request_id"] == REQUEST_ID
    assert backend.reopens == 1
    _attempt, request, domain, invocation = _rows(restarted)
    assert request["status"] == "completed"
    assert domain == invocation == 1

    stored = _exact_replay(restarted)
    assert stored["ok"]
    assert stored["data"]["request_replayed"] is True


def test_reread_failure_stays_pending_then_exact_replay_confirms(tmp_path):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)
    original_update = backend.update_task_completed

    def apply_then_break_reread(*, task_gid, completed):
        original_update(task_gid=task_gid, completed=completed)
        backend.fail_next_read = True

    backend.update_task_completed = apply_then_break_reread
    result = service.execute_admin(
        "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
    )
    assert result["code"] == "BACKEND_UNCERTAIN"
    attempt, request, domain, invocation = _rows(service)
    assert attempt["outcome"] == "uncertain"
    assert request["status"] == "pending"
    assert domain == 0
    assert invocation == 1

    backend.update_task_completed = original_update
    replay = _exact_replay(service)
    assert replay["ok"]
    assert backend.reopens == 1
    attempt, request, domain, invocation = _rows(service)
    assert attempt["outcome"] == "confirmed"
    assert request["status"] == "completed"
    assert domain == invocation == 1


def test_genuinely_uncertain_reopen_blocks_only_its_task_and_never_retries(tmp_path, monkeypatch):
    backend = CompletedBackend()
    service, _ = _service(tmp_path, backend)

    def die_before_effect(*, task_gid, completed):
        raise SimulatedProcessDeath("before external effect")

    with monkeypatch.context() as killed:
        killed.setattr(backend, "update_task_completed", die_before_effect)
        with pytest.raises(SimulatedProcessDeath):
            service.execute_admin(
                "reopen-planning", ARGS, principal=ADMIN, request_id=REQUEST_ID
            )

    backend.modified_at = "m-external-drift"
    restarted = _restart(service, backend)
    startup = restarted.startup_check()
    recovery = startup["startup"]["planning_reopen_recovery"]
    assert recovery["uncertain"] == 1
    _assert_unresolved_blocked(restarted)

    replay = _exact_replay(restarted)
    assert replay["code"] == "BACKEND_UNCERTAIN"
    assert replay["data"]["original_request_id"] == REQUEST_ID
    assert "--request-id " + REQUEST_ID in replay["data"]["admin_command"]
    assert ARGS["reason"] in replay["data"]["admin_command"]
    assert backend.reopens == 0
    attempt, request, domain, _invocation = _rows(restarted)
    assert attempt["outcome"] == "uncertain"
    assert request["status"] == "pending"
    assert domain == 0

    other = restarted.execute_agent(
        "start",
        {"agent": "gpt", "task_gid": "other", "kind": "planning"},
        principal=PLANNER,
        request_id="a0000000-0000-4000-8000-000000000004",
    )
    assert other["ok"]
    assert other["task_gid"] == "other"


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
    conn = initialize_database(db_path)
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

    conn.execute("DROP TRIGGER planning_reopen_attempts_status_monotonic_update")
    conn.execute(
        """CREATE TRIGGER planning_reopen_attempts_status_monotonic_update
        BEFORE UPDATE OF outcome, finished_at, confirmed_modified_at ON planning_reopen_attempts
        WHEN OLD.outcome <> 'started'
          OR NEW.outcome NOT IN ('confirmed','not_applied','uncertain')
          OR NEW.finished_at IS NULL
        BEGIN SELECT RAISE(ABORT, 'planning reopen attempt completion is one-way'); END"""
    )
    conn.execute("DELETE FROM schema_migrations WHERE version=30")
    conn.execute("PRAGMA user_version=29")
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
