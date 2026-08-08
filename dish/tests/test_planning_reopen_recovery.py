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
        managed_thread(target=replay, args=(worker,)) for worker in services
    ]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    assert first_update_entered.wait(timeout=5)
    assert not duplicate_update_entered.wait(timeout=0.5)
    release_first_update.set()
    for thread in threads:
        join_thread(thread, timeout=5)
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
        restarted,
        request_id="a0000000-0000-4000-8000-000000000005",
        challenge_request_id="a0000000-0000-4000-8000-000000000007",
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

    other = confirmed_planning_start(
        restarted,
        {"agent": "gpt", "task_gid": "other", "kind": "planning"},
        principal=PLANNER,
        challenge_request_id="a0000000-0000-4000-8000-000000000008",
        start_request_id="a0000000-0000-4000-8000-000000000004",
    )
    assert other["ok"]
    assert other["task_gid"] == "other"
